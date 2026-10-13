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
