"""index recent succeeded jobs per model for observed timings

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Serves the observed-timings refresh: newest succeeded jobs per model. The
    # partial predicate keeps the index to the rows that query looks at, and
    # jobs_user_created cannot answer it because it leads with user_id.
    op.create_index(
        "jobs_model_finished",
        "jobs",
        ["model_id", sa.text("finished_at DESC")],
        postgresql_where=sa.text("state = 'succeeded' AND gpu_ms IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("jobs_model_finished", table_name="jobs")
