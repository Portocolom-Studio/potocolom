"""0041 clears the canvas content that older account purges left behind.

A session of an account purged by an older release has user_id NULL. The
upgrade clears its prompt, auth binding and browser binding, and ends it if it
was still nonterminal. A session of a live account is not touched. The
downgrade is a no-op, so the cleared content stays cleared.
"""

import uuid

import pytest
from alembic import command

from app import db
from tests.test_migration_0035 import Scratch, _config

PREVIOUS = "0040"
NIL_BROWSER = uuid.UUID(int=0)


@pytest.fixture(autouse=True)
def _run_database_migrated(portal_runner):
    # The cleanup after each db test reads this run's own database, which has
    # to be at head before that read, as in test_migration_0035.
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(db.dispose())


@pytest.mark.db
def test_upgrade_scrubs_orphaned_session_content_and_downgrade_keeps_it_scrubbed():
    user_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    kept_id = uuid.uuid4()
    orphan_auth = uuid.uuid4()
    kept_auth = uuid.uuid4()
    kept_browser = uuid.uuid4()

    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), PREVIOUS)

        async def seed(connection):
            await connection.execute(
                "INSERT INTO users (id, email, role, state) "
                "VALUES ($1, 'm41@example.com', 'user', 'active')", user_id)
            await connection.execute(
                "INSERT INTO realtime_sessions (id, user_id, auth_session_id, browser_owner_id, "
                "region, model_id, desired_params, effective_params, state, control_generation, "
                "started_at) VALUES ($1, NULL, $2, $3, 'local', 'sd-sim', "
                "'{\"prompt\": \"orphan-prompt\"}'::jsonb, '{\"prompt\": \"orphan-prompt\"}'::jsonb, "
                "'live', 1, now())", orphan_id, orphan_auth, kept_browser)
            await connection.execute(
                "INSERT INTO realtime_sessions (id, user_id, auth_session_id, browser_owner_id, "
                "region, model_id, desired_params, effective_params, state, control_generation, "
                "started_at) VALUES ($1, $2, $3, $4, 'local', 'sd-sim', "
                "'{\"prompt\": \"kept-prompt\"}'::jsonb, '{\"prompt\": \"kept-prompt\"}'::jsonb, "
                "'live', 1, now())", kept_id, user_id, kept_auth, kept_browser)

        scratch.run(seed)
        assert scratch.run(lambda c: _row(c, orphan_id))["prompt"] == "orphan-prompt"

        command.upgrade(_config(scratch.alembic_url), "0041")
        orphan = scratch.run(lambda c: _row(c, orphan_id))
        assert orphan == {
            "state": "ended",
            "ended": True,
            "terminal_reason": "account deleted",
            "prompt": None,
            "effective": None,
            "auth_session_id": None,
            "browser_owner_id": NIL_BROWSER,
        }
        kept = scratch.run(lambda c: _row(c, kept_id))
        assert kept == {
            "state": "live",
            "ended": False,
            "terminal_reason": None,
            "prompt": "kept-prompt",
            "effective": "kept-prompt",
            "auth_session_id": kept_auth,
            "browser_owner_id": kept_browser,
        }

        command.downgrade(_config(scratch.alembic_url), PREVIOUS)
        assert scratch.run(lambda c: _row(c, orphan_id)) == orphan
        assert scratch.run(lambda c: _row(c, kept_id)) == kept


async def _row(connection, session_id):
    row = await connection.fetchrow(
        "SELECT state, ended_at IS NOT NULL AS ended, terminal_reason, "
        "desired_params ->> 'prompt' AS prompt, effective_params ->> 'prompt' AS effective, "
        "auth_session_id, browser_owner_id FROM realtime_sessions WHERE id = $1",
        session_id)
    return dict(row)
