"""Verified restore: work on a temporary copy, never touch live data until it checks out.

The backup's database copy is verified against the manifest, the *current* deletion
tombstones are applied to it (a restore must not resurrect deleted content), the
schema must match the running head, and only then is the live file replaced atomically.
A failure before the swap leaves the current data untouched; a failure after the swap
rolls back from a safety copy. Restored executions stay ``INTERRUPTED``: a restore
never auto-resumes research.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.lifecycle_models import (
    BackupManifest,
    DeletionRecord,
    RestoreRecord,
)
from personal_agent.bootstrap.backup import MANIFEST_FILENAME, BackupService, _backup_sqlite
from personal_agent.bootstrap.config import Policy
from personal_agent.bootstrap.lifespan import mark_interrupted_runs, migration_head
from personal_agent.domain.common import AppError, iso_z, new_id
from personal_agent.ports.contracts import Clock


class RestoreService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        *,
        data_dir: Path,
        policy: Policy,
    ) -> None:
        self._database = database
        self._clock = clock
        self._data_dir = data_dir
        self._policy = policy
        self._backups = BackupService(database, clock, data_dir=data_dir, policy=policy)

    async def restore(
        self,
        *,
        backup_id: str,
        manifest_hash: str,
        acknowledge_lifecycle: bool = True,
    ) -> dict[str, Any]:
        if not acknowledge_lifecycle:
            raise AppError(
                code="RESTORE_BLOCKED",
                message="a restore needs an explicit acknowledgement of lifecycle rules",
                status=422,
            )
        async with self._database.session() as session:
            backup = await session.get(BackupManifest, backup_id)
            if backup is None:
                raise AppError(
                    code="BACKUP_NOT_FOUND", message="backup not found", status=404
                )
            if backup.status == "DELETED":
                raise AppError(code="BACKUP_DELETED", message="backup deleted", status=410)
            if backup.status == "EXPIRED" or backup.expires_at <= self._clock.now():
                raise AppError(code="BACKUP_EXPIRED", message="backup expired", status=410)
            if backup.manifest_hash != manifest_hash:
                raise AppError(
                    code="MANIFEST_HASH_MISMATCH",
                    message="the manifest hash does not match this backup",
                    status=412,
                )
            head = migration_head()
            if backup.schema_version != head:
                raise AppError(
                    code="BACKUP_INCOMPATIBLE",
                    message="the backup schema is not compatible with this build",
                    status=422,
                    details={"backup": backup.schema_version, "current": head},
                )
            backup_dir = self._data_dir / "backups" / backup.id
            if not (backup_dir / "personal-agent.db").exists():
                raise AppError(
                    code="BACKUP_VERIFICATION_FAILED",
                    message="the backup database file is missing",
                    status=422,
                )
            blobs = list(backup.blob_hashes)
            manifest_path = backup_dir / MANIFEST_FILENAME
            if not manifest_path.exists():
                raise AppError(
                    code="BACKUP_VERIFICATION_FAILED",
                    message="the backup manifest is missing",
                    status=422,
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("manifest_hash") != backup.manifest_hash:
                raise AppError(
                    code="MANIFEST_HASH_MISMATCH",
                    message="the manifest file does not match the recorded backup",
                    status=409,
                )
            if manifest.get("schema_version") != head:
                raise AppError(
                    code="BACKUP_INCOMPATIBLE",
                    message="the backup file schema is not compatible with this build",
                    status=409,
                    details={"backup": manifest.get("schema_version"), "current": head},
                )

            record = RestoreRecord(
                id=new_id(),
                backup_id=backup.id,
                status="VALIDATING",
                stages=[{"name": "VALIDATE_BACKUP", "status": "RUNNING"}],
                error=None,
                post_restore={},
                started_at=self._clock.now(),
                completed_at=None,
                revision=1,
            )
            session.add(record)
            await session.commit()
            restore_id = record.id
            started_at = record.started_at
            backup_values: dict[str, Any] = {
                "manifest_hash": backup.manifest_hash,
                "blob_hashes": list(backup.blob_hashes),
                "blob_count": backup.blob_count,
                "total_bytes": backup.total_bytes,
                "blob_hashes_digest": backup.blob_hashes_digest,
            }

        result = self._backups._verify_files(
            backup_dir, backup_dir / "personal-agent.db", blobs,
            expected_db_sha256=manifest.get("db_sha256"),
        )
        if not result["ok"]:
            await self._fail(restore_id, "BACKUP_VERIFICATION_FAILED", result["problems"])
            raise AppError(
                code="BACKUP_VERIFICATION_FAILED",
                message="the backup did not verify",
                status=422,
                details=result,
            )

        tombstones = await self._current_tombstones()
        await self._stage(restore_id, "APPLYING_TOMBSTONES", tombstones)

        work_dir = self._data_dir / "runtime" / f"restore-{restore_id}"
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        staged_db = work_dir / "personal-agent.db"
        safety_db = work_dir / "current-safety.db"
        shutil.copy2(backup_dir / "personal-agent.db", staged_db)
        # The rollback copy must be a consistent snapshot: a raw copy of a live
        # WAL database can miss recent commits. Same SQLite backup API as backup.
        _backup_sqlite(self._database.path, safety_db)
        artifacts_dir = self._data_dir / "artifacts"
        safety_artifacts = work_dir / "artifacts-safety"
        if artifacts_dir.exists():
            shutil.copytree(artifacts_dir, safety_artifacts)
        try:
            applied = self._apply_tombstones(staged_db, tombstones)
            self._check_database(staged_db)
            await self._stage(restore_id, "RESTORING", None)
            await self._database.dispose()
            self._swap_database(staged_db)
            self._restore_blobs(backup_dir, blobs)
            interrupted = await mark_interrupted_runs(self._database, self._clock)
        except Exception as error:  # any failure must roll back
            rollback_ok = True
            try:
                await self._database.dispose()
                if self._database.path.exists() and safety_db.exists():
                    self._swap_database(safety_db)
                if safety_artifacts.exists():
                    shutil.rmtree(artifacts_dir, ignore_errors=True)
                    shutil.copytree(safety_artifacts, artifacts_dir)
            except Exception as rollback_error:
                rollback_ok = False
                await self._fail(
                    restore_id,
                    "ROLLBACK_FAILED",
                    [str(error), str(rollback_error)],
                    rolled_back=False,
                )
                raise AppError(
                    code="RESTORE_ROLLBACK_FAILED",
                    message=(
                        "the restore failed and the original database or blobs "
                        "could not be fully restored"
                    ),
                    status=500,
                    details={"stage": "RESTORING", "error": str(error)},
                ) from error
            if rollback_ok:
                await self._fail(
                    restore_id, "ROLLED_BACK", [str(error)], rolled_back=True
                )
            raise AppError(
                code="RESTORE_FAILED",
                message="the restore failed; the current data was kept",
                status=500,
                details={"stage": "RESTORING", "error": str(error)},
            ) from error

        # Expire what the policy says is due before the restored data is served.
        from personal_agent.application.data_lifecycle import DataLifecycleService

        lifecycle = DataLifecycleService(
            self._database,
            self._clock,
            BlobStore(self._data_dir / "artifacts"),
            retention=self._policy.retention,
            data_dir=self._data_dir,
        )
        retention = await lifecycle.apply_retention()
        post_restore = {
            "interrupted_runs": interrupted,
            "tombstones_applied": applied,
            "retention": retention,
            "auto_resumed": False,
        }
        async with self._database.session() as session:
            # The restored database is the backup's copy, so the restore record and
            # the final manifest facts have to be re-applied to it. The manifest row
            # exists in the snapshot (it was inserted before the copy) with a
            # placeholder hash, which must not survive.
            restored_backup = await session.get(BackupManifest, backup_id)
            if restored_backup is not None:
                restored_backup.status = "VERIFIED"
                restored_backup.manifest_hash = backup_values["manifest_hash"]
                restored_backup.blob_hashes = backup_values["blob_hashes"]
                restored_backup.blob_count = backup_values["blob_count"]
                restored_backup.total_bytes = backup_values["total_bytes"]
                restored_backup.blob_hashes_digest = backup_values["blob_hashes_digest"]
                restored_backup.usable = True
                restored_backup.verified_at = self._clock.now()
            completed = await session.get(RestoreRecord, restore_id)
            if completed is None:
                completed = RestoreRecord(
                    id=restore_id,
                    backup_id=backup_id,
                    status="COMPLETED",
                    stages=[],
                    error=None,
                    post_restore={},
                    started_at=started_at,
                    completed_at=None,
                    revision=1,
                )
                session.add(completed)
            completed.status = "COMPLETED"
            completed.stages = [
                {"name": "VALIDATE_BACKUP", "status": "SUCCEEDED"},
                {"name": "APPLYING_TOMBSTONES", "status": "SUCCEEDED", "applied": applied},
                {"name": "RESTORING", "status": "SUCCEEDED"},
                {"name": "VERIFYING", "status": "SUCCEEDED"},
            ]
            completed.post_restore = post_restore
            completed.completed_at = self._clock.now()
            completed.revision += 1
            await session.commit()
            # A restored tree must keep the same single-user boundary; re-verify
            # instead of assuming the copied ACLs are the intended ones.
            from personal_agent.bootstrap.acl import secure_directory

            secure_directory(self._data_dir)
            secure_directory(self._data_dir / "artifacts")
            return {
                "restore_id": restore_id,
                "backup_id": backup_id,
                "status": "COMPLETED",
                "post_restore": post_restore,
                "status_url": f"/api/v1/maintenance/restores/{restore_id}",
            }

    async def get(self, restore_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            record = await session.get(RestoreRecord, restore_id)
            if record is None:
                raise AppError(
                    code="RESTORE_NOT_FOUND", message="restore not found", status=404
                )
            return self._serialize(record)

    # ------------------------------------------------------------------
    async def _current_tombstones(self) -> list[str]:
        async with self._database.session() as session:
            rows = (await session.scalars(select(DeletionRecord))).all()
            affected: set[str] = set()
            for row in rows:
                affected.update(str(item) for item in row.affected_ids)
            return sorted(affected)

    def _apply_tombstones(self, database_path: Path, tombstones: list[str]) -> int:
        if not tombstones:
            return 0
        connection = sqlite3.connect(database_path.as_posix())
        try:
            applied = 0
            for artifact_id in tombstones:
                cursor = connection.execute(
                    "UPDATE artifacts SET access_state = 'DELETED' WHERE id = ?",
                    (artifact_id,),
                )
                applied += cursor.rowcount if cursor.rowcount > 0 else 0
            placeholders = ",".join("?" * len(tombstones))
            connection.execute(
                f"DELETE FROM reused_results WHERE artifact_id IN ({placeholders})",
                tombstones,
            )
            # Persist a deletion tombstone into the restored database itself, so a
            # second restore from the same backup cannot resurrect the content.
            digest = hashlib.sha256(",".join(tombstones).encode("utf-8")).hexdigest()
            stamp = iso_z(self._clock.now())
            connection.execute(
                "INSERT OR IGNORE INTO deletion_records "
                "(id, status, run_id, artifact_id, affected_ids, backup_ids, "
                "tombstone_hash, reason, requested_at, completed_at, revision) "
                "VALUES (?, 'COMPLETED', NULL, NULL, ?, '[]', ?, ?, ?, ?, 1)",
                (
                    f"restore-{digest[:28]}",
                    json.dumps(tombstones),
                    f"sha256:{digest}",
                    "restore tombstone",
                    stamp,
                    stamp,
                ),
            )
            connection.commit()
            return applied
        finally:
            connection.close()

    @staticmethod
    def _check_database(database_path: Path) -> None:
        connection = sqlite3.connect(database_path.as_posix())
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise AppError(
                    code="RESTORE_BLOCKED",
                    message="the restored database failed its integrity check",
                    status=422,
                )
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise AppError(
                    code="RESTORE_BLOCKED",
                    message="the restored database has foreign key violations",
                    status=422,
                )
        finally:
            connection.close()

    def _swap_database(self, source: Path) -> None:
        target = self._database.path
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            sidecar.unlink(missing_ok=True)
        os.replace(source, target)

    def _restore_blobs(self, backup_dir: Path, blobs: list[dict[str, Any]]) -> None:
        root = self._data_dir / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        for entry in blobs:
            source = backup_dir / "artifacts" / str(entry["path"])
            if not source.exists():
                continue
            destination = root / str(entry["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    async def _stage(self, restore_id: str, name: str, detail: Any) -> None:
        async with self._database.session() as session:
            record = await session.get(RestoreRecord, restore_id)
            if record is None:
                return
            stage = {"name": name, "status": "RUNNING" if detail is None else "SUCCEEDED"}
            if detail is not None:
                stage["detail"] = detail
            record.stages = [*record.stages, stage]
            record.status = name if name in {"APPLYING_TOMBSTONES", "RESTORING"} else record.status
            record.revision += 1
            await session.commit()

    async def _fail(
        self, restore_id: str, stage: str, problems: list[str], *, rolled_back: bool = False
    ) -> None:
        async with self._database.session() as session:
            record = await session.get(RestoreRecord, restore_id)
            if record is None:
                return
            record.status = "ROLLED_BACK" if rolled_back else "FAILED"
            record.error = "; ".join(problems)[:2000]
            record.stages = [*record.stages, {"name": stage, "status": "FAILED"}]
            record.completed_at = self._clock.now()
            record.revision += 1
            await session.commit()

    @staticmethod
    def _serialize(record: RestoreRecord) -> dict[str, Any]:
        return {
            "restore_id": record.id,
            "backup_id": record.backup_id,
            "status": record.status,
            "stages": list(record.stages),
            "error": record.error,
            "post_restore": dict(record.post_restore),
            "started_at": iso_z(record.started_at),
            "completed_at": iso_z(record.completed_at) if record.completed_at else None,
            "revision": record.revision,
        }


__all__ = ["RestoreService"]
