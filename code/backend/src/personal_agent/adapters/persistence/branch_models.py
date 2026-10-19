"""Branch proposals and reused results.

A branch is a new run that shares the parent's root and reuses only the results
that are still valid *before* the chosen start point. It is not a new attempt of
the parent, and the parent's history is append-only.

Statuses are separate from reasons: ``BLOCKED`` always carries why, and
``CONFIRMED`` requires both the new input and the final start point. A proposal and
its confirmation can create exactly one run.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonValue, UtcDateTime

BRANCH_STATUS = "('DRAFT','BLOCKED','READY','CONFIRMED')"


class BranchProposal(Base):
    __tablename__ = "branch_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    root_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fork_step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    changed_paths: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    input_reads_closure: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    affected_step_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    reusable_step_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    invalidated_artifact_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    blocked_reasons: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    report_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confirmation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("confirmations.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    created_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(f"status IN {BRANCH_STATUS}", name="branch_status"),
        Index("ix_branch_proposals_parent", "parent_run_id", "created_at"),
        Index("ix_branch_proposals_root", "root_run_id"),
    )


class ReusedResult(Base):
    """A result copied from an ancestor. It is not a new successful attempt."""

    __tablename__ = "reused_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "artifact_id", name="uq_reused_run_step_artifact"),
        Index("ix_reused_results_run", "run_id"),
    )
