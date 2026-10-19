"""Assistance requests and explicit user decisions.

An open assistance request is the only way a run may leave WAITING_USER; silence,
chat discussion or a timeout never resumes it. Budget top-up and "continue" are
separate explicit fields, and a client cannot declare a write as APPLIED.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonValue, UtcDateTime


class AssistanceRequest(Base):
    __tablename__ = "assistance_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_snapshot: Mapped[dict] = mapped_column(JsonValue, nullable=False)
    consumed_snapshot: Mapped[dict] = mapped_column(JsonValue, nullable=False)
    completed_steps: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    remaining_steps: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    observed_problem: Mapped[str] = mapped_column(Text, nullable=False)
    unknowns: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    attempted_remedies: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    #: The reviewed step's declared result contract; kept so a restart can still
    #: check the answer instead of trusting whatever the client sends.
    decision_schema_json: Mapped[dict | None] = mapped_column(JsonValue, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('BUDGET_LIMIT','WRITE_RECONCILIATION','HUMAN_REVIEW','RUNTIME_UNCERTAIN')",
            name="assistance_kind",
        ),
        CheckConstraint(
            "status IN ('OPEN','RESOLVED','CANCELLED')", name="assistance_status"
        ),
    )


class UserDecision(Base):
    __tablename__ = "user_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assistance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assistance_requests.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_increment: Mapped[dict | None] = mapped_column(JsonValue, nullable=True)
    continue_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: A structured human-review answer, validated against the request's schema.
    review_result_json: Mapped[dict | None] = mapped_column(JsonValue, nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('DISCUSS','GRANT_AND_CONTINUE','PAUSE','CANCEL',"
            "'CHANGE_REQUIREMENTS','RECONCILE')",
            name="user_decision_type",
        ),
    )
