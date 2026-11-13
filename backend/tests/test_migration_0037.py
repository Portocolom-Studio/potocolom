"""0037 adds the pause flag to fleet members (issue #889).

A member that exists before the upgrade is unpaused, and a downgrade drops the
column without touching the row. The scratch database and the helpers are
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


async def _has_paused_column(connection) -> bool:
    return await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'fleet_members' AND column_name = 'paused')")


async def _members(connection) -> list[tuple]:
    rows = await connection.fetch(
        "SELECT worker_id, paused FROM fleet_members ORDER BY worker_id")
    return [tuple(row) for row in rows]


@pytest.mark.db
def test_upgrade_downgrade_upgrade_keeps_a_member_and_leaves_it_unpaused():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "head")

        async def seed(connection):
            await connection.execute(
                "INSERT INTO fleet_members (id, worker_id, label, device, join_expires_at) "
                "VALUES (gen_random_uuid(), 'm-seeded', 'Seeded', 'cuda', "
                "now() + interval '1 hour')")

        scratch.run(seed)
        assert scratch.run(_has_paused_column) is True
        assert scratch.run(_members) == [("m-seeded", False)]

        command.downgrade(_config(scratch.alembic_url), "0036")
        assert scratch.run(_has_paused_column) is False

        command.upgrade(_config(scratch.alembic_url), "head")
        assert scratch.run(_has_paused_column) is True
        assert scratch.run(_members) == [("m-seeded", False)]
