"""Requirement drafts, chat messages, suggestions and immutable confirmations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, JsonValue, UtcDateTime


class RequirementDraft(Base):
    __tablename__ = "requirement_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    selected_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    missing_fields_json: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    conflicts_json: Mapped[list[dict]] = mapped_column(JsonValue, nullable=False)
    field_values_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    preference_order_json: Mapped[list[str]] = mapped_column(JsonValue, nullable=False)
    output_spec_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)
    summary_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_hash: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    source_draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('COLLECTING','READY','CONFIRMED','STALE')", name="draft_status_enum"
        ),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("requirement_drafts.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("draft_id", "sequence", name="uq_messages_draft_sequence"),
        CheckConstraint("role IN ('user','assistant','system')", name="message_role_enum"),
    )


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("requirement_drafts.id", ondelete="RESTRICT"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    value_json: Mapped[object] = mapped_column(JsonValue, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    accepted_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED')", name="suggestion_status_enum"
        ),
    )


class Confirmation(Base):
    __tablename__ = "confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("requirement_drafts.id", ondelete="RESTRICT"), nullable=False
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_versions.id", ondelete="RESTRICT"), nullable=False
    )
    summary_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    budget_policy_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    data_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    field_snapshot_json: Mapped[dict] = mapped_column(JsonObject, nullable=False)

    __table_args__ = (
        CheckConstraint("action IN ('START','BRANCH')", name="confirmation_action_enum"),
    )
