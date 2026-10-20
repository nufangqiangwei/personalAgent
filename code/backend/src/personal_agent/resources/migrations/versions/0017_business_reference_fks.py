"""Business-reference foreign keys for runs and the execution slot.

Orphaned rows block migration until an operator verifies them; no reference is
silently nulled. SQLite needs a table rebuild for new constraints, so each change
uses batch mode.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_clean(connection: sa.Connection, query: str, message: str) -> None:
    if connection.scalar(sa.text(query)):
        raise RuntimeError(message)


def upgrade() -> None:
    connection = op.get_bind()
    _require_clean(
        connection,
        "SELECT COUNT(*) FROM workflow_runs r "
        "LEFT JOIN workflow_runs p ON r.parent_run_id = p.id "
        "WHERE r.parent_run_id IS NOT NULL AND p.id IS NULL",
        "orphaned parent_run_id values require verification before migration",
    )
    _require_clean(
        connection,
        "SELECT COUNT(*) FROM workflow_runs r "
        "LEFT JOIN workflow_runs p ON r.root_run_id = p.id WHERE p.id IS NULL",
        "orphaned root_run_id values require verification before migration",
    )
    _require_clean(
        connection,
        "SELECT COUNT(*) FROM execution_slot s "
        "LEFT JOIN workflow_runs r ON s.run_id = r.id "
        "WHERE s.run_id IS NOT NULL AND r.id IS NULL",
        "the execution slot references a missing run",
    )
    with op.batch_alter_table("workflow_runs") as batch:
        batch.create_foreign_key(
            "fk_workflow_runs_root_run", "workflow_runs",
            ["root_run_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_workflow_runs_parent_run", "workflow_runs",
            ["parent_run_id"], ["id"], ondelete="RESTRICT",
        )
    with op.batch_alter_table("execution_slot") as batch:
        batch.create_foreign_key(
            "fk_execution_slot_run", "workflow_runs",
            ["run_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_slot") as batch:
        batch.drop_constraint("fk_execution_slot_run", type_="foreignkey")
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_constraint("fk_workflow_runs_parent_run", type_="foreignkey")
        batch.drop_constraint("fk_workflow_runs_root_run", type_="foreignkey")
