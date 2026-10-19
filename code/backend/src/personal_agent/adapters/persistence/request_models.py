"""Idempotency records for mutating requests.

A key is unique per ``(workspace, route)``. Repeating the same canonical request
returns the original response; the same key with a different request hash is a
conflict. Records live as long as the resource they protect.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, JsonObject, UtcDateTime


class RequestRecord(Base):
    __tablename__ = "request_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    route: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JsonObject, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "route", "idempotency_key", name="uq_request_records_scope_key"
        ),
        Index("ix_request_records_key", "idempotency_key"),
    )
