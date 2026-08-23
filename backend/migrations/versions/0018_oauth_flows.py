"""one redirect to a provider and back

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_flows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Only the hash, so a row cannot be turned back into a usable state.
        sa.Column("state_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("verifier", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        # Null means a sign-in attempt; set means linking to that account.
        sa.Column("link_user_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    # The sweep that clears expired flows reads by time.
    op.create_index("oauth_flows_expires", "oauth_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_index("oauth_flows_expires", table_name="oauth_flows")
    op.drop_table("oauth_flows")
