"""the one banner the SPA may show

Revision ID: 0028
Revises: 0027

A single-row table (issue #46): an administrator sets it from the admin area,
and an install that never sets one leaves the table empty. The cloud autoscaler's
automatic high_demand trigger is designed, not shipped here (docs/api.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

BANNER_KINDS = "('high_demand', 'degraded', 'maintenance')"


def upgrade() -> None:
    op.create_table(
        "status_banner",
        sa.Column("id", sa.Boolean(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("message_key", sa.Text(), nullable=True),
        sa.Column("custom_text", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                 server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("id", name="status_banner_singleton"),
        sa.CheckConstraint(f"kind IN {BANNER_KINDS}", name="status_banner_kind"),
        sa.CheckConstraint("char_length(custom_text) <= 280",
                           name="status_banner_custom_text_length"),
    )


def downgrade() -> None:
    op.drop_table("status_banner")
