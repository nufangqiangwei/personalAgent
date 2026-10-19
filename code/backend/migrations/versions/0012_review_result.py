"""human review result contract

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20 08:15:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent.adapters.persistence.base  # noqa: F401  (custom column types)

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("assistance_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "decision_schema_json",
                personal_agent.adapters.persistence.base.JsonValue(),
                nullable=True,
            )
        )
    with op.batch_alter_table("user_decisions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "review_result_json",
                personal_agent.adapters.persistence.base.JsonValue(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_decisions", schema=None) as batch_op:
        batch_op.drop_column("review_result_json")
    with op.batch_alter_table("assistance_requests", schema=None) as batch_op:
        batch_op.drop_column("decision_schema_json")
