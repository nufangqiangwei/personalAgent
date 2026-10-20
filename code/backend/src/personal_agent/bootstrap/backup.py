"""Consistent backups: SQLite backup API plus a verified blob/hash manifest.

A backup must never be a raw copy of a live database (WAL could be missed). The
SQLite backup API produces a consistent snapshot, then every managed blob is hashed
and the deletion tombstones are summarised. The manifest is verified before it is
reported as usable; a backup that cannot be verified never becomes restorable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.lifecycle_models import (
    BackupManifest,
    DeletionRecord,
)
from personal_agent.bootstrap.config import APP_VERSION, Policy
from personal_agent.bootstrap.lifespan import migration_head
from personal_agent.domain.common import AppError, iso_z, new_id, stable_hash
from personal_agent.ports.contracts import Clock

FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target.as_posix())
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class BackupService:
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

    def _dir(self, backup_id: str) -> Path:
        return self._data_dir / "backups" / backup_id

    # ------------------------------------------------------------------
    async def create(self, *, reason: str | None = None) -> dict[str, Any]:
        backup_id = new_id()
        target_dir = self._dir(backup_id)
        target_dir.mkdir(parents=True, exist_ok=False)
        from personal_agent.bootstrap.acl import secure_directory

        secure_directory(target_dir)
        database_path = target_dir / "personal-agent.db"
        now = self._clock.now()
        schema_version = migration_head() or "unknown"
        # Insert the manifest before copying the database so the snapshot itself
        # records that this backup exists; a restored database can then reference the
        # manifest that describes it.
        async with self._database.session() as session:
            session.add(
                BackupManifest(
                    id=backup_id,
                    status="CREATING",
                    format_version=FORMAT_VERSION,
                    schema_version=schema_version,
                    app_version=APP_VERSION,
                    policy_version=self._policy.version,
                    db_revision=schema_version,
                    manifest_hash=stable_hash({"placeholder": backup_id}),
                    blob_hashes=[],
                    blob_count=0,
                    total_bytes=0,
                    blob_hashes_digest=stable_hash([]),
                    deletion_tombstone_hash=stable_hash([]),
                    unknown_model_request=False,
                    verification={"ok": False, "problems": ["creation in progress"]},
                    usable=False,
                    secret_free=True,
                    created_at=now,
                    verified_at=None,
                    expires_at=now + timedelta(days=self._policy.retention.backup_days),
                    deleted_at=None,
                )
            )
            await session.commit()
        _backup_sqlite(self._database.path, database_path)

        blobs_dir = target_dir / "artifacts"
        blob_entries: list[dict[str, Any]] = []
        artifacts_root = self._data_dir / "artifacts"
        if artifacts_root.exists():
            for candidate in sorted(artifacts_root.rglob("*")):
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(artifacts_root).as_posix()
                digest, size = file_digest(candidate)
                destination = blobs_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, destination)
                blob_entries.append(
                    {"path": relative, "sha256": digest, "size_bytes": size}
                )

        async with self._database.session() as session:
            deletions = list(
                (await session.scalars(select(DeletionRecord))).all()
            )
            tombstones = stable_hash(
                [
                    {
                        "id": row.id,
                        "affected": list(row.affected_ids),
                        "tombstone_hash": row.tombstone_hash,
                        "status": row.status,
                    }
                    for row in deletions
                ]
            )
        blob_digest = stable_hash(blob_entries)
        schema_version = migration_head() or "unknown"
        manifest_hash = stable_hash(
            {
                "format": FORMAT_VERSION,
                "schema": schema_version,
                "app": APP_VERSION,
                "policy": self._policy.version,
                "blobs": blob_digest,
                "tombstones": tombstones,
            }
        )
        now = self._clock.now()
        expires_at = now + timedelta(days=self._policy.retention.backup_days)
        verification = self._verify_files(target_dir, database_path, blob_entries)
        status = "VERIFIED" if verification["ok"] else "FAILED"
        manifest = {
            "format_version": FORMAT_VERSION,
            "schema_version": schema_version,
            "app_version": APP_VERSION,
            "policy_version": self._policy.version,
            "db_revision": schema_version,
            "manifest_hash": manifest_hash,
            "blob_hashes_digest": blob_digest,
            "deletion_tombstone_hash": tombstones,
            "blob_count": len(blob_entries),
            "total_bytes": sum(entry["size_bytes"] for entry in blob_entries),
            "verification": verification,
            "created_at": iso_z(now),
            "expires_at": iso_z(expires_at),
            "reason": reason,
            "secret_free": True,
        }
        (target_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        async with self._database.session() as session:
            row = await session.get(BackupManifest, backup_id)
            assert row is not None
            row.status = status
            row.schema_version = schema_version
            row.db_revision = schema_version
            row.manifest_hash = manifest_hash
            row.blob_hashes = blob_entries
            row.blob_count = len(blob_entries)
            row.total_bytes = manifest["total_bytes"]
            row.blob_hashes_digest = blob_digest
            row.deletion_tombstone_hash = tombstones
            row.verification = verification
            row.usable = status == "VERIFIED"
            row.verified_at = now if status == "VERIFIED" else None
            row.expires_at = expires_at
            await session.commit()
        return self.serialize(row)

    async def verify(self, backup_id: str, *, manifest_hash: str | None = None) -> dict[str, Any]:
        async with self._database.session() as session:
            row = await session.get(BackupManifest, backup_id)
            if row is None:
                raise AppError(
                    code="BACKUP_NOT_FOUND", message="backup not found", status=404
                )
            self._assert_available(row)
            if manifest_hash is not None and manifest_hash != row.manifest_hash:
                raise AppError(
                    code="MANIFEST_HASH_MISMATCH",
                    message="the manifest hash does not match this backup",
                    status=412,
                )
            target_dir = self._dir(row.id)
            verification = self._verify_files(
                target_dir, target_dir / "personal-agent.db", list(row.blob_hashes)
            )
            row.verification = verification
            row.status = "VERIFIED" if verification["ok"] else "FAILED"
            row.usable = verification["ok"]
            row.verified_at = self._clock.now() if verification["ok"] else None
            await session.commit()
            return {
                "backup_id": row.id,
                "status": row.status,
                "verification": verification,
                "usable": row.usable,
                "restore_allowed": row.usable,
            }

    async def list_backups(self) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.scalars(
                    select(BackupManifest).order_by(BackupManifest.created_at.desc())
                )
            ).all()
            return [self.serialize(row) for row in rows]

    async def get(self, backup_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            row = await session.get(BackupManifest, backup_id)
            if row is None:
                raise AppError(
                    code="BACKUP_NOT_FOUND", message="backup not found", status=404
                )
            self._assert_available(row)
            return self.serialize(row)

    async def expire_due(self) -> int:
        now = self._clock.now()
        async with self._database.session() as session:
            rows = (
                await session.scalars(
                    select(BackupManifest).where(
                        BackupManifest.expires_at <= now,
                        BackupManifest.status.notin_(["EXPIRED", "DELETED"]),
                    )
                )
            ).all()
            for row in rows:
                row.status = "EXPIRED"
                row.usable = False
            await session.commit()
            return len(rows)

    # ------------------------------------------------------------------
    def _assert_available(self, row: BackupManifest) -> None:
        if row.status == "DELETED":
            raise AppError(code="BACKUP_DELETED", message="backup was deleted", status=410)
        if row.status == "EXPIRED" or row.expires_at <= self._clock.now():
            raise AppError(code="BACKUP_EXPIRED", message="backup expired", status=410)
        if row.status not in {"VERIFIED", "VERIFYING", "CREATING"}:
            raise AppError(
                code="BACKUP_VERIFICATION_FAILED",
                message="backup is not usable",
                status=422,
            )

    def _verify_files(
        self, target_dir: Path, database_path: Path, blob_entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        problems: list[str] = []
        if not database_path.exists():
            problems.append("database copy is missing")
        else:
            try:
                connection = sqlite3.connect(database_path.as_posix())
                try:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
                finally:
                    connection.close()
                if not integrity or integrity[0] != "ok":
                    problems.append("database integrity check failed")
                if foreign:
                    problems.append("database has foreign key violations")
            except sqlite3.Error as error:  # pragma: no cover - corrupt file
                problems.append(f"database cannot be opened: {error}")
        for entry in blob_entries:
            path = target_dir / "artifacts" / str(entry["path"])
            if not path.exists():
                problems.append(f"blob missing: {entry['path']}")
                continue
            digest, _size = file_digest(path)
            if digest != entry["sha256"]:
                problems.append(f"blob hash mismatch: {entry['path']}")
        return {
            "ok": not problems,
            "checked_at": iso_z(self._clock.now()),
            "problems": problems,
            "schema_version": migration_head(),
        }

    @staticmethod
    def serialize(row: BackupManifest) -> dict[str, Any]:
        return {
            "id": row.id,
            "status": row.status,
            "usable": row.usable,
            "restore_allowed": row.usable and row.status == "VERIFIED",
            "format_version": row.format_version,
            "schema_version": row.schema_version,
            "app_version": row.app_version,
            "policy_version": row.policy_version,
            "db_revision": row.db_revision,
            "manifest_hash": row.manifest_hash,
            "blob_count": row.blob_count,
            "total_bytes": row.total_bytes,
            "blob_hashes_digest": row.blob_hashes_digest,
            "deletion_tombstone_hash": row.deletion_tombstone_hash,
            "unknown_model_request": row.unknown_model_request,
            "verification": dict(row.verification),
            "secret_free": row.secret_free,
            "created_at": iso_z(row.created_at),
            "verified_at": iso_z(row.verified_at) if row.verified_at else None,
            "expires_at": iso_z(row.expires_at),
            "deleted_at": iso_z(row.deleted_at) if row.deleted_at else None,
        }


__all__ = ["MANIFEST_FILENAME", "BackupService", "file_digest"]
