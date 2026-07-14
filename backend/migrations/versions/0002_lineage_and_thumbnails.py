"""lineage columns and thumbnail parent links

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("source_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "jobs_source_asset_id_fkey", "jobs", "assets",
        ["source_asset_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("assets", sa.Column("parent_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "assets_parent_asset_id_fkey", "assets", "assets",
        ["parent_asset_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("assets_parent", "assets", ["parent_asset_id"])


def downgrade() -> None:
    op.drop_index("assets_parent", table_name="assets")
    op.drop_constraint("assets_parent_asset_id_fkey", "assets", type_="foreignkey")
    op.drop_column("assets", "parent_asset_id")
    op.drop_constraint("jobs_source_asset_id_fkey", "jobs", type_="foreignkey")
    op.drop_column("jobs", "source_asset_id")
