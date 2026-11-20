"""0038 indexes jobs by owner and state (issue #1004).

Rows that exist before the upgrade keep their state, and a downgrade drops
the index without touching them. The scratch database and the helpers are
shared with test_migration_0035.
"""

import pytest
from alembic import command

from app import db
from tests.test_migration_0035 import Scratch, _config


@pytest.fixture(autouse=True)
def _run_database_migrated(portal_runner):
    # The cleanup after each db test reads this run's own database, which has
    # to be at head before that read, as in test_migration_0035.
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(db.dispose())


@pytest.mark.db
def test_upgrade_downgrade_upgrade_keeps_populated_jobs_and_the_index():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0037")

        async def seed(connection):
            user_id = await connection.fetchval(
                "INSERT INTO users (id, email, role, state) "
                "VALUES (gen_random_uuid(), 'm38@example.com', 'user', 'active') "
                "RETURNING id")
            await connection.execute(
                "INSERT INTO models (id, name, capabilities, parameters_schema, min_vram_gb) "
                "VALUES ('m38-model', 'M38', '[]'::jsonb, '{}'::jsonb, 0)")
            for state in ("queued", "running", "succeeded"):
                await connection.execute(
                    "INSERT INTO jobs (id, user_id, model_id, params, state, attempt) "
                    "VALUES (gen_random_uuid(), $1, 'm38-model', '{\"prompt\": \"kept\"}'::jsonb, "
                    "$2, 1)",
                    user_id, state)

        scratch.run(seed)
        assert scratch.run(_indexdef) is None
        assert scratch.run(_states) == ["queued", "running", "succeeded"]

        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_indexdef) == (
            "CREATE INDEX jobs_user_state ON public.jobs USING btree (user_id, state)"
        )
        assert scratch.run(_states) == ["queued", "running", "succeeded"]

        command.downgrade(_config(scratch.alembic_url), "0037")
        assert scratch.run(_indexdef) is None
        assert scratch.run(_states) == ["queued", "running", "succeeded"]

        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_indexdef) == (
            "CREATE INDEX jobs_user_state ON public.jobs USING btree (user_id, state)"
        )
        assert scratch.run(_states) == ["queued", "running", "succeeded"]


async def _indexdef(connection):
    return await connection.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_user_state'")


async def _states(connection):
    rows = await connection.fetch("SELECT state FROM jobs ORDER BY state")
    return [row["state"] for row in rows]
