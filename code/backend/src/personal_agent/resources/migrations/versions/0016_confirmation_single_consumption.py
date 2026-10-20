"""One confirmation per draft revision and action.

A double-confirm (two requests racing on the same revision) must not create two
executions. Existing duplicates block migration until reviewed; no row is deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    duplicates = op.get_bind().scalar(sa.text(
        "SELECT COUNT(*) FROM ("
        "SELECT draft_id, draft_revision, action FROM confirmations "
        "GROUP BY draft_id, draft_revision, action HAVING COUNT(*) > 1"
        ")"
    ))
    if duplicates:
        raise RuntimeError(
            "duplicate confirmations require review before enforcing single consumption"
        )
    op.create_index(
        "uq_confirmations_draft_revision_action",
        "confirmations",
        ["draft_id", "draft_revision", "action"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_confirmations_draft_revision_action", table_name="confirmations"
    )
