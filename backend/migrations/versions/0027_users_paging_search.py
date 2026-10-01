"""index the admin user list for paging and search

Revision ID: 0027
Revises: 0026

GET /api/v1/users now pages by keyset on (created_at, id) the way
jobs.list_generations does, and filters by a substring of email (issue
#641). Without users_created_id the keyset predicate falls back to a sort of
the whole table every page; without users_email_trgm a substring search is a
sequential scan of every row.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX users_created_id ON users (created_at DESC, id DESC)"
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX users_email_trgm ON users USING gin (email gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS users_email_trgm")
    op.execute("DROP INDEX IF EXISTS users_created_id")
