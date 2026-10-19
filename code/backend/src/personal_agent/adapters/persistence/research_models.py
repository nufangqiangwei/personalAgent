"""Research actions, sources and evidence.

Every defined direction must end with an action result or an explicit gap. Fetch
time is mandatory; an unknown publication time stays null instead of being
inferred from the fetch time. Content hashes deduplicate identical content without
erasing distinct fetch times.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonValue, UtcDateTime


class ResearchAction(Base):
    __tablename__ = "research_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    direction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(300), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    scope_hash: Mapped[str] = mapped_column(String(80), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "tool_name IN ('search.web','http.fetch','browser.read','content.extract','agent')",
            name="research_action_tool",
        ),
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    normalized_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    blob_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="AVAILABLE")
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "normalized_url",
            "content_hash",
            "fetched_at",
            name="uq_sources_fetch_identity",
        ),
        CheckConstraint(
            "availability IN ('AVAILABLE','DELETED','EXPIRED','SOURCE_UNAVAILABLE')",
            name="source_availability",
        ),
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="RESTRICT"), nullable=False
    )
    direction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    excerpt_refs: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conflicts: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    validity: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "classification IN ('FACT','OPINION','INFERENCE')", name="evidence_classification"
        ),
        CheckConstraint(
            "validity IN ('VERIFIED','UNVERIFIED','OUTDATED','CONFLICTED')",
            name="evidence_validity",
        ),
    )
