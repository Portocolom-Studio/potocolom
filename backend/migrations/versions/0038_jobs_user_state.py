"""index an account's jobs by state

Revision ID: 0038
Revises: 0037

jobs_user_created has no state, so the outstanding-job count would scan an
owner's whole history while it holds the account gate. A plain composite
index, not a partial one: a parameterized state list does not match a
partial predicate in a generic plan.
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Concurrently, as 0012 does: a plain build holds job writes for its
    # whole length.
    with op.get_context().autocommit_block():
        op.drop_index("jobs_user_state", table_name="jobs",
                      postgresql_concurrently=True, if_exists=True)
        op.create_index("jobs_user_state", "jobs", ["user_id", "state"],
                        postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("jobs_user_state", table_name="jobs", postgresql_concurrently=True)
