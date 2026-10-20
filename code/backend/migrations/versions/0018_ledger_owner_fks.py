"""Budget ledger owner foreign keys.

A ledger names either a run root or a draft; both must exist. Orphaned rows block
migration until verified; nothing is nulled automatically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_clean(connection: sa.Connection, query: str, message: str) -> None:
    if connection.scalar(sa.text(query)):
        raise RuntimeError(message)


def upgrade() -> None:
    connection = op.get_bind()
    _require_clean(
        connection,
        "SELECT COUNT(*) FROM budget_ledgers l "
        "LEFT JOIN workflow_runs r ON l.root_run_id = r.id "
        "WHERE l.root_run_id IS NOT NULL AND r.id IS NULL",
        "a run budget ledger references a missing run",
    )
    _require_clean(
        connection,
        "SELECT COUNT(*) FROM budget_ledgers l "
        "LEFT JOIN requirement_drafts d ON l.draft_id = d.id "
        "WHERE l.draft_id IS NOT NULL AND d.id IS NULL",
        "a draft budget ledger references a missing draft",
    )
    with op.batch_alter_table("budget_ledgers") as batch:
        batch.create_foreign_key(
            "fk_budget_ledgers_root_run", "workflow_runs",
            ["root_run_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_budget_ledgers_draft", "requirement_drafts",
            ["draft_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("budget_ledgers") as batch:
        batch.drop_constraint("fk_budget_ledgers_draft", type_="foreignkey")
        batch.drop_constraint("fk_budget_ledgers_root_run", type_="foreignkey")
