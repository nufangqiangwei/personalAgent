"""Event step/attempt foreign keys.

Events that name a step run or attempt must reference existing rows. The engine
persists the target row before the event, so insertion order is safe; orphans
block the migration until verified.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS: list[tuple[str, str]] = [
    (
        "SELECT COUNT(*) FROM execution_events e "
        "LEFT JOIN step_runs s ON e.step_run_id = s.id "
        "WHERE e.step_run_id IS NOT NULL AND s.id IS NULL",
        "an event references a missing step run",
    ),
    (
        "SELECT COUNT(*) FROM execution_events e "
        "LEFT JOIN step_attempts a ON e.attempt_id = a.id "
        "WHERE e.attempt_id IS NOT NULL AND a.id IS NULL",
        "an event references a missing attempt",
    ),
]


def upgrade() -> None:
    connection = op.get_bind()
    for query, message in _CHECKS:
        if connection.scalar(sa.text(query)):
            raise RuntimeError(message + "; verify the rows before migration")
    with op.batch_alter_table("execution_events") as batch:
        batch.create_foreign_key(
            "fk_execution_events_step_run", "step_runs",
            ["step_run_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_execution_events_attempt", "step_attempts",
            ["attempt_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_events") as batch:
        batch.drop_constraint("fk_execution_events_attempt", type_="foreignkey")
        batch.drop_constraint("fk_execution_events_step_run", type_="foreignkey")
