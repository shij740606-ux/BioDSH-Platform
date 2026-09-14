"""Add leased job execution and result artifacts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0004"
down_revision: str | None = "20260914_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("assigned_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("exit_code", sa.Integer()))
        batch_op.add_column(
            sa.Column("result_manifest", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("error_message", sa.Text(), nullable=False, server_default="")
        )
        batch_op.create_index(
            "ix_jobs_status_priority_created", ["status", "priority", "created_at"]
        )
        batch_op.create_index("ix_jobs_lease_expires", ["lease_expires_at"])
    op.create_table(
        "job_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("storage_key", sa.String(700), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "relative_path", name="uq_job_artifacts_job_path"),
    )
    op.create_index("ix_job_artifacts_job_id", "job_artifacts", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_artifacts_job_id", table_name="job_artifacts")
    op.drop_table("job_artifacts")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_lease_expires")
        batch_op.drop_index("ix_jobs_status_priority_created")
        batch_op.drop_column("error_message")
        batch_op.drop_column("result_manifest")
        batch_op.drop_column("exit_code")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("finished_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("assigned_at")
        batch_op.drop_column("attempt")
        batch_op.drop_column("priority")
