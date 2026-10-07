"""durable realtime session authority for protocol 6 workers

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "realtime_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("auth_session_id", sa.Uuid()),
        sa.Column("browser_owner_id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("effective_params", postgresql.JSONB()),
        sa.Column("desired_params", postgresql.JSONB()),
        sa.Column("params_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("desired_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("control_generation", sa.BigInteger(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger()),
        sa.Column("input_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("original_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_reason", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("last_input_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "state IN ('queued','assigning','live','idle','ending','ended')",
            name="realtime_sessions_state",
        ),
        sa.CheckConstraint("control_generation > 0", name="realtime_sessions_generation"),
    )
    op.create_index("realtime_sessions_user_state", "realtime_sessions", ["user_id", "state"])
    op.create_table(
        "realtime_session_attempts",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("control_generation", sa.BigInteger(), primary_key=True),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("incarnation", sa.Uuid(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("gpu_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("frames", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("report_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("range_id", sa.Uuid()),
        sa.Column("accounted_gpu_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accounted_frames", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accounted_duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("control_generation > 0", name="realtime_attempts_generation"),
        sa.CheckConstraint("gpu_ms >= 0 AND frames >= 0 AND duration_ms >= 0",
                           name="realtime_attempts_measurements"),
    )
    op.drop_constraint("physical_ranges_kind", "worker_physical_ranges", type_="check")
    op.drop_constraint("physical_ranges_identity", "worker_physical_ranges", type_="check")
    op.create_check_constraint(
        "physical_ranges_kind",
        "worker_physical_ranges",
        "kind IN ('job','session')",
    )
    op.create_check_constraint(
        "physical_ranges_identity",
        "worker_physical_ranges",
        "(kind = 'job' AND attempt_id IS NULL AND control_generation IS NULL "
        "AND dispatch_sequence IS NOT NULL AND dispatch_sequence > 0) OR "
        "(kind = 'session' AND attempt_id IS NOT NULL AND control_generation IS NOT NULL "
        "AND control_generation > 0 AND dispatch_sequence IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("physical_ranges_identity", "worker_physical_ranges", type_="check")
    op.drop_constraint("physical_ranges_kind", "worker_physical_ranges", type_="check")
    op.create_check_constraint(
        "physical_ranges_kind",
        "worker_physical_ranges",
        "kind IN ('job')",
    )
    op.create_check_constraint(
        "physical_ranges_identity",
        "worker_physical_ranges",
        "attempt_id IS NULL AND control_generation IS NULL "
        "AND dispatch_sequence IS NOT NULL AND dispatch_sequence > 0",
    )
    op.drop_table("realtime_session_attempts")
    op.drop_index("realtime_sessions_user_state", table_name="realtime_sessions")
    op.drop_table("realtime_sessions")
