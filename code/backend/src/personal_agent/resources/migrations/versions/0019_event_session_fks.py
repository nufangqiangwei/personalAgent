"""Session-link business-reference foreign keys.

A session link names a run, an attempt or a draft; every listed reference must
exist. Orphans block migration until verified; nothing is nulled automatically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS: list[tuple[str, str]] = [
    (
        "SELECT COUNT(*) FROM session_links l "
        "LEFT JOIN workflow_runs r ON l.run_id = r.id "
        "WHERE l.run_id IS NOT NULL AND r.id IS NULL",
        "a session link references a missing run",
    ),
    (
        "SELECT COUNT(*) FROM session_links l "
        "LEFT JOIN step_attempts a ON l.attempt_id = a.id "
        "WHERE l.attempt_id IS NOT NULL AND a.id IS NULL",
        "a session link references a missing attempt",
    ),
    (
        "SELECT COUNT(*) FROM session_links l "
        "LEFT JOIN requirement_drafts d ON l.draft_id = d.id "
        "WHERE l.draft_id IS NOT NULL AND d.id IS NULL",
        "a session link references a missing draft",
    ),
]


def upgrade() -> None:
    connection = op.get_bind()
    for query, message in _CHECKS:
        if connection.scalar(sa.text(query)):
            raise RuntimeError(message + "; verify the rows before migration")
    with op.batch_alter_table("session_links") as batch:
        batch.create_foreign_key(
            "fk_session_links_run", "workflow_runs",
            ["run_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_session_links_attempt", "step_attempts",
            ["attempt_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_session_links_draft", "requirement_drafts",
            ["draft_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("session_links") as batch:
        batch.drop_constraint("fk_session_links_draft", type_="foreignkey")
        batch.drop_constraint("fk_session_links_attempt", type_="foreignkey")
        batch.drop_constraint("fk_session_links_run", type_="foreignkey")
