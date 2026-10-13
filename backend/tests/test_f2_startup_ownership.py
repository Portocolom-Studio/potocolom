"""F2 startup ownership gaps: connect joins its migration thread, and the
pending-delete caller waits out the actual session close."""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

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

    class FocusSession(db.OwnedSession):
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


FAKE_DATABASE_URL = "postgresql://f2:fake@127.0.0.1/f2"


def _prepare_lazy_database(monkeypatch, settings=None):
    settings = settings or Settings(database_url=FAKE_DATABASE_URL)

    async def postgres_version(database_url):
        assert database_url == settings.database_url
        return (16, 0)

    def migrate(database_url):
        assert database_url == settings.database_url

    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_postgres_version", postgres_version)
    monkeypatch.setattr(db, "_migrate", migrate)
    monkeypatch.setattr(db, "engine", None)
    monkeypatch.setattr(db, "session_factory", None)
    monkeypatch.setattr(db, "local_user_id", None)
    return settings


async def _connect_lazy_database():
    connected = await db.connect(serving=False)
    assert connected, "fake-version database connect did not succeed"
    assert db.engine is not None, "connect did not create its lazy engine"
    assert db.session_factory is not None, "connect did not create its session factory"
    return db.engine, db.session_factory.class_


@pytest.mark.parametrize("close_fails", [False, True], ids=["close-ok", "close-error"])
def test_lifespan_shutdown_waits_for_actual_gpu_maintenance_close(monkeypatch, close_fails):
    from app import gpu_samples
    import app.main as main

    settings = Settings(
        database_url=FAKE_DATABASE_URL,
        fleet_token_key="f2-test-key",
        auth_mode="none",
        telemetry=False,
        email_backend="none",
    )
    _prepare_lazy_database(monkeypatch, settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "check_mail_configuration", lambda _: None)
    monkeypatch.setattr(main, "check_oauth_configuration", lambda _: None)
    monkeypatch.setattr(main, "setup_logging", lambda _: None)
    monkeypatch.setattr(main.access_log, "install", lambda: None)

    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_returned = asyncio.Event()
    guard_released = asyncio.Event()
    dispose_started = asyncio.Event()
    loop_stopped = asyncio.Event()
    refresh_calls = 0
    execute_calls = 0
    commit_calls = 0
    close_tasks = []
    maintenance_tasks = []
    engine_box = []
    close_error = RuntimeError("actual session close failed after closing") if close_fails else None
    real_connect = db.connect

    @asynccontextmanager
    async def startup_guard():
        try:
            yield
        finally:
            guard_released.set()

    async def dispose_event():
        dispose_started.set()

    async def fake_connect():
        connected = await real_connect(serving=False)
        assert connected
        engine_box.append(db.engine)
        selected_class = db.session_factory.class_

        class FocusSession(selected_class):
            async def execute(self, *args, **kwargs):
                nonlocal execute_calls
                execute_calls += 1
                return None

            async def commit(self):
                nonlocal commit_calls
                commit_calls += 1

            async def close(self):
                close_tasks.append(asyncio.current_task())
                close_started.set()
                await release_close.wait()
                await super().close()
                close_returned.set()
                if close_error is not None:
                    raise close_error

        db.session_factory = async_sessionmaker(
            db.engine, class_=FocusSession, expire_on_commit=False
        )
        return False

    async def parked_loop():
        await asyncio.Future()

    async def no_op():
        return None

    async def refresh_estimates():
        nonlocal refresh_calls
        refresh_calls += 1

    async def tracked_maintenance_loop():
        maintenance_tasks.append(asyncio.current_task())
        try:
            await gpu_samples.maintain_loop()
        finally:
            loop_stopped.set()

    monkeypatch.setattr(db, "hold_local_startup_lock", startup_guard)
    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "dispose", dispose_event)
    monkeypatch.setattr(main.jobs, "drain_blob_cleanup", no_op)
    monkeypatch.setattr(gpu_samples.estimates, "refresh_observed_timings", refresh_estimates)
    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main, name, parked_loop)
    monkeypatch.setattr(main.jobs, "dispatch_loop", parked_loop)
    monkeypatch.setattr(main, "maintain_loop", tracked_maintenance_loop)

    async def exercise():
        lifespan = main.lifespan(main.app)
        entered = False
        exit_task = None
        exit_result = None
        observed_execute_calls = None
        failure = None
        cleanup_failures = []
        try:
            await lifespan.__aenter__()
            entered = True
            await asyncio.wait_for(close_started.wait(), BOUND)
            observed_execute_calls = execute_calls
            assert observed_execute_calls > 0, "real maintain_once did not run its database body"
            assert commit_calls == 1, "real maintain_once did not commit before closing"

            exit_task = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(0.05)
            exit_task.cancel()
            await asyncio.sleep(0.05)
            exit_task.cancel()
            await asyncio.sleep(0.05)

            assert not close_returned.is_set(), "actual session close returned before release"
            assert not dispose_started.is_set(), "engine disposal started during actual session close"
            assert not guard_released.is_set(), "startup guard released during actual session close"
            assert not exit_task.done(), "lifespan shutdown returned during actual session close"
        except BaseException as error:
            failure = error
        finally:
            release_close.set()
            if entered and exit_task is None:
                exit_task = asyncio.create_task(lifespan.__aexit__(None, None, None))

            if close_started.is_set():
                try:
                    await asyncio.wait_for(close_returned.wait(), 15)
                except BaseException as error:
                    cleanup_failures.append(error)

            if exit_task is not None:
                _, pending = await asyncio.wait({exit_task}, timeout=15)
                if pending:
                    cleanup_failures.append(
                        AssertionError("lifespan shutdown did not finish after actual session close")
                    )
                    if maintenance_tasks:
                        maintenance_task = maintenance_tasks[0]
                        if not maintenance_task.done():
                            maintenance_task.cancel()

                    owned_tasks = {exit_task, *close_tasks, *maintenance_tasks}
                    done, pending = await asyncio.wait(owned_tasks, timeout=15)
                    finished_tasks = set(done)
                    if pending:
                        cleanup_failures.append(
                            AssertionError("owned shutdown tasks did not finish after maintenance cancellation")
                        )
                        for task in pending:
                            task.cancel()
                        done, pending = await asyncio.wait(pending, timeout=15)
                        finished_tasks.update(done)
                    if pending:
                        cleanup_failures.append(
                            AssertionError("owned shutdown tasks remained after bounded cleanup")
                        )
                    if finished_tasks:
                        await asyncio.gather(*finished_tasks, return_exceptions=True)

                if exit_task.done():
                    exit_result = await asyncio.gather(exit_task, return_exceptions=True)

            await asyncio.gather(*(task for task in close_tasks if task.done()), return_exceptions=True)
            if engine_box:
                try:
                    await engine_box[0].dispose()
                except BaseException as error:
                    cleanup_failures.append(error)
        if failure is not None:
            raise failure
        if cleanup_failures:
            raise cleanup_failures[0]

        assert close_returned.is_set(), "actual session close did not finish after release"
        assert loop_stopped.is_set(), "real GPU maintenance loop did not stop"
        assert dispose_started.is_set(), "lifespan did not dispose its database"
        assert guard_released.is_set(), "lifespan did not release its startup guard"
        assert exit_task is not None and exit_task.done(), "lifespan shutdown did not finish"
        assert exit_result and isinstance(exit_result[0], asyncio.CancelledError), (
            "two cancellations of the outer shutdown caller must preserve cancellation"
        )
        assert refresh_calls == 1, "maintenance retried after cancellation during close"
        assert execute_calls == observed_execute_calls, "maintenance restarted after cancellation during close"
        assert len(close_tasks) == 1, "maintenance opened another session after close failure"

    asyncio.run(exercise())


def test_factory_selected_session_class_propagates_ordinary_close_error(monkeypatch):
    _prepare_lazy_database(monkeypatch)

    async def exercise():
        engine, selected_class = await _connect_lazy_database()
        close_error = RuntimeError("ordinary close failed")

        class FocusSession(selected_class):
            async def close(self):
                await super().close()
                raise close_error

        factory = async_sessionmaker(engine, class_=FocusSession, expire_on_commit=False)
        try:
            with pytest.raises(RuntimeError) as raised:
                async with factory():
                    pass
            assert raised.value is close_error
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_factory_selected_session_class_preserves_body_cancellation_on_close_error(monkeypatch):
    _prepare_lazy_database(monkeypatch)

    async def exercise():
        engine, selected_class = await _connect_lazy_database()
        body_cancelled = asyncio.CancelledError("maintenance stopped")
        close_error = RuntimeError("actual close failed")

        class FocusSession(selected_class):
            async def close(self):
                await super().close()
                raise close_error

        factory = async_sessionmaker(engine, class_=FocusSession, expire_on_commit=False)
        try:
            with pytest.raises(asyncio.CancelledError) as raised:
                async with factory():
                    raise body_cancelled
            assert raised.value is body_cancelled, "close error replaced the body cancellation"
            assert raised.value.__cause__ is close_error, "close error was not retained as the cause"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_factory_selected_session_class_waits_through_repeated_close_cancellation(monkeypatch):
    _prepare_lazy_database(monkeypatch)

    async def exercise():
        engine, selected_class = await _connect_lazy_database()
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        close_returned = asyncio.Event()
        close_tasks = []

        class FocusSession(selected_class):
            async def close(self):
                close_tasks.append(asyncio.current_task())
                close_started.set()
                await release_close.wait()
                await super().close()
                close_returned.set()

        factory = async_sessionmaker(engine, class_=FocusSession, expire_on_commit=False)

        async def use_session():
            async with factory():
                pass

        caller = asyncio.create_task(use_session())
        failure = None
        try:
            await asyncio.wait_for(close_started.wait(), BOUND)
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0.05)
            assert not caller.done(), "caller exited before actual close finished"
            assert not close_returned.is_set(), "actual close returned before release"
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, BOUND)
        except BaseException as error:
            failure = error
        finally:
            release_close.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(caller, *close_tasks, return_exceptions=True), 15
                )
            except BaseException as error:
                if failure is None:
                    failure = error
            try:
                await engine.dispose()
            except BaseException as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure
        assert close_returned.is_set(), "actual close did not finish after release"

    asyncio.run(exercise())


def test_factory_selected_session_class_joins_self_cancelled_close_task(monkeypatch):
    _prepare_lazy_database(monkeypatch)

    async def exercise():
        engine, selected_class = await _connect_lazy_database()
        close_tasks = []
        close_finished = asyncio.Event()

        class FocusSession(selected_class):
            async def close(self):
                close_tasks.append(asyncio.current_task())
                try:
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                finally:
                    close_finished.set()

        factory = async_sessionmaker(engine, class_=FocusSession, expire_on_commit=False)

        async def use_session():
            async with factory():
                pass

        caller = asyncio.create_task(use_session())
        failure = None
        caller_result = None
        close_results = None
        try:
            await asyncio.wait_for(close_finished.wait(), BOUND)
            caller_result = await asyncio.wait_for(
                asyncio.gather(caller, return_exceptions=True), BOUND
            )
            close_results = await asyncio.wait_for(
                asyncio.gather(*close_tasks, return_exceptions=True), BOUND
            )
        except BaseException as error:
            failure = error
        finally:
            for task in (caller, *close_tasks):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(caller, *close_tasks, return_exceptions=True), 15
                )
            except BaseException as error:
                if failure is None:
                    failure = error
            try:
                await engine.dispose()
            except BaseException as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure
        assert caller_result and isinstance(caller_result[0], asyncio.CancelledError)
        assert close_results and isinstance(close_results[0], asyncio.CancelledError)
        assert len(close_tasks) == 1, "self-cancelled close task was restarted"

    asyncio.run(exercise())
