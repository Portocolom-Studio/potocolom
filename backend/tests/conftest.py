"""Shared test environment, set before app.settings is first read: a dedicated
potocolom_test database (created here if the dev PostgreSQL is up) and a
temporary storage root, so a developer's dev data stays untouched.

Tests marked db skip when PostgreSQL is unreachable; `make deps` starts it.
"""

import asyncio
import atexit
import os
import pathlib
import shutil
import tempfile
from urllib.parse import urlsplit

import pytest

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"

os.environ.setdefault("DATABASE_URL",
                      "postgresql://potocolom:potocolom@localhost:5432/potocolom_test")
os.environ.setdefault("TELEMETRY", "false")
_storage_root = tempfile.mkdtemp(prefix="potocolom-test-")
os.environ.setdefault("STORAGE_LOCAL_PATH", _storage_root)
atexit.register(shutil.rmtree, _storage_root, ignore_errors=True)


def _local_revisions() -> set[str]:
    """Revision ids this checkout can migrate through. Every migration names its
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
            return stamped is not None and stamped not in _local_revisions()
        finally:
            await conn.close()

    async def prepare() -> None:
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database="postgres", timeout=3)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1",
                                         database)
            # Worktrees share this database. One carrying a newer migration
            # leaves it stamped at a revision this checkout cannot resolve, and
            # alembic then refuses to start: every db test fails for a reason
            # that looks like broken application code. Rebuild, don't truncate.
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
