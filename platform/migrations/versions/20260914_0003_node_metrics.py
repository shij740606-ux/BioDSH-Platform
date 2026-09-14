"""Add per-node credentials and resource metric history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0003"
down_revision: str | None = "20260914_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("compute_nodes") as batch_op:
        batch_op.add_column(sa.Column("token_hash", sa.String(64)))
        batch_op.add_column(
            sa.Column("hostname", sa.String(255), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("operating_system", sa.String(120), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("agent_version", sa.String(40), nullable=False, server_default="")
        )
        batch_op.create_unique_constraint("uq_compute_nodes_token_hash", ["token_hash"])
    op.create_table(
        "node_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("load_one", sa.Float()),
        sa.Column("memory_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("memory_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("memory_percent", sa.Float(), nullable=False),
        sa.Column("disk_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("disk_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("disk_percent", sa.Float(), nullable=False),
        sa.Column("network_rx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("network_tx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("running_jobs", sa.Integer(), nullable=False),
        sa.Column("gpus", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["compute_nodes.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_node_metrics_node_collected", "node_metrics", ["node_id", "collected_at"])
    op.create_index("ix_node_metrics_collected", "node_metrics", ["collected_at"])


def downgrade() -> None:
    op.drop_index("ix_node_metrics_collected", table_name="node_metrics")
    op.drop_index("ix_node_metrics_node_collected", table_name="node_metrics")
    op.drop_table("node_metrics")
    with op.batch_alter_table("compute_nodes") as batch_op:
        batch_op.drop_constraint("uq_compute_nodes_token_hash", type_="unique")
        batch_op.drop_column("agent_version")
        batch_op.drop_column("operating_system")
        batch_op.drop_column("hostname")
        batch_op.drop_column("token_hash")
