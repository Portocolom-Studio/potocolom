"""Record the installation's auth mode as a decision: unclaimed, none or accounts

Revision ID: 0035
Revises: 0034
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An install that already holds people or work was decided before this
    # revision existed, so an upgrade never shows the setup page. Only an empty
    # database is unclaimed. A row that already exists keeps its mode.
    op.execute(
        "INSERT INTO installation_auth_state (id, auth_mode, root_key_version) "
        "SELECT 1, CASE WHEN EXISTS (SELECT 1 FROM users) "
        "OR EXISTS (SELECT 1 FROM jobs) OR EXISTS (SELECT 1 FROM assets) "
        "THEN 'none' ELSE 'unclaimed' END, NULL "
        "WHERE NOT EXISTS (SELECT 1 FROM installation_auth_state WHERE id = 1)"
    )
    op.create_check_constraint(
        "installation_auth_state_mode", "installation_auth_state",
        "auth_mode IN ('unclaimed', 'none', 'accounts')",
    )


def downgrade() -> None:
    op.drop_constraint("installation_auth_state_mode", "installation_auth_state", type_="check")
    # Before this revision a missing row meant none, so the decisions this
    # revision added are removed. An accounts row stays, because it is the one
    # the earlier code reads.
    op.execute("DELETE FROM installation_auth_state WHERE auth_mode <> 'accounts'")
