"""F2 startup ownership gaps: connect joins its migration thread, and the
pending-delete caller waits out the actual session close."""

import asyncio
import threading

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, jobs
from app.settings import Settings

BOUND = 5


def test_connect_waits_for_migration_thread_after_direct_cancellation(monkeypatch):
    settings = Settings(database_url="postgresql://f2@localhost/f2")
    migration_started = threading.Event()
    release_migration = threading.Event()
    migration_finished = threading.Event()
    order: list[str] = []

    def fake_migrate(database_url: str) -> None:
        migration_started.set()
        release_migration.wait(timeout=BOUND)
        order.append("migration-finished")
        migration_finished.set()

    async def fake_postgres_version(database_url: str) -> tuple[int, ...]:
        return (16, 0)

    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_postgres_version", fake_postgres_version)
    monkeypatch.setattr(db, "_migrate", fake_migrate)
    monkeypatch.setattr(db, "engine", None)
    monkeypatch.setattr(db, "session_factory", None)
    monkeypatch.setattr(db, "local_user_id", None)

    async def exercise() -> None:
        connect_task = asyncio.create_task(db.connect(serving=False))
        connect_task.add_done_callback(lambda _: order.append("connect-finished"))
        try:
            started = await asyncio.to_thread(migration_started.wait, BOUND)
            assert started, "migration thread did not start"
            connect_task.cancel()
            await asyncio.sleep(0.05)
            connect_task.cancel()
            await asyncio.sleep(0.05)
            assert not connect_task.done(), "connect returned while the migration was blocked"
            assert not migration_finished.is_set(), "migration returned before it was released"
        finally:
            release_migration.set()
            await asyncio.to_thread(migration_finished.wait, BOUND)
            await asyncio.wait_for(asyncio.gather(connect_task, return_exceptions=True), 15)
        with pytest.raises(asyncio.CancelledError):
            await connect_task
        assert order.index("migration-finished") < order.index("connect-finished")

    asyncio.run(exercise())


def test_cross_loop_startup_lock_owner_cancelled_while_waiting_for_borrower(monkeypatch):
    import time

    import app.db as db

    class Connection:
        owner_loop = None
        lock_calls = 0
        close_calls = 0
        closed = False
        lock_held = False

        async def fetchval(self, query, key, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            assert key == 184467
            if "try_advisory_lock" in query:
                self.lock_calls += 1
                self.lock_held = True
                return True
            raise AssertionError("the session lock must release when the connection closes")

        async def close(self, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            self.close_calls += 1
            self.closed = True
            self.lock_held = False

    connection = Connection()
    owner_entered = threading.Event()
    borrower_entered = threading.Event()
    release_owner = threading.Event()
    release_borrower = threading.Event()
    owner_finished = threading.Event()
    borrower_finished = threading.Event()
    owner_loop = None
    owner_task = None
    owner_report = {}
    thread_errors = []

    async def connect(_, **kwargs):
        connection.owner_loop = asyncio.get_running_loop()
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect)

    def owner_thread():
        async def hold_owner():
            async with db.hold_local_startup_lock():
                owner_entered.set()
                await asyncio.to_thread(release_owner.wait)

        async def main():
            nonlocal owner_loop, owner_task
            owner_loop = asyncio.get_running_loop()
            owner_task = asyncio.create_task(hold_owner())
            owner_task.add_done_callback(lambda _: owner_finished.set())
            try:
                await owner_task
            except asyncio.CancelledError:
                owner_report["cancelled"] = True
                owner_report["close_calls"] = connection.close_calls
                owner_report["closed"] = connection.closed
                owner_report["lock_held"] = connection.lock_held
                with db._lock_guard:
                    owner_report["holders"] = db._lock_holders
                    owner_report["lock_connection"] = db._lock_connection
                    owner_report["lock_owner_loop"] = db._lock_owner_loop

        try:
            asyncio.run(main())
        except BaseException as error:
            thread_errors.append(error)

    def borrower_thread():
        async def hold_borrower():
            async with db.hold_local_startup_lock():
                borrower_entered.set()
                await asyncio.to_thread(release_borrower.wait)

        try:
            asyncio.run(hold_borrower())
        except BaseException as error:
            thread_errors.append(error)
        finally:
            borrower_finished.set()

    owner = threading.Thread(target=owner_thread)
    owner.start()
    borrower = None
    try:
        assert owner_entered.wait(timeout=BOUND), "owner did not enter the startup lock"
        assert owner_loop is not None and owner_task is not None, "owner loop and task were not recorded"

        borrower = threading.Thread(target=borrower_thread)
        borrower.start()
        assert borrower_entered.wait(timeout=BOUND), "borrower did not enter the startup lock"

        with db._lock_guard:
            holders = db._lock_holders
        assert holders == 2, "both entries must hold counted reservations"
        assert connection.lock_calls == 1, "the startup lock must acquire exactly one connection"
        assert connection.lock_held, "the advisory lock must be held after both entries"
        assert not connection.closed, "the startup lock connection must stay open after both entries"

        release_owner.set()
        closing = False
        reserved = False
        deadline = time.monotonic() + BOUND
        while time.monotonic() < deadline:
            with db._lock_guard:
                closing = db._lock_closing
                reserved = db._lock_connection is not None
            if closing and reserved:
                break
            time.sleep(0.01)
        assert closing and reserved, "owner did not reach the cross-loop borrower wait"
        assert not owner_finished.is_set(), "owner completed before the borrower released"

        for cancel_number in (1, 2):
            owner_loop.call_soon_threadsafe(owner_task.cancel)
            ticked = threading.Event()
            owner_loop.call_soon_threadsafe(ticked.set)
            assert ticked.wait(timeout=BOUND), "owner loop did not process the cancellation request"
            assert not owner_finished.wait(timeout=0.2), (
                f"owner exited after cancellation {cancel_number} while the borrower still held the lock"
            )
            assert not connection.closed, f"the startup lock connection closed after cancellation {cancel_number}"
            assert connection.lock_held, f"the advisory lock was lost after cancellation {cancel_number}"
            assert connection.close_calls == 0, f"the owner close started after cancellation {cancel_number}"

        release_borrower.set()
        assert borrower_finished.wait(timeout=BOUND), "borrower did not exit after release"
        assert owner_finished.wait(timeout=BOUND), "owner did not complete after the borrower released"
    finally:
        release_owner.set()
        release_borrower.set()
        owner.join(timeout=BOUND)
        if borrower is not None:
            borrower.join(timeout=BOUND)

    assert owner_report.get("cancelled") is True, "the owner must report CancelledError to its caller"
    assert owner_report.get("close_calls") == 1, "the close must run exactly once before CancelledError surfaces"
    assert owner_report.get("closed") is True, "the connection must be closed when CancelledError surfaces"
    assert owner_report.get("lock_held") is False, "the advisory lock flag must clear when CancelledError surfaces"
    assert owner_report.get("holders") == 0, "the lock state must clear before CancelledError surfaces"
    assert owner_report.get("lock_connection") is None, "the lock connection must be released before CancelledError"
    assert owner_report.get("lock_owner_loop") is None, "the owner loop must be released before CancelledError"
    assert connection.closed, "the startup lock connection must close on the owner loop"
    assert connection.close_calls == 1, "the owner must close the connection exactly once"
    assert not connection.lock_held, "the advisory lock must be released by the close"
    with db._lock_guard:
        holders = db._lock_holders
        lock_connection = db._lock_connection
        lock_owner_loop = db._lock_owner_loop
    assert holders == 0, "no startup lock reservations may remain"
    assert lock_connection is None, "the startup lock connection must be cleared"
    assert lock_owner_loop is None, "the startup lock owner loop must be cleared"
    assert thread_errors == [], f"worker threads must exit without error: {thread_errors!r}"
    assert not owner.is_alive() and not borrower.is_alive(), "cleanup failure: worker thread still alive"


def test_record_pending_delete_waits_for_actual_session_close(monkeypatch):
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    order: list[str] = []

    class FocusSession(AsyncSession):
        async def execute(self, *args, **kwargs):
            return None

        async def commit(self):
            return None

        async def close(self):
            close_started.set()
            await asyncio.wait_for(release_close.wait(), BOUND)
            await super().close()
            order.append("close-returned")

    monkeypatch.setattr(db, "session_factory", FocusSession)

    async def exercise() -> None:
        caller = asyncio.create_task(jobs.record_pending_delete("f2/key", "receipt timeout"))
        caller.add_done_callback(lambda _: order.append("caller-finished"))
        try:
            await asyncio.wait_for(close_started.wait(), BOUND)
            caller.cancel()
            await asyncio.sleep(0.05)
            caller.cancel()
            await asyncio.sleep(0.05)
            assert not caller.done(), "caller returned while the actual close was blocked"
            assert "close-returned" not in order, "the actual close returned before release"
        finally:
            release_close.set()
            await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 15)
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert order.index("close-returned") < order.index("caller-finished")

    asyncio.run(exercise())
