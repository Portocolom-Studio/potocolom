import asyncio
import threading
from urllib.parse import urlsplit

import pytest

from app.db import _postgres_version_supported


def _reset_startup_lock_state(db, connection):
    with db._lock_guard:
        db._lock_holders = 0
        db._lock_connection = None
        db._lock_owner_loop = None
        db._lock_acquisition = None
        db._lock_borrowers_done = None
        db._lock_closing = False
    if not connection.closed:
        connection.closed = True


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
    assert db._lock_holders == 0
    assert db._lock_connection is None
    assert db._lock_owner_loop is None


def test_startup_lock_supports_overlapping_same_process_entries(monkeypatch):
    import app.db as db

    class Connection:
        def __init__(self):
            self.lock_calls = 0
            self.close_calls = 0
            self.closed = False

        async def fetchval(self, query, key, **kwargs):
            assert key == 184467
            if "try_advisory_lock" in query:
                self.lock_calls += 1
                return True
            raise AssertionError("the session lock must release when the connection closes")

        async def close(self, **kwargs):
            self.close_calls += 1
            self.closed = True

    connection = Connection()
    connect_calls = []

    async def connect(_, **kwargs):
        connect_calls.append(True)
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    async def exercise():
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with db.hold_local_startup_lock():
                if both_entered.is_set():
                    pass
                else:
                    both_entered.set()
                await release.wait()

        first = asyncio.create_task(holder())
        await both_entered.wait()
        second = asyncio.create_task(holder())
        while db._lock_holders < 2:
            await asyncio.sleep(0)
        assert len(connect_calls) == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())
    assert connection.lock_calls == 1
    assert connection.close_calls == 1
    assert connection.closed


def test_simultaneous_startup_lock_entries_share_one_acquisition(monkeypatch):
    import app.db as db

    class Connection:
        def __init__(self):
            self.lock_calls = 0
            self.close_calls = 0
            self.closed = False

        async def fetchval(self, query, key, **kwargs):
            assert key == 184467
            if "try_advisory_lock" in query:
                self.lock_calls += 1
                return True
            raise AssertionError("the session lock must release when the connection closes")

        async def close(self, **kwargs):
            self.close_calls += 1
            self.closed = True

    connection = Connection()
    connect_calls = []
    connect_started = asyncio.Event()
    finish_connect = asyncio.Event()

    async def connect(_, **kwargs):
        connect_calls.append(True)
        connect_started.set()
        await finish_connect.wait()
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    async def exercise():
        both_entered = asyncio.Event()
        finish_holders = asyncio.Event()
        entered = 0

        async def holder():
            nonlocal entered
            async with db.hold_local_startup_lock():
                entered += 1
                if entered == 2:
                    both_entered.set()
                await finish_holders.wait()

        first = asyncio.create_task(holder())
        await connect_started.wait()
        second = asyncio.create_task(holder())
        await asyncio.sleep(0)
        assert len(connect_calls) == 1
        finish_connect.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        finish_holders.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())
    assert connection.lock_calls == 1
    assert connection.close_calls == 1
    assert connection.closed


def test_cancelled_waiter_releases_last_startup_lock_reservation(monkeypatch):
    import app.db as db

    class Connection:
        closed = False

        async def fetchval(self, query, key, **kwargs):
            assert "try_advisory_lock" in query
            assert key == 184467
            return True

        async def close(self, **kwargs):
            self.closed = True

    connection = Connection()
    connect_started = asyncio.Event()
    finish_connect = asyncio.Event()
    waiter_started = asyncio.Event()

    async def connect(_, **kwargs):
        connect_started.set()
        await finish_connect.wait()
        return connection

    original_wrap_future = asyncio.wrap_future
    observed = {}

    def hold_waiter_resume(future, loop=None):
        if future is db._lock_acquisition:
            waiter_started.set()
            return (loop or asyncio.get_running_loop()).create_future()
        return original_wrap_future(future, loop=loop)

    monkeypatch.setattr(db.asyncpg, "connect", connect)
    monkeypatch.setattr(asyncio, "wrap_future", hold_waiter_resume)

    async def exercise():
        async def holder():
            async with db.hold_local_startup_lock():
                pass

        first = asyncio.create_task(holder())
        await connect_started.wait()
        second = asyncio.create_task(holder())
        await waiter_started.wait()
        finish_connect.set()
        await asyncio.sleep(0.05)
        owner_exited_before_waiter = first.done()
        first.cancel()
        first.cancel()
        await asyncio.sleep(0)
        connection_held_during_cancellation = not connection.closed
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        with pytest.raises(asyncio.CancelledError):
            await first
        observed["owner_exited_before_waiter"] = owner_exited_before_waiter
        observed["connection_held_during_cancellation"] = connection_held_during_cancellation
        observed["closed"] = connection.closed
        observed["holders"] = db._lock_holders

    try:
        asyncio.run(exercise())
    finally:
        monkeypatch.setattr(asyncio, "wrap_future", original_wrap_future)
        _reset_startup_lock_state(db, connection)

    assert observed == {
        "owner_exited_before_waiter": False,
        "connection_held_during_cancellation": True,
        "closed": True,
        "holders": 0,
    }


def test_final_cross_loop_startup_lock_exit_closes_on_owner_loop(monkeypatch):
    import app.db as db

    class Connection:
        owner_loop = None
        lock_calls = 0
        close_calls = 0
        closed = False

        async def fetchval(self, query, key, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            assert key == 184467
            if "try_advisory_lock" in query:
                self.lock_calls += 1
                return True
            raise AssertionError("the session lock must release when the connection closes")

        async def close(self, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            self.close_calls += 1
            self.closed = True

    connection = Connection()
    outer_entered = threading.Event()
    inner_entered = threading.Event()
    finish_outer = threading.Event()
    finish_inner = threading.Event()
    thread_errors = []

    async def connect(_, **kwargs):
        connection.owner_loop = asyncio.get_running_loop()
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    def outer_thread():
        async def hold_outer():
            async with db.hold_local_startup_lock():
                outer_entered.set()
                await asyncio.to_thread(finish_outer.wait)

        try:
            asyncio.run(hold_outer())
        except BaseException as error:
            thread_errors.append(error)

    outer = threading.Thread(target=outer_thread)
    outer.start()
    inner = None
    observed = {}
    try:
        assert outer_entered.wait(timeout=1)

        async def hold_inner():
            async with db.hold_local_startup_lock():
                inner_entered.set()
                await asyncio.to_thread(finish_inner.wait)

        def inner_thread():
            try:
                asyncio.run(hold_inner())
            except BaseException as error:
                thread_errors.append(error)

        inner = threading.Thread(target=inner_thread)
        inner.start()
        assert inner_entered.wait(timeout=1)
        finish_outer.set()
        outer.join(timeout=0.05)
        observed["owner_waited_for_borrower"] = outer.is_alive()
        observed["connection_held"] = not connection.closed
        finish_inner.set()
        inner.join(timeout=1)
        outer.join(timeout=1)
        observed["threads_stopped"] = not inner.is_alive() and not outer.is_alive()
        observed["thread_errors"] = thread_errors.copy()
        observed["lock_calls"] = connection.lock_calls
        observed["close_calls"] = connection.close_calls
        observed["closed"] = connection.closed
    finally:
        finish_outer.set()
        finish_inner.set()
        if inner is not None:
            inner.join(timeout=1)
        outer.join(timeout=1)
        _reset_startup_lock_state(db, connection)

    assert observed == {
        "owner_waited_for_borrower": True,
        "connection_held": True,
        "threads_stopped": True,
        "thread_errors": [],
        "lock_calls": 1,
        "close_calls": 1,
        "closed": True,
    }


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
