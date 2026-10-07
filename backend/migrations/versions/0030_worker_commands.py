"""durable encrypted worker command journal and job dispatch attempts

Revision ID: 0030
Revises: 0029

Protocol 6 job commands are committed in worker_commands with matching
job_attempts rows and jobs.current_dispatch_sequence.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_commands",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("incarnation", sa.Uuid(), primary_key=True),
        sa.Column("command_sequence", sa.BigInteger(), primary_key=True),
        sa.Column("command_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("key_version", sa.Integer()),
        sa.Column("encrypted_body", sa.LargeBinary()),
        sa.Column("complete_bytes", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("ack_status", sa.Text()),
        sa.Column("ack_code", sa.Text()),
        sa.Column("expected_sequence", sa.BigInteger()),
        sa.Column("ack_body", sa.LargeBinary()),
        sa.CheckConstraint("command_sequence > 0", name="worker_commands_sequence"),
        sa.CheckConstraint("complete_bytes >= 0", name="worker_commands_size"),
        sa.CheckConstraint("complete_bytes <= 1048576", name="worker_commands_body_limit"),
        sa.CheckConstraint("state IN ('pending','acknowledged','erased')",
                           name="worker_commands_state"),
        sa.CheckConstraint(
            "(state = 'pending' AND encrypted_body IS NOT NULL AND key_version IS NOT NULL) "
            "OR (state <> 'pending' AND encrypted_body IS NULL AND key_version IS NULL)",
            name="worker_commands_body_state",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND ack_status IS NULL AND ack_body IS NULL) OR "
            "(state = 'acknowledged' AND ack_status IS NOT NULL AND ack_body IS NOT NULL) OR "
            "(state = 'erased' AND ack_body IS NULL)",
            name="worker_commands_ack_state",
        ),
    )
    op.create_index("worker_commands_created", "worker_commands", ["created_at"])
    op.add_column(
        "jobs",
        sa.Column("current_dispatch_sequence", sa.BigInteger(), nullable=False,
                  server_default="0"),
    )
    op.create_table(
        "job_attempts",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("dispatch_sequence", sa.BigInteger(), primary_key=True),
        sa.Column("command_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("incarnation", sa.Uuid(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("dispatch_token_hash", sa.String(64), nullable=False),
        sa.Column("range_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("dispatch_sequence > 0", name="job_attempts_sequence"),
        sa.CheckConstraint("state IN ('dispatching')", name="job_attempts_state"),
    )
    op.create_index("job_attempts_worker", "job_attempts", ["worker_id", "incarnation"])
    op.add_column(
        "worker_connections",
        sa.Column("next_command_sequence", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "worker_connections",
        sa.Column("acknowledged_floor", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "worker_connections_sequence",
        "worker_connections",
        "next_command_sequence > 0 AND acknowledged_floor >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("worker_connections_sequence", "worker_connections", type_="check")
    op.drop_column("worker_connections", "acknowledged_floor")
    op.drop_column("worker_connections", "next_command_sequence")
    op.drop_index("job_attempts_worker", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_column("jobs", "current_dispatch_sequence")
    op.drop_index("worker_commands_created", table_name="worker_commands")
    op.drop_table("worker_commands")
