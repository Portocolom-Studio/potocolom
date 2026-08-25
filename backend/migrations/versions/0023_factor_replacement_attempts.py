"""how many times somebody has failed to prove the factor they are replacing

Revision ID: 0023
Revises: 0022

Replacing a factor asks for a code from the one being retired, and without a
count that ask is free to retry: the caller already holds the new secret, so
they can answer the new half correctly every time and grind the old half. The
counter is what makes the ask cost something.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("auth_factors", sa.Column(
        "replace_attempts", sa.SmallInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("auth_factors", "replace_attempts")
