"""Shared test environment, set before app.settings is first read: a test
database of this run's own (created here if the dev PostgreSQL is up) and a
temporary storage root, so a developer's dev data stays untouched.

The database name carries a hash of the checkout path and this process's id.
The checkout part is for worktrees, which are routine here, sit at different
migrations, and would otherwise let whichever ran last decide what the others
find: alembic refuses a revision it cannot resolve and every db test then fails
for a reason that looks like broken application code.

The process part is for two runs of one checkout, which is just as ordinary: an
editor test runner beside a terminal run, or an agent probing while the suite
goes. They shared job and attempt rows, so the retry tests failed with a 403 on
an upload key that had just been issued, or waited for an attempt another run
had already advanced. Every one of those failures pointed at the application
rather than at the harness, which is what made them expensive (issue #280). The
storage root was already per process.

The cost is a migration chain per run rather than per checkout, which is a
second or two, and a database that has to be dropped afterwards: it goes at
exit, and `make test-db-clean` collects whatever a hard kill leaves behind. Set
DATABASE_URL to override the whole scheme.

A name this file generates never exists beforehand, so the stamp check that
used to rebuild a stale database now applies only to a supplied one, where it
refuses rather than rebuilds.

Tests marked db skip when PostgreSQL is unreachable; `make deps` starts it.
"""

import asyncio
import atexit
import hashlib
import os
import pathlib
import secrets
import shutil
import tempfile
from urllib.parse import urlsplit

import pytest

_CHECKOUT = pathlib.Path(__file__).resolve().parents[2]
_VERSIONS = _CHECKOUT / "backend" / "migrations" / "versions"
_SUFFIX = hashlib.sha256(str(_CHECKOUT).encode()).hexdigest()[:8]
# The pid names the run for whoever is watching pg_database during it; the
# random tail is what makes the name unique, since pids are recycled and two
# containers sharing this checkout can hold the same one.
_RUN = f"{_SUFFIX}_{os.getpid()}_{secrets.token_hex(8)}"

# What an exported DATABASE_URL buys: this suite never drops, rebuilds or
# empties it, and never drops it at the end. It does create it when missing,
# because CI supplies a name its PostgreSQL service has not created yet. And
# the application itself still migrates it and writes to it, because that is
# what starting the app does: alembic upgrades to head, the local user row is
# inserted or promoted, leftover running jobs are requeued, and the tests
# insert their own rows. Point it at a database you are willing to have the
# application own; it is not a read-only borrow.
_OURS = "DATABASE_URL" not in os.environ
os.environ.setdefault("DATABASE_URL",
                      "postgresql://potocolom:potocolom@localhost:5432/"
                      f"potocolom_test_{_RUN}")
_DATABASE_URL = os.environ["DATABASE_URL"]
os.environ.setdefault("TELEMETRY", "false")
_storage_root = tempfile.mkdtemp(prefix="potocolom-test-")
os.environ.setdefault("STORAGE_LOCAL_PATH", _storage_root)
atexit.register(shutil.rmtree, _storage_root, ignore_errors=True)


_SKIP_REASON = "PostgreSQL unreachable; run make deps"


def _local_revisions() -> set[str]:
    """Revision ids this tree can migrate through. Every migration names its
    revision after its filename prefix: 0011_usage_event_rollups.py declares
    revision = "0011". test_migrations.py keeps that assumption honest."""
    return {path.name.split("_", 1)[0] for path in _VERSIONS.glob("[0-9]*.py")}


def _prepare_database() -> bool:
    import asyncpg

    url = urlsplit(_DATABASE_URL)
    database = url.path.lstrip("/")

    async def stamped_ahead() -> bool:
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database=database, timeout=3)
        try:
            if await conn.fetchval("SELECT to_regclass($1)",
                                   "public.alembic_version") is None:
                return False
            stamped = await conn.fetchval("SELECT version_num FROM alembic_version")
            local = _local_revisions()
            # An empty set would call every stamp ahead and drop unconditionally.
            return bool(local) and stamped is not None and stamped not in local
        finally:
            await conn.close()

    async def prepare() -> None:
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database="postgres", timeout=3)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1",
                                         database)
            # A name this file generated cannot already exist, so everything
            # here is about a supplied one. Switching branches leaves a reused
            # database stamped at a revision this tree cannot resolve; alembic
            # then refuses to start and every db test fails for a reason that
            # looks like broken application code. Saying so beats dropping
            # someone else's database to make the run convenient.
            if exists and await stamped_ahead():
                raise RuntimeError(
                    f"{database} is stamped at a revision this tree cannot resolve, "
                    "and DATABASE_URL was supplied, so it is not mine to drop; "
                    "point DATABASE_URL elsewhere or migrate it yourself"
                )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{database}"')
        finally:
            await conn.close()
        if not _OURS:
            # Loud, because the alternative is finding out from mutated dev
            # data: the application migrates and writes to whatever it is
            # given, and rows here mean it was given something in use.
            conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                         user=url.username, password=url.password,
                                         database=database, timeout=3)
            try:
                if await conn.fetchval("SELECT to_regclass($1)", "public.jobs") is not None:
                    rows = await conn.fetchval("SELECT count(*) FROM jobs")
                    if rows:
                        print(f"\nWARNING: DATABASE_URL points at {database}, which holds "
                              f"{rows} job(s). The tests will migrate it and write to it.\n")
            finally:
                await conn.close()

    try:
        asyncio.run(prepare())
        return True
    except (OSError, asyncio.TimeoutError) as error:
        # The dev PostgreSQL is simply not up, which is the case the db marker
        # exists for. A supplied DATABASE_URL is a deliberate act, so a failure
        # to reach it is a broken run rather than an absent dependency: CI sets
        # one, and skipping every db test there would be a green run with no
        # database coverage in it.
        if not _OURS:
            raise RuntimeError(f"DATABASE_URL was supplied but unusable: {error}") from error
        return False
    except asyncpg.PostgresError as error:
        # Reachable but refusing: no CREATE DATABASE right, a role that is
        # gone, a broken pg_hba after an upgrade. For a supplied URL that is a
        # broken run and must be loud, because CI going green with 74 tests
        # skipped is worse than CI failing. For this file's own name it is the
        # developer's local PostgreSQL being unusable, where killing the whole
        # collection also takes away the tests that would have told them their
        # application code is fine; those skip, with the real reason.
        if not _OURS:
            raise RuntimeError(f"could not prepare {urlsplit(_DATABASE_URL).path.lstrip('/')}: "
                               f"{error}") from error
        global _SKIP_REASON
        _SKIP_REASON = f"PostgreSQL refused the test database: {error}"
        return False


def _drop_database() -> None:
    """Give the run's database back when the session ends.

    Called from pytest_sessionfinish rather than atexit: asyncpg needs an event
    loop and a thread pool, and at interpreter shutdown scheduling one raises
    "cannot schedule new futures". A hard kill skips this either way and leaves
    the database behind, which is what `make test-db-clean` collects, so the
    name stays matchable.
    """
    import asyncpg

    url = urlsplit(_DATABASE_URL)
    database = url.path.lstrip("/")

    async def drop() -> None:
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database="postgres", timeout=3)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await conn.close()

    try:
        asyncio.run(drop())
    except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
        pass  # nothing to report at exit; test-db-clean sweeps the leftovers


DATABASE_AVAILABLE = _prepare_database()


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs the development PostgreSQL")


def pytest_sessionfinish(session, exitstatus):
    if DATABASE_AVAILABLE and _OURS:
        _drop_database()


def pytest_collection_modifyitems(config, items):
    if DATABASE_AVAILABLE:
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)
