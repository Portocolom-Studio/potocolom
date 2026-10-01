"""index one user's usage events by time

Revision ID: 0026
Revises: 0025

GET /api/v1/usage/me reads only the caller's rows inside a time window
(issue #95). usage_events_created_at cannot skip the other accounts' rows and
usage_events has no user-leading index, so every answer was a scan of the
whole raw retention.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("usage_events_user_created", "usage_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("usage_events_user_created", table_name="usage_events")
