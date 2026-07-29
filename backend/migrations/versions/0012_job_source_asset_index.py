"""index generation lineage child lookups

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("jobs_source_asset", "jobs", ["source_asset_id"])


def downgrade() -> None:
    op.drop_index("jobs_source_asset", table_name="jobs")
