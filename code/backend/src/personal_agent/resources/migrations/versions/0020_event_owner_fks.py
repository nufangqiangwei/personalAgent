"""Event owner foreign keys for drafts and workflow versions.

The step/attempt references are not constrained yet: events may be written before
the referenced row is flushed, and the models have no ORM relationship to order
those inserts. Draft and version references are validated here instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS: list[tuple[str, str]] = [
    (
        "SELECT COUNT(*) FROM execution_events e "
        "LEFT JOIN requirement_drafts d ON e.draft_id = d.id "
        "WHERE e.draft_id IS NOT NULL AND d.id IS NULL",
        "an event references a missing draft",
    ),
    (
        "SELECT COUNT(*) FROM execution_events e "
        "LEFT JOIN workflow_versions v ON e.workflow_version_id = v.id "
        "WHERE e.workflow_version_id IS NOT NULL AND v.id IS NULL",
        "an event references a missing workflow version",
    ),
]


def upgrade() -> None:
    connection = op.get_bind()
    for query, message in _CHECKS:
        if connection.scalar(sa.text(query)):
            raise RuntimeError(message + "; verify the rows before migration")
    with op.batch_alter_table("execution_events") as batch:
        batch.create_foreign_key(
            "fk_execution_events_draft", "requirement_drafts",
            ["draft_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_execution_events_version", "workflow_versions",
            ["workflow_version_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_events") as batch:
        batch.drop_constraint("fk_execution_events_version", type_="foreignkey")
        batch.drop_constraint("fk_execution_events_draft", type_="foreignkey")
