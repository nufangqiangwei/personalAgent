"""policy acknowledgements

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 08:55:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent.adapters.persistence.base  # noqa: F401  (custom column types)

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_acknowledgements",
        sa.Column("policy_hash", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("data_policy_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("budget_policy_acknowledged", sa.Boolean(), nullable=False),
        sa.Column(
            "confirmed_at",
            personal_agent.adapters.persistence.base.UtcDateTime(length=32),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_hash", name=op.f("pk_policy_acknowledgements")),
    )


def downgrade() -> None:
    op.drop_table("policy_acknowledgements")
