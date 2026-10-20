"""Data lifecycle: user deletion, retention and immediate reuse blocking.

Deleting a saved document takes effect at once: normal reads stop returning it, branch
reuse can no longer select it, and the tombstone stores only ids and hashes. Physical
file removal happens after the record is committed, so a crash never leaves a row that
claims a product which was not removed. Retention follows the versioned policy and is
applied here (startup, maintenance and after a restore).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from personal_agent.adapters.persistence.artifact_models import Artifact, Blob
from personal_agent.adapters.persistence.blob_store import BlobStore
from personal_agent.adapters.persistence.branch_models import ReusedResult
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.lifecycle_models import (
    BackupManifest,
    DeletionRecord,
)
from personal_agent.adapters.persistence.session_models import SessionLink
from personal_agent.bootstrap.config import RetentionPolicy
from personal_agent.domain.common import AppError, iso_z, new_id, paginate, stable_hash
from personal_agent.ports.contracts import Clock

DELETABLE_ITEM_TYPES = {"document", "artifact"}


class DataLifecycleService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        store: BlobStore,
        *,
        retention: RetentionPolicy,
        data_dir: Path,
    ) -> None:
        self._database = database
        self._clock = clock
        self._store = store
        self._retention = retention
        self._data_dir = data_dir
        self._cleanup_tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    async def list_items(
        self, *, kind: str | None = None, cursor: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        if kind is not None and kind not in {"document", "all"}:
            raise AppError(
                code="DATA_ITEM_TYPE_INVALID",
                message=f"unsupported data item type {kind!r}",
                status=422,
            )
        async with self._database.session() as session:
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).order_by(Artifact.created_at.desc())
                    )
                ).all()
            )
            items: list[dict[str, Any]] = []
            for artifact in artifacts:
                blob = await session.get(Blob, artifact.blob_id)
                shared = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Artifact)
                        .where(
                            Artifact.blob_id == artifact.blob_id,
                            Artifact.id != artifact.id,
                        )
                    )
                    or 0
                )
                items.append(
                    {
                        "id": artifact.id,
                        "type": "document",
                        "run_id": artifact.run_id,
                        "structure": artifact.structure,
                        "completeness": artifact.completeness,
                        "access_state": artifact.access_state,
                        "created_at": iso_z(artifact.created_at),
                        "expires_at": iso_z(blob.expires_at) if blob and blob.expires_at else None,
                        "deleted_at": iso_z(blob.deleted_at) if blob and blob.deleted_at else None,
                        "blob_id": artifact.blob_id,
                        "shared_artifact_references": shared,
                        # A shared blob is safe to unlink only when no other available
                        # artifact still needs it.
                        "deletable": artifact.access_state != "DELETED",
                        "keeps_shared_blob": shared > 0,
                    }
                )
        page = paginate(items, cursor=cursor, limit=limit)
        return {"items": page.items, "next_cursor": page.next_cursor}

    async def request_deletion(
        self,
        item_ids: list[str],
        *,
        acknowledge_resume_impact: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not acknowledge_resume_impact:
            raise AppError(
                code="RESUME_IMPACT_NOT_ACKNOWLEDGED",
                message="deleting saved content needs an explicit acknowledgement",
                status=422,
            )
        if not item_ids:
            raise AppError(
                code="DELETION_EMPTY",
                message="no data item was selected",
                status=422,
            )
        async with self._database.session() as session:
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(Artifact.id.in_(item_ids))
                    )
                ).all()
            )
            if len(artifacts) != len(set(item_ids)):
                raise AppError(
                    code="DATA_ITEM_NOT_FOUND",
                    message="one or more selected items no longer exist",
                    status=404,
                )
            now = self._clock.now()
            tombstone = stable_hash(
                {"deleted": sorted(artifact.id for artifact in artifacts), "at": iso_z(now)}
            )
            record = DeletionRecord(
                id=new_id(),
                status="REQUESTED",
                run_id=None,
                artifact_id=None,
                affected_ids=sorted(artifact.id for artifact in artifacts),
                backup_ids=[
                    row.id
                    for row in (
                        await session.scalars(
                            select(BackupManifest).where(
                                BackupManifest.status.in_(["VERIFIED", "VERIFYING"])
                            )
                        )
                    ).all()
                ],
                tombstone_hash=tombstone,
                reason=reason,
                requested_at=now,
                completed_at=None,
                revision=1,
            )
            session.add(record)
            paths: list[str] = []
            for artifact in artifacts:
                blob = await session.get(Blob, artifact.blob_id)
                artifact.access_state = "DELETED"
                # Any branch that reused this result loses the invalid dependency.
                await session.execute(
                    delete(ReusedResult).where(ReusedResult.artifact_id == artifact.id)
                )
                if blob is not None:
                    others = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(Artifact)
                            .where(
                                Artifact.blob_id == blob.id,
                                Artifact.id != artifact.id,
                                Artifact.access_state == "AVAILABLE",
                            )
                        )
                        or 0
                    )
                    if others == 0:
                        blob.deleted_at = now
                        paths.append(blob.managed_relative_path)
            await session.commit()
            record_id = record.id

        task = asyncio.create_task(self._physical_cleanup(record_id, paths))
        self._cleanup_tasks.append(task)
        task.add_done_callback(lambda item: self._cleanup_tasks.remove(item))
        return await self.get_deletion(record_id)

    async def get_deletion(self, record_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            record = await session.get(DeletionRecord, record_id)
            if record is None:
                raise AppError(
                    code="DELETION_NOT_FOUND", message="deletion not found", status=404
                )
            return self._serialize_deletion(record)

    async def wait_for_cleanup(self, timeout_seconds: float = 10.0) -> None:
        """Deterministic test hook: wait for pending physical removals."""
        if self._cleanup_tasks:
            await asyncio.wait_for(
                asyncio.gather(*self._cleanup_tasks), timeout=timeout_seconds
            )

    async def _physical_cleanup(self, record_id: str, paths: list[str]) -> None:
        failed: list[str] = []
        for path in paths:
            try:
                await self._store.delete(path)
            except (FileNotFoundError, OSError):
                failed.append(path)
        async with self._database.session() as session:
            record = await session.get(DeletionRecord, record_id)
            if record is None:
                return
            record.status = "PARTIAL" if failed else "COMPLETED"
            record.completed_at = self._clock.now()
            record.revision += 1
            await session.commit()

    # ------------------------------------------------------------------
    async def apply_retention(self) -> dict[str, int]:
        """Expire business content and destroy sessions, cache and logs on schedule."""
        now = self._clock.now()
        business_cutoff = now - timedelta(days=self._retention.business_content_days)
        session_cutoff = now - timedelta(days=self._retention.session_data_max_days)
        cache_cutoff = now - timedelta(days=self._retention.cache_and_logs_days)
        expired_artifacts = 0
        expired_backups = 0
        purged_files = 0

        async with self._database.session() as session:
            artifacts = list(
                (
                    await session.scalars(
                        select(Artifact).where(
                            Artifact.created_at < business_cutoff,
                            Artifact.access_state == "AVAILABLE",
                        )
                    )
                ).all()
            )
            for artifact in artifacts:
                artifact.access_state = "EXPIRED"
                blob = await session.get(Blob, artifact.blob_id)
                if blob is not None and blob.deleted_at is None:
                    blob.deleted_at = now
                expired_artifacts += 1
            await session.execute(
                delete(SessionLink).where(SessionLink.created_at < session_cutoff)
            )
            backups = (
                await session.scalars(
                    select(BackupManifest).where(
                        BackupManifest.expires_at <= now,
                        BackupManifest.status.notin_(["EXPIRED", "DELETED"]),
                    )
                )
            ).all()
            for backup in backups:
                backup.status = "EXPIRED"
                backup.usable = False
                expired_backups += 1
            await session.commit()

        for directory in (self._data_dir / "cache", self._data_dir / "logs"):
            if not directory.exists():
                continue
            for candidate in directory.rglob("*"):
                if not candidate.is_file():
                    continue
                try:
                    modified = candidate.stat().st_mtime
                except OSError:
                    continue
                if modified < cache_cutoff.timestamp():
                    candidate.unlink(missing_ok=True)
                    purged_files += 1
        return {
            "expired_artifacts": expired_artifacts,
            "expired_backups": expired_backups,
            "purged_files": purged_files,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _serialize_deletion(record: DeletionRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "status": record.status,
            "affected_ids": list(record.affected_ids),
            "backup_ids": list(record.backup_ids),
            "tombstone_hash": record.tombstone_hash,
            "reason": record.reason,
            "requested_at": iso_z(record.requested_at),
            "completed_at": iso_z(record.completed_at) if record.completed_at else None,
            "revision": record.revision,
        }

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


__all__ = ["DataLifecycleService"]
