"""Persisted, append-only analysis operations for requirement chat.

An operation records one analysis attempt. It is visible without re-triggering
work: ``GET /operations/{id}`` only reads state. A busy model records the message
and leaves the operation ``WAITING_MODEL`` instead of silently dropping it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime


class AnalysisOperation(Base):
    __tablename__ = "analysis_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("requirement_drafts.id", ondelete="RESTRICT"), nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JsonObject, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','WAITING_MODEL','SUCCEEDED','FAILED','UNKNOWN')",
            name="analysis_operation_status",
        ),
    )
