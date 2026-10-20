"""deletion records and backup manifests

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 08:40:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent.adapters.persistence.base  # noqa: F401  (custom column types)

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deletion_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("affected_ids", personal_agent.adapters.persistence.base.JsonValue(), nullable=False),
        sa.Column("backup_ids", personal_agent.adapters.persistence.base.JsonValue(), nullable=False),
        sa.Column("tombstone_hash", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=False),
        sa.Column("completed_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED','COMPLETED','PARTIAL','FAILED')",
            name=op.f("ck_deletion_records_deletion_status"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"],
            name=op.f("fk_deletion_records_artifact_id_artifacts"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"],
            name=op.f("fk_deletion_records_run_id_workflow_runs"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deletion_records")),
    )
    with op.batch_alter_table("deletion_records", schema=None) as batch_op:
        batch_op.create_index("ix_deletions_artifact", ["artifact_id"], unique=False)
        batch_op.create_index("ix_deletions_requested", ["requested_at"], unique=False)

    op.create_table(
        "backup_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("db_revision", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=80), nullable=False),
        sa.Column("blob_hashes", personal_agent.adapters.persistence.base.JsonValue(), nullable=False),
        sa.Column("blob_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("blob_hashes_digest", sa.String(length=80), nullable=False),
        sa.Column("deletion_tombstone_hash", sa.String(length=80), nullable=False),
        sa.Column("unknown_model_request", sa.Boolean(), nullable=False),
        sa.Column("verification", personal_agent.adapters.persistence.base.JsonObject(), nullable=False),
        sa.Column("usable", sa.Boolean(), nullable=False),
        sa.Column("secret_free", sa.Boolean(), nullable=False),
        sa.Column("created_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=False),
        sa.Column("verified_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.Column("expires_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=False),
        sa.Column("deleted_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('CREATING','VERIFYING','VERIFIED','FAILED','EXPIRED','DELETED')",
            name=op.f("ck_backup_manifests_backup_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_manifests")),
        sa.UniqueConstraint("manifest_hash", name="uq_backup_manifest_hash"),
    )
    with op.batch_alter_table("backup_manifests", schema=None) as batch_op:
        batch_op.create_index("ix_backups_expires", ["expires_at"], unique=False)

    op.create_table(
        "restore_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("backup_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stages", personal_agent.adapters.persistence.base.JsonValue(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("post_restore", personal_agent.adapters.persistence.base.JsonObject(), nullable=False),
        sa.Column("started_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=False),
        sa.Column("completed_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('VALIDATING','APPLYING_TOMBSTONES','RESTORING','VERIFYING',"
            "'COMPLETED','FAILED','ROLLED_BACK')",
            name=op.f("ck_restore_records_restore_status"),
        ),
        sa.ForeignKeyConstraint(
            ["backup_id"], ["backup_manifests.id"],
            name=op.f("fk_restore_records_backup_id_backup_manifests"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_restore_records")),
    )
    with op.batch_alter_table("restore_records", schema=None) as batch_op:
        batch_op.create_index("ix_restores_backup", ["backup_id"], unique=False)

    op.create_table(
        "maintenance_state",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.Column("entered_at", personal_agent.adapters.persistence.base.UtcDateTime(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("singleton_id = 1", name=op.f("ck_maintenance_state_maintenance_singleton")),
        sa.CheckConstraint(
            "mode IN ('NORMAL','ENTERING','MAINTENANCE','EXITING')",
            name=op.f("ck_maintenance_state_maintenance_mode"),
        ),
        sa.PrimaryKeyConstraint("singleton_id", name=op.f("pk_maintenance_state")),
    )


def downgrade() -> None:
    op.drop_table("maintenance_state")
    with op.batch_alter_table("restore_records", schema=None) as batch_op:
        batch_op.drop_index("ix_restores_backup")
    op.drop_table("restore_records")
    with op.batch_alter_table("backup_manifests", schema=None) as batch_op:
        batch_op.drop_index("ix_backups_expires")
    op.drop_table("backup_manifests")
    with op.batch_alter_table("deletion_records", schema=None) as batch_op:
        batch_op.drop_index("ix_deletions_requested")
        batch_op.drop_index("ix_deletions_artifact")
    op.drop_table("deletion_records")
