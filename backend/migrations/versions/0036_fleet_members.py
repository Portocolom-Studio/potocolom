"""Fleet members: one join token per GPU machine, spent for that machine's credential

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fleet_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("worker_id", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False),
        sa.Column("join_token_hash", sa.LargeBinary(), unique=True),
        sa.Column("join_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credential_hash", sa.LargeBinary(), unique=True),
        sa.Column("base_url", sa.Text()),
        sa.Column("enrolled_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("device IN ('cuda', 'rocm')", name="fleet_members_device"),
    )


def downgrade() -> None:
    op.drop_table("fleet_members")
