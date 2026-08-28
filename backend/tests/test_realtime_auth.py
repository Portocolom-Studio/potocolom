import asyncio
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
@pytest.mark.parametrize("state", ["suspended", "disabled", "deletion_pending", "purging"])
def test_an_account_that_is_not_active_is_refused(accounts, state):
    """Suspension blocks GPU work, and this socket is nothing but GPU work.
    The others cannot sign in at all."""
    with TestClient(app) as client:
        _signed_in(client, f"{state}@example.com", state=state)
        with client.websocket_connect("/api/v1/realtime") as ws:
            message = ws.receive_json()
    assert message["code"] == realtime.CLOSE_FORBIDDEN


@pytest.mark.db
def test_a_handshake_survives_a_database_that_is_down(accounts, monkeypatch):
    """The none branch tolerates a down database. This one must be equally
    defined: a refusal, not a traceback on every reconnect."""
    from app import sessions as account_sessions

    async def broken(_token):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(account_sessions, "resolve", broken)
    with TestClient(app) as client:
        client.cookies.set("potocolom_session", "anything")
        with client.websocket_connect("/api/v1/realtime") as ws:
            assert ws.receive_json()["code"] == realtime.CLOSE_UNAUTHORIZED
    assert realtime.sessions == {}


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
                worker = _only_session().worker
                client.portal.call(sessions.revoke, resolved.session.id)

                closing = browser_ws.receive_json()
                assert closing["type"] == "error"
                assert closing["code"] == realtime.CLOSE_UNAUTHORIZED
                # The slot goes with it. A revoked account holding a GPU slot
                # until its transport happens to die is the thing revocation
                # exists to prevent.
                assert realtime.sessions == {}
                assert worker.slots_in_use == 0


@pytest.mark.db
def test_a_worker_that_stopped_reading_does_not_hold_up_a_revocation(accounts):
    """The slot is handed back before the worker is told it is gone, so the
    notification is a courtesy. Waiting on it without a bound would leave the
    revoked browser drawing for as long as that worker holds the connection."""
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, issued = _signed_in(client, "wedged@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-wedged", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                resolved = client.portal.call(sessions.resolve, issued.token)
                worker = _only_session().worker
                assert worker is not None
                never = asyncio.Event()

                async def wedged(_message: dict) -> None:
                    await never.wait()

                worker.ws.send_json = wedged
                client.portal.call(sessions.revoke, resolved.session.id)

                closing = browser_ws.receive_json()
                assert closing["type"] == "error"
                assert closing["code"] == realtime.CLOSE_UNAUTHORIZED
                assert realtime.sessions == {}
                assert worker.slots_in_use == 0


@pytest.mark.db
def test_demoting_an_account_closes_the_socket_it_is_drawing_on(accounts):
    """Through the real endpoint, not through the helper it happens to call.

    A demotion to viewer exists precisely to take the slot away. The account
    binds its principal once, so without an explicit close it keeps relaying
    frames and keeps metering for as long as it holds the connection.
    """
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        target, _ = _signed_in(client, "demoted@example.com")
        boss = client.portal.call(_make, "boss-rt@example.com", "admin")
        # The admin acts by bearer, so the browser's cookie jar stays the
        # target's and the socket below is opened as the target.
        admin_token = client.portal.call(sessions.mint, boss, False, True).token
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-demote", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                demoted = client.post(
                    f"/api/v1/users/{target.id}/role",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"role": "viewer"})
                assert demoted.status_code == 204

                closing = browser_ws.receive_json()
                assert closing["type"] == "error"
                assert closing["code"] == realtime.CLOSE_UNAUTHORIZED


@pytest.mark.db
def test_one_unread_socket_does_not_keep_the_others_alive(accounts):
    """refuse writes before it closes, and a browser that stopped reading
    blocks that write with no timeout. Sequentially, the first such socket
    would strand every other socket on the account."""
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, issued = _signed_in(client, "stalled@example.com")
        resolved = client.portal.call(sessions.resolve, issued.token)

        stalled = realtime.Session(id=uuid.uuid4(), model_id="sd-sim",
                                   browser=_NeverDrains(), user_id=user.id,
                                   auth_session_id=resolved.session.id)
        realtime.sessions[stalled.id] = stalled
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-stall", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                client.portal.call(sessions.revoke, resolved.session.id)

                assert browser_ws.receive_json()["code"] == realtime.CLOSE_UNAUTHORIZED
                # Including the one whose transport ate the close.
                assert stalled.id not in realtime.sessions
        realtime.sessions.pop(stalled.id, None)


class _NeverDrains:
    """A browser that accepted the socket and then stopped reading."""

    async def send_json(self, _payload) -> None:
        await asyncio.Event().wait()

    async def close(self, **_kwargs) -> None:
        await asyncio.Event().wait()


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


async def _revoke_out_of_band(session_id: uuid.UUID) -> None:
    """Revoke the row the way another process does, and nothing else.

    sessions.revoke() closes the socket itself, in this process, so a test
    built on it passes with the sweep deleted and proves nothing about a
    revocation performed by the operator command, direct SQL or a replica.
    """
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id = :id"),
            {"id": session_id})
        await session.commit()


@pytest.mark.db
def test_a_session_revoked_in_another_process_is_swept_off_the_socket(accounts):
    """Nothing outside this process can reach the socket map that close_revoked
    walks, so a revocation performed anywhere else only lands if the process
    holding the socket asks the database which of its sessions are still live.
    """
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        _signed_in(client, "swept@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-sweep", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                worker = _only_session().worker
                client.portal.call(_revoke_out_of_band, _only_session().auth_session_id)
                client.portal.call(realtime.close_dead_sessions)

                closing = browser_ws.receive_json()
                assert closing["type"] == "error"
                assert closing["code"] == realtime.CLOSE_UNAUTHORIZED
                # The slot goes back with it, the same as an in-process revocation.
                assert realtime.sessions == {}
                assert worker.slots_in_use == 0


@pytest.mark.db
def test_a_sweep_leaves_a_live_session_drawing(accounts):
    """Without this, a sweep that closed every socket unconditionally would
    still satisfy the test above."""
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, _ = _signed_in(client, "kept@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-kept", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                worker = _only_session().worker
                client.portal.call(realtime.close_dead_sessions)

                assert _only_session().user_id == user.id
                assert worker.slots_in_use == 1
                # Still the socket it was: it answers, rather than having been
                # closed with nobody reading the close.
                browser_ws.send_json({"type": "update_params",
                                      "params": {"prompt": "a blue house on a hill"}})
                assert browser_ws.receive_json()["type"] == "params_updated"


@pytest.mark.db
def test_a_sweep_that_cannot_reach_the_database_keeps_live_sockets(accounts, monkeypatch):
    """_still_live fails closed for the one socket it is about to grant a slot
    to. The sweep judges every socket on the installation at once, so failing
    closed would sign everybody out of a live canvas on one database blip."""
    from app import sessions as account_sessions

    async def broken(_session_ids):
        raise RuntimeError("database unavailable")

    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        user, _ = _signed_in(client, "blipped@example.com")
        with client.websocket_connect("/api/v1/fleet") as worker_ws:
            worker_ws.send_json(hello(worker_id="w-rt-blip", parameters=REQUIRES_PROMPT))
            assert worker_ws.receive_json()["type"] == "registered"
            with client.websocket_connect("/api/v1/realtime") as browser_ws:
                _open(browser_ws)
                answer_ready(worker_ws, worker_ws.receive_json())
                assert browser_ws.receive_json()["type"] == "ready"

                worker = _only_session().worker
                monkeypatch.setattr(account_sessions, "live_among", broken)
                client.portal.call(realtime.close_dead_sessions)

                assert _only_session().user_id == user.id
                assert worker.slots_in_use == 1


async def _settle() -> None:
    await asyncio.sleep(0.05)


@pytest.mark.db
def test_the_running_app_runs_the_sweep(accounts, monkeypatch):
    """The sweep is a fix only if something runs it, on its own.

    Every test above calls close_dead_sessions by hand, so both the line in
    the lifespan that schedules the loop and the line in the loop that calls
    it could go with all of them still green and no socket ever swept. This
    drives the scheduled loop itself and waits for it to come round.
    """
    swept: list[int] = []

    async def counting() -> None:
        swept.append(1)

    # Before the client starts, because the loop reads both of these when the
    # lifespan creates it.
    monkeypatch.setattr(realtime, "SESSION_SWEEP_SECONDS", 0.05)
    monkeypatch.setattr(realtime, "close_dead_sessions", counting)
    with TestClient(app, client=("127.0.0.1", 50000), headers=FLEET_HEADERS) as client:
        for _ in range(100):
            if swept:
                break
            client.portal.call(_settle)
    assert swept, "the scheduled loop never called close_dead_sessions"
