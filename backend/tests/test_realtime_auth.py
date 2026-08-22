import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app import db, realtime, sessions
from app.main import app
from app.passwords import hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, User
from tests.test_realtime import FLEET_HEADERS, REQUIRES_PROMPT, answer_ready, hello

PASSWORD = "a-long-enough-account-password"
ORIGIN = "http://localhost:8000"


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    monkeypatch.setenv("ROOT_KEYS", "1:" + "A" * 43 + "=")
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    original = db.local_user_id

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("sessions", "auth_identities", "audit_events",
                          "installation_auth_state"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(text("DELETE FROM users WHERE id <> :id"), {"id": original})
            await session.execute(
                text("UPDATE users SET email = :local, role = 'admin', state = 'active' "
                     "WHERE id = :id"),
                {"local": db.LOCAL_USER_EMAIL, "id": original})
            await session.commit()

    try:
        yield portal_runner
    finally:
        realtime.sessions.clear()
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(clear())
        portal_runner(db.dispose())
        get_settings.cache_clear()


async def _make(email: str, role: str = "user", state: str = "active") -> User:
    async with db.session_factory() as session:
        user = User(id=uuid.uuid4(), email=email, role=role, state=state)
        session.add(user)
        await session.flush()
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                 subject=email.lower(), password_hash=hash_password(PASSWORD)))
        await session.commit()
        return user


def _signed_in(client, email, role="user", state="active"):
    """A client carrying the cookies a browser would send on the upgrade."""
    user = client.portal.call(_make, email, role, state)
    issued = client.portal.call(sessions.mint, user, False)
    client.cookies.set("potocolom_session", issued.token)
    return user, issued


def _open(ws, model_id="sd-sim"):
    ws.send_json({"type": "open", "model_id": model_id,
                  "params": {"prompt": "a red house on a hill"}})


@pytest.mark.db
def test_a_browser_with_no_cookie_never_reaches_the_socket(accounts):
    """Refused before accept, so the handshake fails as HTTP 403 and no
    WebSocket ever exists to spend a GPU slot."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/realtime") as ws:
                _open(ws)
                ws.receive_json()
    assert realtime.sessions == {}


@pytest.mark.db
def test_an_expired_or_forged_cookie_closes_unauthorized(accounts):
    with TestClient(app) as client:
        client.cookies.set("potocolom_session", "not-a-real-session")
        with client.websocket_connect("/api/v1/realtime") as ws:
            message = ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] == realtime.CLOSE_UNAUTHORIZED == 4401
    assert realtime.sessions == {}


@pytest.mark.db
def test_a_viewer_is_refused_before_any_scarce_work(accounts):
    """A viewer may read their own work. Spending a GPU slot is not reading."""
    with TestClient(app) as client:
        _signed_in(client, "viewer@example.com", role="viewer")
        with client.websocket_connect("/api/v1/realtime") as ws:
            message = ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] == realtime.CLOSE_FORBIDDEN == 4403
    assert realtime.sessions == {}


@pytest.mark.db
def test_a_suspended_account_is_refused(accounts):
    """Suspension blocks GPU work, and this socket is nothing but GPU work."""
    with TestClient(app) as client:
        _signed_in(client, "paused@example.com", state="suspended")
        with client.websocket_connect("/api/v1/realtime") as ws:
            message = ws.receive_json()
    assert message["code"] == realtime.CLOSE_FORBIDDEN


@pytest.mark.db
@pytest.mark.parametrize("role", ["user", "admin"])
def test_a_signed_in_account_opens_a_session_bound_to_itself(accounts, role):
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, _ = _signed_in(client, f"{role}-rt@example.com", role=role)
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id=f"w-rt-{role}", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"
                # The identity is the server's, never the browser's payload.
                assert _only_session().user_id == user.id
                assert _only_session().user_id != db.local_user_id


def _only_session() -> realtime.Session:
    return next(iter(realtime.sessions.values()))


@pytest.mark.db
def test_revoking_the_account_session_closes_the_live_socket(accounts):
    """Logout, revocation, disable, deletion and role change all end here: the
    socket binds once, so an explicit close is what makes revocation real."""
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, issued = _signed_in(client, "revoked@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-revoke", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                resolved = client.portal.call(sessions.resolve, issued.token)
                client.portal.call(sessions.revoke, resolved.session.id)

                closing = browser_ws.receive_json()
                assert closing["type"] == "error"
                assert closing["code"] == realtime.CLOSE_UNAUTHORIZED


@pytest.mark.db
def test_a_role_change_closes_the_sockets_it_revokes(accounts):
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, _ = _signed_in(client, "promoted@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-role", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                client.portal.call(sessions.revoke_all, user.id)

                assert browser_ws.receive_json()["code"] == realtime.CLOSE_UNAUTHORIZED


def test_none_mode_still_needs_no_credential():
    """The shipped default must keep working exactly as it did."""
    assert get_settings().auth_mode == "none"
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/realtime") as ws:
            ws.send_json({"type": "open", "model_id": "does-not-exist"})
            assert ws.receive_json()["code"] == 4004


@pytest.mark.db
def test_a_session_revoked_during_the_handshake_never_opens(accounts):
    """The socket binds once, so a revocation that lands between resolving the
    cookie and registering the session would otherwise be missed by the close
    sweep and the socket would outlive its credential for good.

    The server is blocked awaiting the open message while the revoke commits,
    which is exactly that window.
    """
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, issued = _signed_in(client, "raced@example.com")
        resolved = client.portal.call(sessions.resolve, issued.token)
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-race", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                client.portal.call(sessions.revoke, resolved.session.id)
                _open(browser_ws)
                message = browser_ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] == realtime.CLOSE_UNAUTHORIZED
    assert realtime.sessions == {}
