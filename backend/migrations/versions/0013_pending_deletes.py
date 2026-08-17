"""one row per blob the terminal paths could not delete

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_deletes",
        sa.Column("storage_key", sa.Text(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The sweep reads by due time every five minutes and the table can hold
    # thousands of rows while a bucket policy is broken, which is exactly when
    # it runs hardest.
    op.create_index("pending_deletes_due", "pending_deletes", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("pending_deletes_due", table_name="pending_deletes")
    op.drop_table("pending_deletes")