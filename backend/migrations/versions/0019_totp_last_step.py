"""the last time step a factor accepted

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RFC 6238 requires that a code accepted once is never accepted again. A
    # code stays valid for ninety seconds across the drift window, which is
    # long enough for whoever phished it to spend it.
    op.add_column("auth_factors", sa.Column("last_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("auth_factors", "last_step")
