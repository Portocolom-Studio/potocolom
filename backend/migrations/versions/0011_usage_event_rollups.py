"""daily per-user usage event rollups

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_event_rollups",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False),
        sa.Column("category_score_sum", sa.Float(), nullable=True),
        sa.Column("category_score_count", sa.BigInteger(), nullable=False),
        sa.Column("gpu_ms_sum", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms_sum", sa.BigInteger(), nullable=True),
        sa.Column("frames_sum", sa.BigInteger(), nullable=True),
    )
    # Serves idempotent rollup writes and user-scoped cohort/GDPR reads.
    op.create_index(
        "usage_event_rollups_key",
        "usage_event_rollups",
        [
            "user_id",
            "bucket_date",
            "kind",
            "action",
            "model_id",
            sa.text("COALESCE(tier, '')"),
            "category",
        ],
        unique=True,
    )
    # Serves period scans for cohort and admin usage aggregation.
    op.create_index(
        "usage_event_rollups_bucket_date",
        "usage_event_rollups",
        ["bucket_date"],
    )


def downgrade() -> None:
    op.drop_table("usage_event_rollups")
