"""Deletion records and backup manifests.

A deletion is an append-only record: the tombstone keeps ids and hashes only, never
the removed content, and normal queries must stop returning the content immediately.
A backup manifest records the database revision, every blob hash and its expiry so a
restore can be verified without trusting the client.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, JsonValue, UtcDateTime

DELETION_STATUS = "('REQUESTED','COMPLETED','PARTIAL','FAILED')"
BACKUP_STATUS = "('CREATING','VERIFYING','VERIFIED','FAILED','EXPIRED','DELETED')"


class DeletionRecord(Base):
    __tablename__ = "deletion_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=True
    )
    affected_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    backup_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    tombstone_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(f"status IN {DELETION_STATUS}", name="deletion_status"),
        Index("ix_deletions_requested", "requested_at"),
        Index("ix_deletions_artifact", "artifact_id"),
    )


class BackupManifest(Base):
    __tablename__ = "backup_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    db_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    blob_hashes: Mapped[list[dict]] = mapped_column(JsonValue, nullable=False)
    blob_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blob_hashes_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    deletion_tombstone_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    unknown_model_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    secret_free: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(f"status IN {BACKUP_STATUS}", name="backup_status"),
        UniqueConstraint("manifest_hash", name="uq_backup_manifest_hash"),
        Index("ix_backups_expires", "expires_at"),
    )


class MaintenanceState(Base):
    """Single-row maintenance state machine: NORMAL → ENTERING → MAINTENANCE → EXITING."""

    __tablename__ = "maintenance_state"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="maintenance_singleton"),
        CheckConstraint(
            "mode IN ('NORMAL','ENTERING','MAINTENANCE','EXITING')", name="maintenance_mode"
        ),
    )


__all__ = ["BackupManifest", "DeletionRecord", "MaintenanceState", "RestoreRecord"]


RESTORE_STATUS = (
    "('VALIDATING','APPLYING_TOMBSTONES','RESTORING','VERIFYING',"
    "'COMPLETED','FAILED','ROLLED_BACK')"
)


class RestoreRecord(Base):
    """A restore attempt. It is a record of what happened, not a request to retry."""

    __tablename__ = "restore_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    backup_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backup_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    stages: Mapped[list[dict]] = mapped_column(JsonValue, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_restore: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(f"status IN {RESTORE_STATUS}", name="restore_status"),
        Index("ix_restores_backup", "backup_id"),
    )
