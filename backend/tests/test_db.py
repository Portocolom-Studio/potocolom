import asyncio
from urllib.parse import urlsplit

import pytest

from app.db import _postgres_version_supported


def test_postgres_version_comparison():
    assert not _postgres_version_supported((12, 99))
    assert _postgres_version_supported((13, 0))
    assert _postgres_version_supported((16, 3))


def test_startup_lock_closes_connection_without_explicit_unlock(monkeypatch):
    import app.db as db

    class Connection:
        closed = False
        calls = 0

        async def fetchval(self, query, key, **kwargs):
            self.calls += 1
            assert "try_advisory_lock" in query
            assert key == 184467
            return True

        async def close(self, **kwargs):
            self.closed = True

    connection = Connection()

    async def connect(_, **kwargs):
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    async def exercise():
        async with db.hold_local_startup_lock():
            pass

    asyncio.run(exercise())
    assert connection.closed
    assert connection.calls == 1


def test_startup_lock_terminates_connection_when_close_fails(monkeypatch):
    import app.db as db

    class Connection:
        closed = False
        terminated = False

        async def fetchval(self, query, key, **kwargs):
            assert "try_advisory_lock" in query
            assert key == 184467
            return True

        async def close(self, **kwargs):
            raise RuntimeError("close failed")

        def terminate(self):
            self.terminated = True
            self.closed = True

    connection = Connection()

    async def connect(_, **kwargs):
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    async def exercise():
        with pytest.raises(RuntimeError, match="close failed"):
            async with db.hold_local_startup_lock():
                pass

    asyncio.run(exercise())
    assert connection.terminated
    assert connection.closed


def test_cancelled_startup_lock_exit_waits_for_the_connection_close(monkeypatch):
    import app.db as db

    events = []

    async def exercise():
        close_started = asyncio.Event()
        finish_close = asyncio.Event()

        class Connection:
            async def fetchval(self, query, key, **kwargs):
                return True

            async def close(self, **kwargs):
                close_started.set()
                await finish_close.wait()
                events.append("closed")

        async def connect(_, **kwargs):
            return Connection()

        monkeypatch.setattr(db.asyncpg, "connect", connect)

        async def holder():
            async with db.hold_local_startup_lock():
                pass

        holding = asyncio.create_task(holder())
        await close_started.wait()
        holding.cancel()
        await asyncio.sleep(0)
        holding.cancel()
        await asyncio.sleep(0)
        assert not holding.done()
        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await holding

    asyncio.run(exercise())
    assert events == ["closed"]


def test_run_to_completion_returns_the_work_result():
    import app.db as db

    async def exercise():
        async def work():
            await asyncio.sleep(0)
            return "done"

        return await db.run_to_completion(work())

    assert asyncio.run(exercise()) == "done"


def test_run_to_completion_raises_the_work_error():
    import app.db as db

    async def exercise():
        async def work():
            raise RuntimeError("work failed")

        with pytest.raises(RuntimeError, match="work failed"):
            await db.run_to_completion(work())

    asyncio.run(exercise())


def test_run_to_completion_waits_through_repeated_cancellation():
    import app.db as db

    async def exercise():
        finished = False
        release = asyncio.Event()

        async def work():
            nonlocal finished
            await release.wait()
            finished = True

        async def caller():
            await db.run_to_completion(work())

        waiting = asyncio.create_task(caller())
        await asyncio.sleep(0)
        waiting.cancel()
        waiting.cancel()
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert finished

    asyncio.run(exercise())


def test_run_to_completion_keeps_a_cancellation_over_the_work_error():
    """db.connect turns any Exception into degraded startup, so a failed
    migration must not swallow the cancellation that arrived during it."""
    import app.db as db

    async def exercise():
        release = asyncio.Event()

        async def work():
            await release.wait()
            raise RuntimeError("work failed")

        raised = []

        async def caller():
            try:
                await db.run_to_completion(work())
            except asyncio.CancelledError as error:
                raised.append(error)
                raise

        waiting = asyncio.create_task(caller())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert str(raised[0].__cause__) == "work failed"

    asyncio.run(exercise())


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
    configured = urlsplit(conftest._DATABASE_URL)
    admin = configured._replace(path="/postgres").geturl()
    url = configured._replace(path=f"/{supplied}").geturl()

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
        with monkeypatch.context() as patch:
            patch.setattr(conftest, "_OURS", False)
            patch.setattr(conftest, "_DATABASE_URL", url)
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
