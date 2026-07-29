"""persist job favorites

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True))
    # Serves GET /generations?starred=true newest-first for one account.
    op.create_index(
        "jobs_user_starred",
        "jobs",
        ["user_id", sa.text("starred_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("starred_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("jobs_user_starred", table_name="jobs")
    op.drop_column("jobs", "starred_at")
