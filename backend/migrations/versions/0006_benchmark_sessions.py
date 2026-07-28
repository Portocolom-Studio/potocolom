"""persist benchmark session history

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_vram_gb", sa.Float(), nullable=True),
        sa.Column("prompt_count", sa.Integer(), nullable=False),
        sa.Column("models", JSONB(), nullable=False),
        sa.Column("variants_per_prompt", sa.Integer(), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
    )
    # Serves the install-scoped newest-first session list.
    op.create_index(
        "benchmark_sessions_created",
        "benchmark_sessions",
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_table(
        "benchmark_measurements",
        sa.Column("session_id", sa.Uuid(),
                  sa.ForeignKey("benchmark_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("cell_key", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("model_load_ms", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("gpu_ms", sa.Integer(), nullable=True),
        sa.Column("wall_s", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id", "position"),
    )


def downgrade() -> None:
    op.drop_table("benchmark_measurements")
    op.drop_index("benchmark_sessions_created", table_name="benchmark_sessions")
    op.drop_table("benchmark_sessions")
