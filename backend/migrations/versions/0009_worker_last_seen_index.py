"""index worker activity

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Serves daily telemetry selection and stale worker pruning.
    op.create_index("workers_last_seen", "workers", ["last_seen"])


def downgrade() -> None:
    op.drop_index("workers_last_seen", table_name="workers")
