"""prompt search on generation history

Revision ID: 0025
Revises: 0024

GET /api/v1/generations?q= uses pg_trgm against the prompt stored in
jobs.params. The GIN index is on that expression so the ids-only overlay can
mark thousands of canvas nodes without a sequential scan (issue #132).
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX jobs_prompt_trgm ON jobs "
        "USING gin ((params->>'prompt') gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_prompt_trgm")
