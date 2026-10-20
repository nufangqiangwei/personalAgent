"""Enforce the single non-terminal model request across connections.

Existing ambiguous requests deliberately block migration: an operator must verify
them; migration must never discard evidence to make the index fit. Restore a
verified pre-migration backup to roll back production data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    active = op.get_bind().scalar(sa.text(
        "SELECT COUNT(*) FROM model_requests "
        "WHERE status IN ('RESERVED','IN_FLIGHT','UNKNOWN')"
    ))
    if active and active > 1:
        raise RuntimeError(
            "multiple unresolved model requests require verified reconciliation before migration"
        )
    op.add_column("model_requests", sa.Column("epoch", sa.Integer(), server_default="1", nullable=False))
    op.add_column("model_requests", sa.Column("reservation_id", sa.String(128), nullable=True))
    op.create_index("uq_model_requests_reservation_id", "model_requests", ["reservation_id"], unique=True)
    op.execute(sa.text(
        "CREATE UNIQUE INDEX uq_model_requests_active ON model_requests ((1)) "
        "WHERE status IN ('RESERVED','IN_FLIGHT','UNKNOWN')"
    ))


def downgrade() -> None:
    op.drop_index("uq_model_requests_active", table_name="model_requests")
    op.drop_index("uq_model_requests_reservation_id", table_name="model_requests")
    op.drop_column("model_requests", "reservation_id")
    op.drop_column("model_requests", "epoch")
