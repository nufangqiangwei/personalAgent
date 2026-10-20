"""Allow a historical definition to be saved again as a new version.

A -> B -> A is a real save with its own version number and summary. The content
unique constraint made that impossible; version numbers stay unique.
Downgrade requires removing duplicate content rows first.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_versions") as batch:
        batch.drop_constraint("uq_workflow_versions_content", type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("workflow_versions") as batch:
        batch.create_unique_constraint(
            "uq_workflow_versions_content", ["workflow_id", "definition_hash"]
        )
