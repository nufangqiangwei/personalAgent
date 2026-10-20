"""Policy acknowledgements recorded by the first-run setup.

Acknowledging the policy records a fact; it does not by itself mark the application as
configured, which is derived from migrations, the data directory and the runtime.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.adapters.persistence.base import Base, UtcDateTime


class PolicyAcknowledgement(Base):
    __tablename__ = "policy_acknowledgements"

    policy_hash: Mapped[str] = mapped_column(String(80), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    data_policy_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    budget_policy_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = ["PolicyAcknowledgement"]
