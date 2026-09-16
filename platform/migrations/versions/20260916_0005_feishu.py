"""Feishu login, project node authorization and message outbox."""

import sqlalchemy as sa
from alembic import op

revision = "20260916_0005"
down_revision = "20260914_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_identities",
        sa.Column("provider", sa.String(24), primary_key=True),
        sa.Column("tenant_key", sa.String(120), primary_key=True),
        sa.Column("external_id", sa.String(120), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("display_name", sa.String(120), nullable=False),
    )
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table(
        "oauth_flows",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("poll_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE")),
    )
    op.create_index("ix_oauth_flows_expires_at", "oauth_flows", ["expires_at"])
    op.create_table(
        "project_nodes",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey("compute_nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "project_chats",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.String(120), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_key", name="uq_notification_event"),
    )
    op.create_index("ix_notification_outbox_project_id", "notification_outbox", ["project_id"])
    op.create_index("ix_notification_outbox_available_at", "notification_outbox", ["available_at"])


def downgrade():
    for table in [
        "notification_outbox",
        "project_chats",
        "project_nodes",
        "oauth_flows",
        "external_identities",
    ]:
        op.drop_table(table)
