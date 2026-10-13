import asyncio

import pytest

from app import db
from app.settings import Settings


BOUND = 5
DATABASE_URL = "postgresql://f2@127.0.0.1/f2"


@pytest.mark.parametrize("cancel_startup", [False, True], ids=["ordinary-error", "cancelled"])
def test_lifespan_initialization_close_error_preserves_startup_outcome(
    monkeypatch, cancel_startup
):
    import app.main as main
    from test_main import _stub_lifespan_tasks

    _stub_lifespan_tasks(monkeypatch)

    settings = Settings(
        database_url=DATABASE_URL,
        fleet_token_key="f2-test-key",
        auth_mode="none",
        telemetry=False,
        email_backend="none",
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_postgres_version", lambda _: asyncio.sleep(0, result=(16, 0)))
    monkeypatch.setattr(db, "_migrate", lambda _: None)
    monkeypatch.setattr(db, "validate_startup_auth_mode", lambda _: asyncio.sleep(0))
    monkeypatch.setattr(db, "validate_startup_key_ring", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "check_mail_configuration", lambda _: None)
    monkeypatch.setattr(main, "check_oauth_configuration", lambda _: None)
    monkeypatch.setattr(main, "setup_logging", lambda _: None)
    monkeypatch.setattr(main.access_log, "install", lambda: None)
    monkeypatch.setattr(main.jobs, "recover", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main.jobs, "drain_blob_cleanup", lambda: asyncio.sleep(0))

    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_returned = asyncio.Event()
    disposed = asyncio.Event()
    guard_released = asyncio.Event()
    initialization_tasks = []
    close_tasks = []
    order = []
    close_error = RuntimeError("initialization session close failed")

    class AdvisoryConnection:
        owner_loop = None

        async def fetchval(self, query, key, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            assert "pg_try_advisory_lock" in query
            assert key == 184467
            return True

        async def close(self, **kwargs):
            assert asyncio.get_running_loop() is self.owner_loop
            order.append("guard-released")
            guard_released.set()

    connection = AdvisoryConnection()

    async def connect_advisory(*args, **kwargs):
        connection.owner_loop = asyncio.get_running_loop()
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", connect_advisory)
    monkeypatch.setattr(db, "engine", None)
    monkeypatch.setattr(db, "session_factory", None)
    monkeypatch.setattr(db, "local_user_id", None)
    original_dispose = db.dispose

    async def dispose():
        await original_dispose()
        order.append("disposed")
        disposed.set()

    monkeypatch.setattr(db, "dispose", dispose)
    selected_class = db.OwnedSession
    original_close = selected_class.close

    async def instrumented_close(session, *args, **kwargs):
        close_tasks.append(asyncio.current_task())
        close_started.set()
        await release_close.wait()
        await original_close(session, *args, **kwargs)
        order.append("close-returned")
        close_returned.set()
        raise close_error

    monkeypatch.setattr(selected_class, "close", instrumented_close)

    async def ensure_local_user(factory):
        assert factory is db.session_factory
        assert factory.class_ is selected_class
        assert factory.kw["bind"] is db.engine
        initialization_tasks.append(asyncio.current_task())
        async with factory():
            pass
        return None

    monkeypatch.setattr(db, "_ensure_local_user", ensure_local_user)

    async def exercise():
        lifespan = main.lifespan(main.app)
        startup_task = asyncio.create_task(lifespan.__aenter__())
        failure = None
        outcome = None
        cleanup_failures = []
        try:
            await asyncio.wait_for(close_started.wait(), BOUND)
            assert len(initialization_tasks) == 1
            assert len(close_tasks) == 1
            initialization = initialization_tasks[0]
            close_task = close_tasks[0]
            assert not startup_task.done()
            assert not initialization.done()
            assert not close_task.done()

            if cancel_startup:
                startup_task.cancel("startup stopped")
                await asyncio.sleep(0)
                startup_task.cancel("startup stopped again")
                await asyncio.sleep(0)
                assert not startup_task.done(), "startup finished while session close was held"
                assert not initialization.done(), "initialization finished while close was held"
                assert not close_task.done(), "session close finished before release"
                assert not disposed.is_set(), "database disposal started while close was held"
                assert not guard_released.is_set(), "startup guard released while close was held"
                assert db._lock_holders == 1
                assert db._lock_connection is connection

            release_close.set()
            try:
                await asyncio.wait_for(startup_task, BOUND)
            except BaseException as error:
                outcome = error
            assert initialization.done()
            assert close_task.done()
            assert disposed.is_set()
            assert guard_released.is_set()
            assert order == ["close-returned", "disposed", "guard-released"]
            assert db.engine is None
            assert db.session_factory is None
            assert db.local_user_id is None
            assert db._lock_holders == 0
            assert db._lock_connection is None
            assert db._lock_owner_loop is None
        except BaseException as error:
            failure = error
        finally:
            release_close.set()
            if not startup_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(startup_task), BOUND)
                except BaseException as error:
                    cleanup_failures.append(error)
            if startup_task.done():
                try:
                    await startup_task
                except BaseException as error:
                    if outcome is None:
                        outcome = error
                if outcome is None:
                    try:
                        await asyncio.wait_for(lifespan.__aexit__(None, None, None), BOUND)
                    except BaseException as error:
                        cleanup_failures.append(error)
            for task in (*initialization_tasks, *close_tasks):
                if not task.done():
                    done, pending = await asyncio.wait({task}, timeout=BOUND)
                    if pending:
                        cleanup_failures.append(AssertionError("owned initialization task did not finish"))
                if task.done():
                    try:
                        await task
                    except BaseException:
                        pass
            if db.engine is not None:
                try:
                    await asyncio.wait_for(original_dispose(), BOUND)
                except BaseException as error:
                    cleanup_failures.append(error)
        if failure is not None:
            raise failure
        if cleanup_failures:
            raise cleanup_failures[0]

        assert startup_task.done()
        if cancel_startup:
            assert isinstance(outcome, asyncio.CancelledError)
            assert outcome.__cause__ is close_error
        else:
            assert outcome is close_error

    asyncio.run(exercise())
