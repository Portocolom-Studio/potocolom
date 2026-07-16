"""gpu_samples, five-minute rollups, job dispatch timestamps

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpu_samples",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("util_pct", sa.SmallInteger(), nullable=True),
        sa.Column("vram_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("vram_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("power_w", sa.Float(), nullable=True),
        sa.Column("loaded_models", JSONB(), nullable=True),
    )
    op.create_index("gpu_samples_sampled_at", "gpu_samples", ["sampled_at"])
    op.create_index("gpu_samples_worker_sampled", "gpu_samples", ["worker_id", "sampled_at"])

    op.create_table(
        "gpu_sample_rollups",
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("util_mean", sa.Float(), nullable=True),
        sa.Column("util_min", sa.SmallInteger(), nullable=True),
        sa.Column("util_max", sa.SmallInteger(), nullable=True),
        sa.Column("vram_used_pct_mean", sa.Float(), nullable=True),
        sa.Column("vram_used_pct_min", sa.SmallInteger(), nullable=True),
        sa.Column("vram_used_pct_max", sa.SmallInteger(), nullable=True),
        sa.Column("temperature_mean", sa.Float(), nullable=True),
        sa.Column("power_mean", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("worker_id", "bucket_start"),
    )
    op.create_index("gpu_sample_rollups_bucket", "gpu_sample_rollups", ["bucket_start"])

    op.add_column("jobs", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "finished_at")
    op.drop_column("jobs", "dispatched_at")
    op.drop_table("gpu_sample_rollups")
    op.drop_index("gpu_samples_worker_sampled", table_name="gpu_samples")
    op.drop_index("gpu_samples_sampled_at", table_name="gpu_samples")
    op.drop_table("gpu_samples")
