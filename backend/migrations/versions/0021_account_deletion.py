"""where a restore puts an account back, and when it asked to go

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One level only: an account suspended when it asked to be deleted comes
    # back suspended, and the state before that one is nobody's business.
    op.add_column("users", sa.Column("prior_state", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("deletion_requested_at",
                                     sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deletion_requested_at")
    op.drop_column("users", "prior_state")
