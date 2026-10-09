"""0033 adds the four indexes the measured plans justify (issue #692).

Every create must run CONCURRENTLY: these are live tables, and a plain
CREATE INDEX holds a lock that blocks writes for the whole build. The
if_exists drop that precedes each create clears the INVALID index a failed
concurrent build leaves behind, so a retry can start over.
"""

import asyncio
import importlib.util
import os
import secrets
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app import db
from conftest import _VERSIONS

# In migration order.
INDEXES = {
    "audit_events_target": ("audit_events", ["target_user_id", "occurred_at"]),
    "audit_events_action": ("audit_events", ["action", "occurred_at"]),
    "assets_user": ("assets", ["user_id"]),
    "users_purge_due": ("users", ["state", "deletion_requested_at"]),
}

# What PostgreSQL reports back for the four, so a column order
# that drifted from these would fail here rather than in a plan nobody runs.
EXPECTED_INDEXES = {
    "audit_events_target": (
        "CREATE INDEX audit_events_target ON public.audit_events "
        "USING btree (target_user_id, occurred_at)"
    ),
    "audit_events_action": (
        "CREATE INDEX audit_events_action ON public.audit_events "
        "USING btree (action, occurred_at)"
    ),
    "assets_user": "CREATE INDEX assets_user ON public.assets USING btree (user_id)",
    "users_purge_due": (
        "CREATE INDEX users_purge_due ON public.users USING btree "
        "(state, deletion_requested_at)"
    ),
}


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_context(self) -> "RecordingOperations":
        return self

    def autocommit_block(self):
        return nullcontext()

    def drop_index(self, *args, **kwargs) -> None:
        self.calls.append(("drop", args, kwargs))

    def create_index(self, *args, **kwargs) -> None:
        self.calls.append(("create", args, kwargs))


def load_migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/0033_measured_indexes.py"
    spec = importlib.util.spec_from_file_location("migration_0033", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_builds_every_index_concurrently_after_dropping_the_same_name() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    calls = operations.calls
    assert len(calls) == 2 * len(INDEXES)
    for position, (name, (table, columns)) in enumerate(INDEXES.items()):
        assert calls[2 * position] == (
            "drop",
            (name,),
            {"table_name": table, "postgresql_concurrently": True, "if_exists": True},
        )
        kind, args, kwargs = calls[2 * position + 1]
        assert kind == "create"
        assert args == (name, table, columns)
        assert kwargs == {"postgresql_concurrently": True}


def test_downgrade_drops_all_four_concurrently() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    assert [call[1][0] for call in operations.calls] == list(reversed(INDEXES))
    for kind, args, kwargs in operations.calls:
        assert kind == "drop"
        # if_exists too: DROP INDEX CONCURRENTLY can itself fail and leave the
        # index behind, and a second attempt must not stop at "does not exist".
        assert kwargs == {
            "table_name": INDEXES[args[0]][0],
            "postgresql_concurrently": True,
            "if_exists": True,
        }


@pytest.mark.db
def test_0033_upgrade_defines_the_four_indexes_and_downgrade_removes_them(portal_runner):
    # Migrate this run's database first: the teardown below this test deletes
    # job rows, and this file may be the first one in a run to reach it.
    assert portal_runner(db.connect(serving=False)) is True
    admin_url = make_url(os.environ["DATABASE_URL"]).set(database="postgres")
    database = f"p6m_{secrets.token_hex(8)}"
    test_url = admin_url.set(database=database, drivername="postgresql+asyncpg")
    url = test_url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        finally:
            await connection.close()

    config = Config(str(_VERSIONS.parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        test_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    try:
        asyncio.run(create_database())
        command.upgrade(config, "0033")

        async def verify_upgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                rows = await connection.fetch(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = ANY($1)",
                    list(EXPECTED_INDEXES),
                )
                assert {row["indexname"]: row["indexdef"] for row in rows} == EXPECTED_INDEXES
                # A concurrent build that could not finish leaves the index
                # behind marked invalid, and the planner ignores it.
                assert await connection.fetchval(
                    "SELECT bool_and(i.indisvalid) FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY($1)",
                    list(EXPECTED_INDEXES),
                ) is True
            finally:
                await connection.close()

        asyncio.run(verify_upgrade())
        command.downgrade(config, "0032")

        async def verify_downgrade() -> None:
            connection = await asyncpg.connect(url)
            try:
                assert await connection.fetchval(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = ANY($1)",
                    list(EXPECTED_INDEXES),
                ) == 0
            finally:
                await connection.close()

        asyncio.run(verify_downgrade())
    finally:
        async def drop_database() -> None:
            connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
            try:
                exists = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database)
                if exists:
                    await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            finally:
                await connection.close()

        asyncio.run(drop_database())
        portal_runner(db.dispose())
