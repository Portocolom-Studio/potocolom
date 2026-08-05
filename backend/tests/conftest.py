"""Shared test environment, set before app.settings is first read: a test
database of this checkout's own (created here if the dev PostgreSQL is up) and a
temporary storage root, so a developer's dev data stays untouched.

The database name carries a hash of the checkout path. Worktrees are routine
here, they sit at different migrations, and one shared database means whichever
ran last decides what the others find: alembic refuses a revision it cannot
resolve and every db test then fails for a reason that looks like broken
application code. A database each removes the contention rather than arbitrating
it. Set DATABASE_URL to override.

Switching branches in one checkout hits the same problem without any worktree
involved, so the stamp check stays: if this database is stamped at a revision
this tree cannot resolve, rebuild it. Dropping is safe now that the database is
nobody else's - the objection to it was that it terminated another worktree's
connections mid-run.

Tests marked db skip when PostgreSQL is unreachable; `make deps` starts it.
"""

import asyncio
import atexit
import hashlib
import os
import pathlib
import shutil
import tempfile
from urllib.parse import urlsplit

import pytest

_CHECKOUT = pathlib.Path(__file__).resolve().parents[2]
_VERSIONS = _CHECKOUT / "backend" / "migrations" / "versions"
_SUFFIX = hashlib.sha256(str(_CHECKOUT).encode()).hexdigest()[:8]

os.environ.setdefault("DATABASE_URL",
                      "postgresql://potocolom:potocolom@localhost:5432/"
                      f"potocolom_test_{_SUFFIX}")
os.environ.setdefault("TELEMETRY", "false")
_storage_root = tempfile.mkdtemp(prefix="potocolom-test-")
os.environ.setdefault("STORAGE_LOCAL_PATH", _storage_root)
atexit.register(shutil.rmtree, _storage_root, ignore_errors=True)


def _local_revisions() -> set[str]:
    """Revision ids this tree can migrate through. Every migration names its
    revision after its filename prefix: 0011_usage_event_rollups.py declares
    revision = "0011". test_migrations.py keeps that assumption honest."""
    return {path.name.split("_", 1)[0] for path in _VERSIONS.glob("[0-9]*.py")}


def _prepare_database() -> bool:
    import asyncpg

    url = urlsplit(os.environ["DATABASE_URL"])
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
            # Switching branches leaves this stamped at a revision the tree
            # cannot resolve; alembic then refuses to start and every db test
            # fails for a reason that looks like broken application code.
            if exists and await stamped_ahead():
                await conn.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
                exists = None
            if not exists:
                await conn.execute(f'CREATE DATABASE "{database}"')
        finally:
            await conn.close()
        # Leftover pending jobs from an interrupted run would be requeued on
        # startup and reach a test's fake worker; start clean instead.
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database=database, timeout=3)
        try:
            candidates = (
                "telemetry_state", "usage_event_rollups", "usage_events",
                "benchmark_measurements", "benchmark_sessions", "workers",
                "gpu_samples", "gpu_sample_rollups", "assets", "jobs",
            )
            existing = [
                name for name in candidates
                if await conn.fetchval("SELECT to_regclass($1)", f"public.{name}") is not None
            ]
            if existing:
                await conn.execute(f"TRUNCATE {', '.join(existing)} CASCADE")
        finally:
            await conn.close()

    try:
        asyncio.run(prepare())
        return True
    except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
        return False


DATABASE_AVAILABLE = _prepare_database()


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs the development PostgreSQL")


def pytest_collection_modifyitems(config, items):
    if DATABASE_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="PostgreSQL unreachable; run make deps")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)
