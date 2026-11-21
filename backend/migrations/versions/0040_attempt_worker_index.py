"""index realtime session attempts by worker for disconnect and erasure

Revision ID: 0040
Revises: 0039

A partial index on (worker_id, incarnation) for opening and running attempts
lets close_worker and the account erasure fence use an index instead of
scanning every attempt row ever kept.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_ACTIVE = sa.text("state IN ('opening', 'running')")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "realtime_attempts_worker",
            table_name="realtime_session_attempts",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "realtime_attempts_worker",
            "realtime_session_attempts",
            ["worker_id", "incarnation"],
            postgresql_where=_ACTIVE,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "realtime_attempts_worker",
            table_name="realtime_session_attempts",
            postgresql_concurrently=True,
            if_exists=True,
        )
