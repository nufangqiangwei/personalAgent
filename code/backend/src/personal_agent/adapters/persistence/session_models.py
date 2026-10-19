"""Runtime session links.

``attempt_id`` or ``draft_id`` — exactly one. A provider session id may be absent
while creation is unfinished. The local link is persisted *before* a task is
submitted so a crash cannot lose the association, and creation is deduplicated by
``operation_id``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, UtcDateTime


class SessionLink(Base):
    __tablename__ = "session_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    adapter_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    sdk_version: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_version: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    store_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_session_links_operation"),
        CheckConstraint(
            "(attempt_id IS NOT NULL AND draft_id IS NULL) OR "
            "(attempt_id IS NULL AND draft_id IS NOT NULL)",
            name="session_link_single_owner",
        ),
        CheckConstraint(
            "status IN ('CREATING','READY','SUBMITTED','STOPPED','UNKNOWN','DISPOSED')",
            name="session_link_status",
        ),
    )
