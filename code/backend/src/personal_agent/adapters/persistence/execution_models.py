"""Execution core: runs, steps, attempts, the singleton slot and events.

Constraints from ``data-model.md``:

* root run has no parent and ``root_run_id == id``; a branch shares the root;
* ``phase1 mode`` is always ``FIXED``; version and confirmed input are immutable;
* ``completeness`` (COMPLETE/PARTIAL/NO_QUALIFIED_RESULT) is independent from
  ``SUCCEEDED`` so a partial delivery cannot masquerade as complete;
* ``UNIQUE(run_id, step_id)`` and ``UNIQUE(step_run_id, attempt_no)``;
* the execution slot is a singleton row (``singleton_id = 1``);
* events are append-only, deduplicated per owner sequence, with an optional
  provider key and no model reasoning.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime

FIXED_MODE = "FIXED"


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    fork_step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("confirmations.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    input_snapshot_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    runtime_fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    budget_policy_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    data_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=FIXED_MODE)
    completeness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("mode = 'FIXED'", name="phase1_mode_fixed"),
        CheckConstraint(
            "completeness IS NULL OR completeness IN "
            "('COMPLETE','PARTIAL','NO_QUALIFIED_RESULT')",
            name="completeness_enum",
        ),
        Index("ix_runs_root_created", "root_run_id", "created_at"),
    )


class StepRun(Base):
    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_transition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "step_id", name="uq_step_runs_run_step"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','WAITING_USER',"
            "'INTERRUPTED','CANCELLED','SKIPPED')",
            name="step_status_enum",
        ),
    )


class StepAttempt(Base):
    __tablename__ = "step_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    step_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("step_runs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JsonObject, nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    session_link_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint("step_run_id", "attempt_no", name="uq_attempts_step_attempt"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED','INTERRUPTED','CANCELLED','UNKNOWN')",
            name="attempt_status_enum",
        ),
    )


class ExecutionSlot(Base):
    """One row for the whole application: at most one active run branch."""

    __tablename__ = "execution_slot"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    process_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (CheckConstraint("singleton_id = 1", name="slot_singleton"),)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("requirement_drafts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workflow_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider_event_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_events_run_sequence", "run_id", "sequence", unique=True),
        Index("ix_events_draft_sequence", "draft_id", "sequence", unique=True),
        CheckConstraint(
            "(run_id IS NOT NULL AND draft_id IS NULL) OR "
            "(run_id IS NULL AND draft_id IS NOT NULL)",
            name="event_single_owner",
        ),
    )
