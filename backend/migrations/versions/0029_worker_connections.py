"""durable scheduler lease and worker registration

Revision ID: 0029
Revises: 0028

The regional lease a single scheduler process holds, and the registration row
one worker incarnation gets when it says hello under protocol 6. The row is
written at registration and closed at disconnect; a later protocol revision
adds the work dispatch columns it will carry.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

WORKER_LIFECYCLES = "('connecting','loading','calibrating','ready','draining','ended')"


def upgrade() -> None:
    op.create_table(
        "scheduler_leases",
        sa.Column("region", sa.Text(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("owner_epoch > 0", name="scheduler_leases_epoch"),
    )
    op.create_table(
        "worker_connections",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("incarnation", sa.Uuid(), primary_key=True),
        sa.Column("transport_owner_id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_nonce", sa.Uuid(), nullable=False),
        sa.Column("grant_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("realtime_slots", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.SmallInteger(), nullable=False),
        sa.Column("capabilities", JSONB(), nullable=False),
        sa.Column("manifests", JSONB(), nullable=False),
        sa.Column("device", sa.Text()),
        sa.Column("memory_mode", sa.Text()),
        sa.Column("envelope_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("execution_envelope", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("observations", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("lifecycle", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            f"lifecycle IN {WORKER_LIFECYCLES}",
            name="worker_connections_lifecycle",
        ),
        sa.CheckConstraint("protocol_version IN (5,6)", name="worker_connections_protocol"),
        sa.CheckConstraint("owner_epoch > 0", name="worker_connections_epoch"),
        sa.CheckConstraint("realtime_slots > 0", name="worker_connections_slots"),
    )
    op.create_index(
        "worker_connections_one_current",
        "worker_connections",
        ["worker_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "worker_connections_ready_region",
        "worker_connections",
        ["region", "lifecycle", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("worker_connections_ready_region", table_name="worker_connections")
    op.drop_index("worker_connections_one_current", table_name="worker_connections")
    op.drop_table("worker_connections")
    op.drop_table("scheduler_leases")
