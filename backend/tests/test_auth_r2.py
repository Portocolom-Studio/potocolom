import asyncio

import asyncpg
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.pool import NullPool

from app import auth
from app import db
from app.keyring import KeyRing
import app.main as main_module
from app.main import app
from app.settings import Settings
from app.tables import Asset


def test_accounts_mode_exposes_password_and_configured_providers():
    """Naming a provider is not the same as being able to use one. The
    frontend renders a button per method, and a button that cannot complete
    is worse than no button."""
    assert Settings(auth_mode="accounts").auth_methods == ["password"]
    assert Settings(auth_mode="accounts", oauth_providers="google, github, ").auth_methods == [
        "password"
    ]
    ready = Settings(auth_mode="accounts", oauth_providers="google, github, ",
                     google_client_id="g", google_client_secret="gs",
                     github_client_id="h", github_client_secret="hs")
    assert ready.auth_methods == ["password", "google", "github"]


def test_health_and_readiness_when_connect_fails(monkeypatch):
    async def unavailable():
        db.engine = None
        db.session_factory = None
        return False

    monkeypatch.setattr(db, "connect", unavailable)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 503


@pytest.mark.db
def test_readiness_when_required_stores_are_available(monkeypatch):
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").status_code == 200


@pytest.mark.db
def test_readiness_reports_unavailable_asset_store(monkeypatch):
    class UnavailableStore:
        async def ready(self):
            return False

    monkeypatch.setattr(main_module, "get_storage", lambda: UnavailableStore(), raising=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").status_code == 503


@pytest.mark.db
def test_database_engine_uses_bounded_pool(portal_runner):
    assert portal_runner(db.connect()) is True
    try:
        assert db.engine is not None
        assert not isinstance(db.engine.pool, NullPool)
        assert db.engine.pool.size() > 0
        assert db.engine.pool._max_overflow >= 0
    finally:
        portal_runner(db.dispose())


@pytest.mark.db
def test_pooled_engine_route_setup_and_dispose_share_one_loop(monkeypatch):
    loops = []
    original_connect = db.connect
    original_dispose = db.dispose
    route = "/__r2_loop_probe"

    async def connect():
        loops.append(("connect", asyncio.get_running_loop()))
        return await original_connect()

    async def dispose():
        loops.append(("dispose", asyncio.get_running_loop()))
        return await original_dispose()

    async def probe(session=Depends(db.get_session)):
        loops.append(("route", asyncio.get_running_loop()))
        await session.execute(text("SELECT 1"))
        return {"ok": True}

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "dispose", dispose)
    app.add_api_route(route, probe)
    added = next(item for item in app.routes if getattr(item, "path", None) == route)
    try:
        with TestClient(app) as client:
            async def setup():
                loops.append(("setup", asyncio.get_running_loop()))
                async with db.session_factory() as session:
                    await session.execute(text("SELECT 1"))

            client.portal.call(setup)
            assert client.get(route).status_code == 200
    finally:
        app.router.routes.remove(added)
    assert len({loop for _, loop in loops}) == 1


def test_account_dependency_rejects_before_principal(monkeypatch):
    called = False

    async def principal():
        nonlocal called
        called = True
        raise AssertionError("principal construction must not run")

    app.dependency_overrides[auth.current_user] = principal
    monkeypatch.setattr(db, "engine", None)
    monkeypatch.setattr(db, "session_factory", None)
    route = "/__r2_account_dependency_probe"

    async def probe(_=Depends(db.require_account_dependencies), principal=Depends(auth.current_user)):
        return {"ok": True}

    app.add_api_route(route, probe)
    added = next(item for item in app.routes if getattr(item, "path", None) == route)
    try:
        response = TestClient(app).get(route)
    finally:
        app.dependency_overrides.clear()
        app.router.routes.remove(added)
    assert response.status_code == 503
    assert not called


@pytest.mark.db
def test_accounts_enable_persists_mode_and_existing_ownership(portal_runner, monkeypatch):
    # Enabling accounts records the root key version it writes with, so it
    # needs a ring; test_auth_r3.py covers what happens without one.
    monkeypatch.setattr(db, "get_key_ring", lambda: KeyRing([(1, bytes(range(32)))]))
    assert portal_runner(db.connect()) is True
    enabled = False
    try:
        assert db.local_user_id is not None
        user_id = db.local_user_id
        async def ownership():
            async with db.session_factory() as session:
                rows = await session.execute(select(Asset.id, Asset.user_id))
                return rows.all()

        async def create_asset():
            async with db.session_factory() as session:
                asset = Asset(
                    user_id=user_id,
                    storage_key=f"{user_id}/r2.png",
                    mime="image/png",
                    width=1,
                    height=1,
                )
                session.add(asset)
                await session.commit()

        portal_runner(create_asset())
        existing_asset_owner = portal_runner(ownership())
        with pytest.raises(RuntimeError, match="explicit"):
            portal_runner(db.validate_startup_auth_mode("accounts"))
        async def enable_twice():
            await asyncio.gather(
                db.enable_accounts_mode(db.session_factory),
                db.enable_accounts_mode(db.session_factory),
            )

        portal_runner(enable_twice())
        enabled = True
        assert portal_runner(db.read_installation_auth_mode()) == "accounts"
        assert portal_runner(ownership()) == existing_asset_owner
        assert db.local_user_id == user_id
        with pytest.raises(RuntimeError, match="accounts"):
            portal_runner(db.validate_startup_auth_mode("none"))
    finally:
        if enabled:
            async def remove_auth_state():
                async with db.session_factory() as session:
                    await session.execute(text("DELETE FROM installation_auth_state"))
                    await session.commit()

            portal_runner(remove_auth_state())
        portal_runner(db.dispose())


@pytest.mark.db
def test_accounts_without_redis_refuses_concurrent_startup(portal_runner):
    async def exercise():
        first = await asyncpg.connect(db.get_settings().database_url)
        second = await asyncpg.connect(db.get_settings().database_url)
        try:
            async with db.accounts_startup_lock(first):
                with pytest.raises(RuntimeError, match="accounts startup"):
                    async with db.accounts_startup_lock(second):
                        pass
            # A session lock the holder never releases wedges every restart
            # until PostgreSQL reaps the backend on its keepalive timeout.
            async with db.accounts_startup_lock(second):
                pass
        finally:
            await second.close()
            await first.close()

    portal_runner(exercise())
