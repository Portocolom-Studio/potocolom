"""Shared test environment, set before app.settings is first read: a dedicated
potocolom_test database (created here if the dev PostgreSQL is up) and a
temporary storage root, so a developer's dev data stays untouched.

Tests marked db skip when PostgreSQL is unreachable; `make deps` starts it.
"""

import asyncio
import atexit
import os
import shutil
import tempfile
from urllib.parse import urlsplit

import pytest

os.environ.setdefault("DATABASE_URL",
                      "postgresql://potocolom:potocolom@localhost:5432/potocolom_test")
_storage_root = tempfile.mkdtemp(prefix="potocolom-test-")
os.environ.setdefault("STORAGE_LOCAL_PATH", _storage_root)
atexit.register(shutil.rmtree, _storage_root, ignore_errors=True)


def _prepare_database() -> bool:
    import asyncpg

    url = urlsplit(os.environ["DATABASE_URL"])
    database = url.path.lstrip("/")

    async def prepare() -> None:
        conn = await asyncpg.connect(host=url.hostname, port=url.port or 5432,
                                     user=url.username, password=url.password,
                                     database="postgres", timeout=3)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1",
                                         database)
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
            await conn.execute("TRUNCATE gpu_samples, gpu_sample_rollups, assets, jobs")
        except asyncpg.UndefinedTableError:
            pass  # first run; migrations have not created the tables yet
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
