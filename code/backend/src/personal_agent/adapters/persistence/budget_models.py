"""Budget ledgers, append-only entries and the single model request slot.

Constraints from ``data-model.md``:

* exactly one of ``root_run_id`` / ``draft_id`` is set, each unique;
* entries are append-only and deduplicated by ``(ledger, operation, kind)``;
* ``GRANT`` only comes from the explicit top-up endpoint and never resets
  ``consumed`` or retry limits;
* a model request occupies the slot until a *verified* terminal state; an
  ``UNKNOWN`` request keeps both the slot and its reservation across restarts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime


class BudgetLedger(Base):
    __tablename__ = "budget_ledgers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    limits_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    consumed_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    reserved_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    active_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "(root_run_id IS NOT NULL AND draft_id IS NULL) OR "
            "(root_run_id IS NULL AND draft_id IS NOT NULL)",
            name="ledger_single_owner",
        ),
        Index("ix_budget_ledgers_root", "root_run_id"),
        Index("ix_budget_ledgers_draft", "draft_id"),
    )


class BudgetEntry(Base):
    __tablename__ = "budget_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ledger_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("budget_ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "ledger_id", "operation_id", "kind", name="uq_budget_entries_ledger_op_kind"
        ),
        CheckConstraint(
            "kind IN ('RESERVE','SETTLE','RELEASE','GRANT')", name="budget_entry_kind"
        ),
    )


class ModelRequest(Base):
    __tablename__ = "model_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reservation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completion_evidence_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JsonObject, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVED','IN_FLIGHT','COMPLETED','CANCELLED_CONFIRMED','UNKNOWN')",
            name="model_request_status",
        ),
        CheckConstraint(
            "owner_type IN ('RUN','DRAFT')",
            name="model_request_owner",
        ),
        Index("ix_model_requests_status", "status"),
        Index("ix_model_requests_owner", "owner_type", "owner_id"),
        Index("uq_model_requests_reservation_id", "reservation_id", unique=True),
        Index(
            "uq_model_requests_active",
            text("(1)"),
            unique=True,
            sqlite_where=text("status IN ('RESERVED','IN_FLIGHT','UNKNOWN')"),
        ),
    )
