"""a link that shows one picture to whoever holds it

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_shares",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="CASCADE"),
                  nullable=False),
        # Only the hash. The token lives in the fragment the owner copied.
        sa.Column("token_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One active share per asset, so revoking the link somebody can see cannot
    # leave an older one alive behind it.
    op.create_index("asset_shares_one_active", "asset_shares", ["asset_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"))


def downgrade() -> None:
    op.drop_index("asset_shares_one_active", table_name="asset_shares")
    op.drop_table("asset_shares")
