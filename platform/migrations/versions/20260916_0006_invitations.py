"""User profiles and project invitations."""

import sqlalchemy as sa
from alembic import op

revision = "20260916_0006"
down_revision = "20260916_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("display_name", sa.String(120), nullable=False, server_default="")
    )
    op.add_column(
        "users", sa.Column("avatar_url", sa.String(1000), nullable=False, server_default="")
    )
    op.add_column("oauth_flows", sa.Column("invitation_hash", sa.String(64)))
    op.create_table(
        "project_invitations",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remaining_uses", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_project_invitations_project_id", "project_invitations", ["project_id"])


def downgrade():
    op.drop_table("project_invitations")
    with op.batch_alter_table("oauth_flows") as batch:
        batch.drop_column("invitation_hash")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("avatar_url")
        batch.drop_column("display_name")
