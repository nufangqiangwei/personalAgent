"""Managed writes, immutable blobs and artifacts.

* ``SideEffectOperation`` is keyed by ``(root_run_id, logical_effect_key)`` so the
  same logical effect is checked across branches; an attempt id is *not* an
  idempotency key.
* ``Blob`` stores a managed relative path only — never an absolute client path.
* ``Artifact`` exposes AV ailable/EXPIRED/DELETED/INTEGRITY_ERROR independently
  from a run's status.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime


class SideEffectOperation(Base):
    __tablename__ = "side_effect_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    logical_effect_key: Mapped[str] = mapped_column(String(200), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    expected_before_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    intended_after_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    intent_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    verification_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "root_run_id", "logical_effect_key", name="uq_operations_root_logical_effect"
        ),
        CheckConstraint(
            "status IN ('INTENT','NOT_APPLIED','APPLIED','PARTIAL','UNKNOWN','CONFLICT')",
            name="side_effect_status",
        ),
        Index("ix_operations_root_logical", "root_run_id", "logical_effect_key"),
    )


class Blob(Base):
    __tablename__ = "blobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    managed_relative_path: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (Index("ix_blobs_expires_at", "expires_at"),)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workflow_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    blob_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    structure: Mapped[str] = mapped_column(String(32), nullable=False)
    completeness: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    document_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    access_state: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("format = 'markdown'", name="artifact_markdown_only"),
        CheckConstraint(
            "access_state IN ('AVAILABLE','EXPIRED','DELETED','INTEGRITY_ERROR')",
            name="artifact_access_state",
        ),
        CheckConstraint(
            "completeness IN ('COMPLETE','PARTIAL','NO_QUALIFIED_RESULT')",
            name="artifact_completeness",
        ),
        CheckConstraint(
            "structure IN ('report','checklist','comparison')", name="artifact_structure"
        ),
        Index("ix_artifacts_run", "run_id"),
    )
