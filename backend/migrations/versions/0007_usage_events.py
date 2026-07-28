"""usage events and telemetry state

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("category_score", sa.Float(), nullable=True),
        sa.Column("gpu_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("frames", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    # Serves daily telemetry range scans and aggregate grouping.
    op.create_index("usage_events_created_at", "usage_events", ["created_at"])
    op.create_table(
        "telemetry_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("install_id", sa.Uuid(), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("last_report_day", sa.Date(), nullable=True),
    )
    op.execute("INSERT INTO telemetry_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("telemetry_state")
    op.drop_index("usage_events_created_at", table_name="usage_events")
    op.drop_table("usage_events")
