"""durable record of privileged action

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

AUDIT_SEVERITIES = "('info', 'high')"


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        # SET NULL, not CASCADE: an administrator deleting their own account
        # must not erase the record of what they did with it.
        sa.Column("actor_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("object_ids", JSONB(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("severity", sa.Text(), nullable=False, server_default=sa.text("'info'")),
        sa.CheckConstraint(f"severity IN {AUDIT_SEVERITIES}", name="audit_events_severity"),
    )
    # Retention and the seven-day summary both read by time; search adds the actor.
    op.create_index("audit_events_occurred", "audit_events", ["occurred_at"])
    op.create_index("audit_events_actor", "audit_events", ["actor_user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("audit_events_actor", table_name="audit_events")
    op.drop_index("audit_events_occurred", table_name="audit_events")
    op.drop_table("audit_events")
