import asyncio

import pytest

from app.db import _postgres_version_supported


def test_postgres_version_comparison():
    assert not _postgres_version_supported((12, 99))
    assert _postgres_version_supported((13, 0))
    assert _postgres_version_supported((16, 3))


@pytest.mark.db
def test_a_supplied_database_is_read_but_never_emptied(monkeypatch):
    """conftest promises an exported DATABASE_URL is left alone.

    That held for the drop and not for the truncate, so a developer who pointed
    DATABASE_URL at a database with rows in it lost them to a test run.
    """
    import asyncpg

    import conftest

    supplied = "potocolom_test_supplied_probe"
    admin = "postgresql://potocolom:potocolom@localhost:5432/postgres"
    url = f"postgresql://potocolom:potocolom@localhost:5432/{supplied}"

    async def sql(dsn, *statements, fetch=None):
        conn = await asyncpg.connect(dsn, timeout=3)
        try:
            for statement in statements:
                await conn.execute(statement)
            return await conn.fetchval(fetch) if fetch else None
        finally:
            await conn.close()

    asyncio.run(sql(admin, f'DROP DATABASE IF EXISTS "{supplied}" WITH (FORCE)',
                    f'CREATE DATABASE "{supplied}"'))
    try:
        asyncio.run(sql(url, "CREATE TABLE jobs (id int)", "INSERT INTO jobs VALUES (42)"))
        monkeypatch.setattr(conftest, "_OURS", False)
        monkeypatch.setattr(conftest, "_DATABASE_URL", url)
        assert conftest._prepare_database() is True
        assert asyncio.run(sql(url, fetch="SELECT count(*) FROM jobs")) == 1
    finally:
        asyncio.run(sql(admin, f'DROP DATABASE IF EXISTS "{supplied}" WITH (FORCE)'))
