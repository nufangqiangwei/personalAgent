"""Workflow and immutable version persistence.

Constraints: ``UNIQUE(workflow_id, version_no)``; content is immutable; every save
with changed content creates a new version; a draft is saved but never becomes the
latest executable version; disabling is recorded as a state fact, not a delete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    latest_executable_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    saved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    change_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_report_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("workflow_id", "version_no", name="uq_workflow_versions_number"),
        CheckConstraint("version_no > 0", name="workflow_version_positive"),
    )
