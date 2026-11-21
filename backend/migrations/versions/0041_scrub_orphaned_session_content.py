"""Scrub canvas content that older purges left on realtime sessions

Revision ID: 0041
Revises: 0040

Releases before the purge fix (#1018) deleted the user row and kept the
realtime sessions of that account. Those rows have user_id NULL and still
hold the prompt, the auth-session id and the browser binding. This clears
them, and ends any that were still nonterminal.
"""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

_NIL_BROWSER = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute(
        "UPDATE realtime_sessions SET desired_params = NULL, effective_params = NULL, "
        f"auth_session_id = NULL, browser_owner_id = '{_NIL_BROWSER}' "
        "WHERE user_id IS NULL"
    )
    op.execute(
        "UPDATE realtime_sessions SET state = 'ended', "
        "ended_at = coalesce(ended_at, now()), "
        "terminal_reason = coalesce(terminal_reason, 'account deleted') "
        "WHERE user_id IS NULL AND state IN ('queued', 'assigning', 'live', 'idle', 'ending')"
    )


def downgrade() -> None:
    """No-op. The content that upgrade removed cannot be restored."""
