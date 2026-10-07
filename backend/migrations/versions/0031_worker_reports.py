"""durable protocol 6 worker report receipts and physical work ranges

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_physical_ranges",
        sa.Column("range_id", sa.Uuid(), primary_key=True),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("incarnation", sa.Uuid(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid()),
        sa.Column("control_generation", sa.BigInteger()),
        sa.Column("dispatch_sequence", sa.BigInteger()),
        sa.Column("state", sa.Text(), nullable=False, server_default="outstanding"),
        sa.Column("report_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("report_floor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("report_hash", sa.String(64)),
        sa.Column("gpu_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("frames", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accounting_retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('job')", name="physical_ranges_kind"),
        sa.CheckConstraint(
            "attempt_id IS NULL AND control_generation IS NULL AND dispatch_sequence > 0",
            name="physical_ranges_identity",
        ),
        sa.CheckConstraint(
            "state IN ('outstanding','drained','measurement_expired')",
            name="physical_ranges_state",
        ),
        sa.CheckConstraint(
            "owner_epoch > 0 AND report_sequence >= 0 AND report_floor >= 0 "
            "AND gpu_ms >= 0 AND frames >= 0 AND duration_ms >= 0",
            name="physical_ranges_counters",
        ),
    )
    op.create_index(
        "physical_ranges_worker_outstanding", "worker_physical_ranges",
        ["worker_id", "state", "incarnation"],
    )
    op.create_index(
        "physical_ranges_subject", "worker_physical_ranges",
        ["kind", "subject_id", "worker_id"],
    )
    op.create_table(
        "worker_report_receipts",
        sa.Column("range_id", sa.Uuid(), sa.ForeignKey("worker_physical_ranges.range_id",
                                                       ondelete="CASCADE"), primary_key=True),
        sa.Column("report_sequence", sa.BigInteger(), primary_key=True),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("gpu_ms", sa.BigInteger(), nullable=False),
        sa.Column("frames", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("ack_body", sa.LargeBinary(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("report_sequence > 0", name="worker_reports_sequence"),
        sa.CheckConstraint("gpu_ms >= 0 AND frames >= 0 AND duration_ms >= 0",
                           name="worker_reports_counters"),
        sa.CheckConstraint("octet_length(ack_body) <= 16384", name="worker_reports_ack_size"),
    )
    op.create_index("worker_report_receipts_age", "worker_report_receipts", ["accepted_at"])
    op.add_column(
        "job_attempts",
        sa.Column("gpu_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "job_attempts",
        sa.Column("frames", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "job_attempts",
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "job_attempts",
        sa.Column("report_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("job_attempts", sa.Column("input_fetch_ms", sa.BigInteger()))
    op.add_column("job_attempts", sa.Column("load_ms", sa.BigInteger()))
    op.add_column("job_attempts", sa.Column("postprocess_ms", sa.BigInteger()))
    op.add_column("job_attempts", sa.Column("terminal_category", sa.Text()))
    op.add_column("job_attempts", sa.Column("terminal_category_score", sa.Float()))
    op.add_column("job_attempts", sa.Column("terminal_has_thumbnail", sa.Boolean()))
    op.add_column("job_attempts", sa.Column("terminal_failure_code", sa.Text()))
    op.add_column("job_attempts", sa.Column("terminal_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "job_attempts_measurements",
        "job_attempts",
        "gpu_ms >= 0 AND frames >= 0 AND duration_ms >= 0",
    )
    op.create_check_constraint(
        "job_attempts_phase_measurements",
        "job_attempts",
        "(input_fetch_ms IS NULL OR input_fetch_ms >= 0) AND "
        "(load_ms IS NULL OR load_ms >= 0) AND "
        "(postprocess_ms IS NULL OR postprocess_ms >= 0)",
    )
    op.create_check_constraint(
        "job_attempts_category_score",
        "job_attempts",
        "terminal_category_score IS NULL OR "
        "(terminal_category_score >= 0 AND terminal_category_score <= 1)",
    )
    op.drop_constraint("job_attempts_state", "job_attempts", type_="check")
    op.create_check_constraint(
        "job_attempts_state",
        "job_attempts",
        "state IN ('dispatching','completed','failed','cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("job_attempts_state", "job_attempts", type_="check")
    op.create_check_constraint(
        "job_attempts_state",
        "job_attempts",
        "state IN ('dispatching')",
    )
    op.drop_constraint("job_attempts_category_score", "job_attempts", type_="check")
    op.drop_constraint("job_attempts_phase_measurements", "job_attempts", type_="check")
    op.drop_constraint("job_attempts_measurements", "job_attempts", type_="check")
    op.drop_column("job_attempts", "terminal_at")
    op.drop_column("job_attempts", "terminal_failure_code")
    op.drop_column("job_attempts", "terminal_has_thumbnail")
    op.drop_column("job_attempts", "terminal_category_score")
    op.drop_column("job_attempts", "terminal_category")
    op.drop_column("job_attempts", "postprocess_ms")
    op.drop_column("job_attempts", "load_ms")
    op.drop_column("job_attempts", "input_fetch_ms")
    op.drop_column("job_attempts", "report_sequence")
    op.drop_column("job_attempts", "duration_ms")
    op.drop_column("job_attempts", "frames")
    op.drop_column("job_attempts", "gpu_ms")
    op.drop_index("worker_report_receipts_age", table_name="worker_report_receipts")
    op.drop_table("worker_report_receipts")
    op.drop_index("physical_ranges_subject", table_name="worker_physical_ranges")
    op.drop_index("physical_ranges_worker_outstanding", table_name="worker_physical_ranges")
    op.drop_table("worker_physical_ranges")
