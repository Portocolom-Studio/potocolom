"""job pipeline phase timings

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("input_fetch_ms", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("load_ms", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("postprocess_ms", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("failure_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "failure_reason")
    op.drop_column("jobs", "postprocess_ms")
    op.drop_column("jobs", "load_ms")
    op.drop_column("jobs", "input_fetch_ms")
