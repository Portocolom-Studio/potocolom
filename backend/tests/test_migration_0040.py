"""0040 indexes realtime session attempts by worker (issue #730).

Every create must run CONCURRENTLY: this is a live table, and a plain
CREATE INDEX holds a lock that blocks writes for the whole build. The
if_exists drop that precedes each create clears the INVALID index a failed
concurrent build leaves behind, so a retry can start over.
"""

import importlib.util
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command

from app import db
from tests.test_migration_0035 import Scratch, _config

INDEX = "realtime_attempts_worker"
TABLE = "realtime_session_attempts"
EXPECTED_INDEX = (
    "CREATE INDEX realtime_attempts_worker ON public.realtime_session_attempts "
    "USING btree (worker_id, incarnation) "
    "WHERE (state = ANY (ARRAY['opening'::text, 'running'::text]))"
)
SEEDED_STATES = (
    ("opening", "assigning"),
    ("running", "live"),
    ("ended", "ended"),
)


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
    path = Path(__file__).parents[1] / "migrations/versions/0040_attempt_worker_index.py"
    spec = importlib.util.spec_from_file_location("migration_0040", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_builds_the_partial_index_concurrently_after_dropping_the_same_name() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert len(operations.calls) == 2
    assert operations.calls[0] == (
        "drop",
        (INDEX,),
        {"table_name": TABLE, "postgresql_concurrently": True, "if_exists": True},
    )
    kind, args, kwargs = operations.calls[1]
    assert kind == "create"
    assert args == (INDEX, TABLE, ["worker_id", "incarnation"])
    assert kwargs["postgresql_concurrently"] is True
    assert kwargs["postgresql_where"] is not None


def test_downgrade_drops_the_index_concurrently() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    assert len(operations.calls) == 1
    kind, args, kwargs = operations.calls[0]
    assert kind == "drop"
    assert args == (INDEX,)
    assert kwargs == {
        "table_name": TABLE,
        "postgresql_concurrently": True,
        "if_exists": True,
    }


@pytest.fixture
def _run_database_migrated(portal_runner):
    # The cleanup after each db test reads this run's own database, which has
    # to be at head before that read, as in test_migration_0038.
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(db.dispose())


async def _seed(connection) -> None:
    for attempt_state, session_state in SEEDED_STATES:
        session_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO realtime_sessions "
            "(id, browser_owner_id, region, model_id, state, control_generation) "
            "VALUES ($1, $2, 'eu-west', 'm40-model', $3, 1)",
            session_id, uuid.uuid4(), session_state)
        await connection.execute(
            "INSERT INTO realtime_session_attempts "
            "(session_id, control_generation, worker_id, incarnation, owner_epoch, "
            "attempt_id, state) "
            "VALUES ($1, 1, 'worker-m40', $2, 1, $3, $4)",
            session_id, uuid.uuid4(), uuid.uuid4(), attempt_state)


async def _attempts(connection):
    rows = await connection.fetch(
        "SELECT session_id, control_generation, worker_id, incarnation, owner_epoch, "
        "attempt_id, state FROM realtime_session_attempts ORDER BY state")
    return [tuple(row) for row in rows]


async def _index_definition(connection):
    return await connection.fetchval(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = $1",
        INDEX,
    )


async def _index_is_valid(connection):
    return await connection.fetchval(
        "SELECT i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = $1",
        INDEX,
    )


@pytest.mark.db
@pytest.mark.usefixtures("_run_database_migrated")
def test_0040_upgrade_defines_the_partial_index_and_keeps_attempt_rows_through_a_round_trip():
    with Scratch() as scratch:
        command.upgrade(_config(scratch.alembic_url), "0039")
        scratch.run(_seed)
        seeded = scratch.run(_attempts)
        assert [row[-1] for row in seeded] == ["ended", "opening", "running"]
        assert scratch.run(_index_definition) is None

        command.upgrade(_config(scratch.alembic_url), "0040")
        assert scratch.run(_index_definition) == EXPECTED_INDEX
        assert scratch.run(_index_is_valid) is True
        assert scratch.run(_attempts) == seeded

        command.downgrade(_config(scratch.alembic_url), "0039")
        assert scratch.run(_index_definition) is None
        assert scratch.run(_attempts) == seeded
