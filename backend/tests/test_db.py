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

    # Named per run like every other database here: a fixed name inside the
    # fix for shared names would let two runs of this test drop each other's
    # probe, which is the defect the branch exists to remove.
    supplied = f"potocolom_test_supplied_probe_{conftest._RUN}"
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

        # Stamped at a revision this tree cannot resolve: a database of this
        # suite's own would be rebuilt, and a supplied one must be refused
        # instead, which is the other half of the same promise.
        asyncio.run(sql(url, "CREATE TABLE alembic_version (version_num varchar(32))",
                        "INSERT INTO alembic_version VALUES ('9999')"))
        with pytest.raises(RuntimeError, match="not mine to drop"):
            conftest._prepare_database()
        assert asyncio.run(sql(url, fetch="SELECT count(*) FROM jobs")) == 1
    finally:
        asyncio.run(sql(admin, f'DROP DATABASE IF EXISTS "{supplied}" WITH (FORCE)'))
