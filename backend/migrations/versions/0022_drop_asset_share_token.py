"""drop the retired share column, one answer per question

Revision ID: 0022
Revises: 0021

Not reversible in the sense that matters: the downgrade recreates the column
so an older release starts, and whatever the column held is gone for good.
Nothing has written it since asset_shares landed in 0020, so on any install
that reached that release there is nothing to lose.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("assets", "share_token")


def downgrade() -> None:
    op.add_column("assets", sa.Column("share_token", sa.Text(), nullable=True))
