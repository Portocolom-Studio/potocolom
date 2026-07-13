"""users, models, jobs, assets: the M2 subset of docs/architecture.md

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("capabilities", JSONB(), nullable=False),
        sa.Column("parameters_schema", JSONB(), nullable=False),
        sa.Column("min_vram_gb", sa.Integer(), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_id", sa.Text(), sa.ForeignKey("models.id"), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("gpu_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
    )
    op.create_index("jobs_user_created", "jobs", ["user_id", "created_at"])
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("share_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("assets_job", "assets", ["job_id"])


def downgrade() -> None:
    op.drop_table("assets")
    op.drop_table("jobs")
    op.drop_table("models")
    op.drop_table("users")
