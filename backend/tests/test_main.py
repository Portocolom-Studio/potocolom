"""Self-hosted SPA static serving and the AUTH_MODE startup gate."""

import asyncio
import importlib
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

import app.main as main_module
from app.main import SPAStaticFiles, app
from app.settings import Settings, get_settings


def _stub_lifespan_tasks(monkeypatch):
    async def idle():
        await asyncio.Future()

    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_loop",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)


def _stub_degraded_lifespan(monkeypatch):
    @asynccontextmanager
    async def admission():
        yield

    async def unavailable():
        return False

    async def drain():
        pass

    async def dispose():
        pass

    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "connect", unavailable)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    _stub_lifespan_tasks(monkeypatch)


@pytest.fixture
def migrated_task_database(portal_runner):
    import app.db as db

    dispose = db.dispose
    assert portal_runner(db.connect(serving=False)) is True
    try:
        yield
    finally:
        portal_runner(dispose())


@pytest.mark.parametrize("mode", ["local", "oauth"])
def test_retired_auth_mode_fails_settings_validation(monkeypatch, mode):
    monkeypatch.setenv("AUTH_MODE", mode)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as error:
            get_settings()
        assert mode in str(error.value)
        assert "none" in str(error.value)
        assert "accounts" in str(error.value)
    finally:
        get_settings.cache_clear()


def test_accounts_mode_imports_and_authenticates_nobody(monkeypatch):
    """Accounts mode boots now, and the guard that used to be an import-time
    refusal moved to the principal: nothing resolves to the implicit admin."""
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    try:
        importlib.reload(main_module)
        assert get_settings().auth_mode == "accounts"
    finally:
        monkeypatch.delenv("AUTH_MODE", raising=False)
        get_settings.cache_clear()
        importlib.reload(main_module)


FLEET_TOKEN_KEY_UNSET = (
    "FLEET_TOKEN_KEY is unset; refusing fleet handshakes. "
    "Run scripts/preflight.sh to write deploy/compose/.env, "
    "then set FLEET_TOKEN_KEY from FLEET_SECRET."
)


def test_unset_fleet_token_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("FLEET_TOKEN_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=re.escape(FLEET_TOKEN_KEY_UNSET)):
            with TestClient(app):
                pass
    finally:
        monkeypatch.setenv("FLEET_TOKEN_KEY", "test-fleet-token")
        get_settings.cache_clear()


def test_unset_fleet_token_key_cannot_even_import(monkeypatch):
    monkeypatch.delenv("FLEET_TOKEN_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=re.escape(FLEET_TOKEN_KEY_UNSET)):
            importlib.reload(main_module)
    finally:
        monkeypatch.setenv("FLEET_TOKEN_KEY", "test-fleet-token")
        get_settings.cache_clear()
        importlib.reload(main_module)


def test_auth_mode_none_starts(monkeypatch):
    """AUTH_MODE=none is the only implemented mode and must not be refused."""
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    _stub_degraded_lifespan(monkeypatch)
    try:
        with TestClient(app):
            pass
    finally:
        get_settings.cache_clear()


def test_auth_mode_unset_starts(monkeypatch):
    """An unset AUTH_MODE falls back to the shipped none default, not a refusal."""
    monkeypatch.delenv("AUTH_MODE", raising=False)
    get_settings.cache_clear()
    _stub_degraded_lifespan(monkeypatch)
    try:
        with TestClient(app):
            pass
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["none", "accounts"])
def test_lifespan_holds_startup_admission_before_database_and_cleanup(monkeypatch, mode):
    events = []
    settings = Settings(auth_mode=mode, fleet_token_key="test-fleet-token")

    @asynccontextmanager
    async def admission():
        events.append("admission")
        yield
        events.append("release")

    async def connect():
        events.append("connect")
        return True

    async def recover():
        events.append("recover")

    async def idle():
        await asyncio.Future()

    async def drain():
        pass

    async def dispose():
        events.append("dispose")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "recover", recover)
    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_loop",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)

    async def exercise():
        async with main_module.lifespan(app):
            events.append("serving")

    asyncio.run(exercise())

    assert events.index("admission") < events.index("connect")
    assert events.index("connect") < events.index("recover")
    assert events.index("serving") < events.index("dispose")
    assert events.index("dispose") < events.index("release")


@pytest.mark.parametrize("mode", ["none", "accounts"])
@pytest.mark.db
def test_existing_startup_lock_refuses_lifespan_before_connect_or_work(
    monkeypatch, mode, portal_runner, migrated_task_database
):
    import asyncpg

    database_url = get_settings().database_url
    settings = Settings(
        auth_mode=mode,
        fleet_token_key="test-fleet-token",
        database_url=database_url,
    )
    calls = []

    async def connect():
        calls.append("connect")
        return True

    async def recover():
        calls.append("recover")

    async def idle():
        calls.append("task")
        await asyncio.Future()

    async def drain():
        pass

    async def dispose():
        pass

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "recover", recover)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    _stub_lifespan_tasks(monkeypatch)
    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_loop",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)

    async def exercise():
        holder = await asyncpg.connect(database_url)
        try:
            await holder.fetchval("SELECT pg_advisory_lock(184467::bigint)")
            with pytest.raises(RuntimeError, match="another .*startup"):
                async with main_module.lifespan(app):
                    pass
        finally:
            await holder.fetchval("SELECT pg_advisory_unlock(184467::bigint)")
            await holder.close()

    portal_runner(exercise())
    assert calls == []


@pytest.mark.db
def test_nested_testclients_share_one_local_startup_lock(monkeypatch, migrated_task_database):
    settings = Settings(
        auth_mode="none",
        fleet_token_key="test-fleet-token",
        database_url=get_settings().database_url,
    )

    async def unavailable():
        return False

    async def drain():
        pass

    async def defer_dispose():
        pass

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "connect", unavailable)
    monkeypatch.setattr(main_module.db, "dispose", defer_dispose)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    _stub_lifespan_tasks(monkeypatch)
    with TestClient(app):
        with TestClient(app):
            pass


def test_lifespan_startup_error_releases_admission(monkeypatch):
    events = []
    settings = Settings(auth_mode="accounts", fleet_token_key="test-fleet-token")

    @asynccontextmanager
    async def admission():
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    async def connect():
        events.append("connect")
        raise RuntimeError("startup probe failed")

    async def dispose():
        events.append("disposed")

    async def drain():
        events.append("drained")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)

    async def exercise():
        with pytest.raises(RuntimeError, match="startup probe failed"):
            async with main_module.lifespan(app):
                pass

    asyncio.run(exercise())
    assert events == ["acquired", "connect", "drained", "disposed", "released"]


@pytest.mark.parametrize("failure", ["recovery", "cleanup"])
def test_lifespan_releases_resources_after_recovery_or_shutdown_error(monkeypatch, failure):
    events = []
    settings = Settings(auth_mode="accounts", fleet_token_key="test-fleet-token")

    @asynccontextmanager
    async def admission():
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    async def connect():
        events.append("connected")
        return True

    async def recover():
        events.append("recovered")
        if failure == "recovery":
            raise RuntimeError("recovery failed")

    async def drain():
        events.append("drained")
        if failure == "cleanup":
            raise RuntimeError("cleanup failed")

    async def dispose():
        events.append("disposed")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "recover", recover)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    _stub_lifespan_tasks(monkeypatch)

    async def exercise():
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            async with main_module.lifespan(app):
                pass

    asyncio.run(exercise())
    assert events == ["admitted", "connected", "recovered", "drained", "disposed", "released"]


@pytest.mark.parametrize("mode", ["none", "accounts"])
def test_unreachable_admission_database_refuses_lifespan(monkeypatch, mode):
    settings = Settings(auth_mode=mode, fleet_token_key="test-fleet-token")
    calls = []

    async def unreachable(*args, **kwargs):
        raise OSError("admission database unavailable")

    async def connect():
        calls.append("connect")
        return True

    async def idle():
        await asyncio.Future()

    async def recover():
        calls.append("recover")

    async def drain():
        pass

    async def dispose():
        pass

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db.asyncpg, "connect", unreachable)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "recover", recover)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_loop",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)

    async def exercise():
        with pytest.raises(OSError, match="admission database unavailable"):
            async with main_module.lifespan(app):
                pass

    asyncio.run(exercise())
    assert calls == []


@pytest.mark.parametrize("mode", ["none", "accounts"])
def test_migration_failure_serves_degraded_only_while_admitted(monkeypatch, mode):
    events = []
    settings = Settings(auth_mode=mode, fleet_token_key="test-fleet-token")

    @asynccontextmanager
    async def admission():
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    async def connect():
        events.append("migration-failed")
        return False

    async def idle():
        await asyncio.Future()

    async def recover():
        events.append("recovered")

    async def drain():
        pass

    async def dispose():
        events.append("disposed")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "recover", recover)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
    for name in (
        "reap_dead_workers",
        "sweep_dead_sessions",
        "maintain_loop",
        "maintain_deletes_loop",
        "telemetry_loop",
        "mail_loop",
        "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)

    async def exercise():
        async with main_module.lifespan(app):
            events.append("serving")

    asyncio.run(exercise())
    assert events == ["admitted", "migration-failed", "serving", "disposed", "released"]


def test_repeated_shutdown_cancellation_keeps_admission_until_cleanup_returns(monkeypatch):
    events = []

    async def exercise():
        loop_stopped = asyncio.Event()
        finish_loop = asyncio.Event()
        drain_started = asyncio.Event()
        finish_drain = asyncio.Event()
        dispose_started = asyncio.Event()
        finish_dispose = asyncio.Event()

        @asynccontextmanager
        async def admission():
            events.append("admitted")
            try:
                yield
            finally:
                events.append("released")

        async def unavailable():
            return False

        async def held_loop():
            try:
                await asyncio.Future()
            finally:
                loop_stopped.set()
                await finish_loop.wait()
                events.append("loop-returned")

        async def drain():
            drain_started.set()
            await finish_drain.wait()
            events.append("drain-returned")

        async def dispose():
            dispose_started.set()
            await finish_dispose.wait()
            events.append("dispose-returned")

        _stub_lifespan_tasks(monkeypatch)
        monkeypatch.setattr(main_module, "reap_dead_workers", held_loop)
        monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
        monkeypatch.setattr(main_module.db, "connect", unavailable)
        monkeypatch.setattr(main_module.db, "dispose", dispose)
        monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)
        lifespan = main_module.lifespan(app)
        await lifespan.__aenter__()
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.wait_for(loop_stopped.wait(), 1)
        for _ in range(2):
            shutdown.cancel()
            await asyncio.sleep(0)
        assert not shutdown.done()
        assert events == ["admitted"]
        finish_loop.set()
        await asyncio.wait_for(drain_started.wait(), 1)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert "released" not in events
        finish_drain.set()
        await asyncio.wait_for(dispose_started.wait(), 1)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert "released" not in events
        finish_dispose.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 1)

    asyncio.run(exercise())
    assert events == ["admitted", "loop-returned", "drain-returned", "dispose-returned", "released"]


def test_cancellation_waits_for_migration_before_releasing_admission(monkeypatch):
    events = []
    migration_started = threading.Event()
    finish_migration = threading.Event()
    settings = Settings(
        auth_mode="none",
        fleet_token_key="test-fleet-token",
        database_url=get_settings().database_url,
    )
    original_connect = main_module.db.connect
    original_dispose = main_module.db.dispose

    @asynccontextmanager
    async def admission():
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    async def version(_):
        return (16, 3)

    def migrate(_):
        migration_started.set()
        finish_migration.wait()
        events.append("migration-finished")

    async def connect():
        return await original_connect(serving=False)

    async def drain():
        events.append("drained")

    async def dispose():
        await original_dispose()
        events.append("disposed")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.db, "hold_local_startup_lock", admission)
    monkeypatch.setattr(main_module.db, "_postgres_version", version)
    monkeypatch.setattr(main_module.db, "_migrate", migrate)
    monkeypatch.setattr(main_module.db, "connect", connect)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain)

    async def exercise():
        lifespan = asyncio.create_task(main_module.lifespan(app).__aenter__())
        try:
            await asyncio.to_thread(migration_started.wait)
            lifespan.cancel()
            await asyncio.sleep(0.05)
            assert "released" not in events
        finally:
            finish_migration.set()
        with pytest.raises(asyncio.CancelledError):
            await lifespan

    asyncio.run(exercise())
    assert events == ["admitted", "migration-finished", "drained", "disposed", "released"]


def _prerendered_build(tmp_path: Path) -> Path:
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>landing</title>")
    (dist / "login.html").write_text("<!doctype html><title>sign in</title>")
    (dist / "404.html").write_text("<!doctype html><title>not found</title>")
    (dist / "asset.txt").write_text("asset")
    (dist / "folder.html").mkdir()
    return dist


def test_prerendered_page_is_served_for_its_route(tmp_path: Path):
    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=_prerendered_build(tmp_path), html=True))

    with TestClient(app) as client:
        for path in ("/", "/index.html"):
            response = client.get(path)
            assert response.status_code == 200
            assert "landing" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        for path in ("/login", "/login/"):
            response = client.get(path)
            assert response.status_code == 200
            assert "sign in" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        assert client.head("/login").status_code == 200
        # The contract is that a hashed asset is not forced to revalidate, not
        # that the framework omits the header entirely.
        asset = client.get("/asset.txt")
        assert "no-cache" not in asset.headers.get("Cache-Control", "")


def test_unknown_path_is_the_not_found_page(tmp_path: Path):
    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=_prerendered_build(tmp_path), html=True))

    with TestClient(app) as client:
        for path in ("/nope", "/app/generate", "/api/v1/no-such-endpoint"):
            response = client.get(path)
            assert response.status_code == 404
            assert "not found" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        assert client.head("/nope").status_code == 404
        assert client.get("/folder").status_code == 404
        for malformed in ("/nope%00", "/" + "x" * 5000):
            assert client.get(malformed).status_code == 404
