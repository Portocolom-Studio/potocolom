"""What the plan promises at the edges: a second process, a store that is
down, a socket that outlives its session, and an audit record that must not
disappear quietly."""

import asyncio
from datetime import datetime, timezone
import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import audit, db, sessions
from app.main import app
from app.oauth import check_configuration
from app.settings import Settings, get_settings
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]


@pytest.mark.db
def test_a_second_accounts_process_refuses_to_start(accounts):
    """Without Redis two processes do not fail loudly, they disagree quietly
    about who owns a socket, so the second one has to refuse here."""

    async def hold() -> asyncpg.Connection:
        connection = await asyncpg.connect(db.get_settings().database_url)
        assert await connection.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", db.ACCOUNTS_STARTUP_LOCK_KEY)
        return connection

    async def release(connection: asyncpg.Connection) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_unlock($1::bigint)", db.ACCOUNTS_STARTUP_LOCK_KEY)
        await connection.close()

    other = accounts(hold())
    try:
        with pytest.raises(RuntimeError, match="another accounts startup"):
            with TestClient(app, base_url=ORIGIN):
                pass
    finally:
        accounts(release(other))
    # And starts once the first one is gone.
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.db
def test_an_account_route_answers_503_while_the_store_is_down(accounts, monkeypatch):
    """The contract is 503 and no principal constructed. Resolving a session
    against a store that is not there raises, which the caller reads as a
    fault of their request."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "outage@example.com")
        assert _login(client, "outage@example.com").status_code == 204
        assert client.get("/api/v1/account").status_code == 200
        resolved: list = []
        original = sessions.resolve

        async def watch(token):
            resolved.append(token)
            return await original(token)

        monkeypatch.setattr(sessions, "resolve", watch)
        factory, db.session_factory = db.session_factory, None
        try:
            answered = client.get("/api/v1/account")
        finally:
            db.session_factory = factory
    assert answered.status_code == 503
    # The other half of the contract: no principal is constructed either.
    assert resolved == []


@pytest.mark.db
def test_the_outbox_has_a_route_an_operator_can_read(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "operator@example.com", "admin")
        assert _login(client, "operator@example.com").status_code == 204
        answered = client.get("/api/v1/mail/status")
        assert answered.status_code == 200
        assert set(answered.json()) >= {"backend", "sent", "pending", "failed"}

        client.portal.call(_make, "bystander@example.com")
    with TestClient(app, base_url=ORIGIN) as other:
        assert _login(other, "bystander@example.com").status_code == 204
        assert other.get("/api/v1/mail/status").status_code == 403


@pytest.mark.parametrize("public_url", ["http://studio.example.com", "https://", "ftp://x"])
def test_oauth_over_plain_http_refuses_to_start(public_url):
    """The authorization code comes back on the redirect URI. In the clear,
    anyone on the path can spend it before the browser does."""
    with pytest.raises(RuntimeError, match="https"):
        check_configuration(Settings(
            auth_mode="accounts", public_url=public_url, oauth_providers="google",
            google_client_id="id", google_client_secret="secret"))


def test_an_install_offering_oauth_over_plain_http_does_not_boot(monkeypatch):
    """Through the lifespan, not the helper: a check nothing calls refuses
    nothing."""
    monkeypatch.setenv("PUBLIC_URL", "http://studio.example.com")
    monkeypatch.setenv("OAUTH_PROVIDERS", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="https"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_oauth_starts_on_https_and_on_the_dev_loop():
    for public_url in ("https://studio.example.com", "http://localhost:8000"):
        check_configuration(Settings(
            auth_mode="accounts", public_url=public_url, oauth_providers="google",
            google_client_id="id", google_client_secret="secret"))
    # No provider configured, so the scheme is nobody's business here.
    check_configuration(Settings(auth_mode="accounts", public_url="http://studio.example.com"))


@pytest.mark.db
def test_a_record_spooled_during_an_insert_is_not_cleared_by_it(accounts):
    """The spool exists for the outage that makes an insert slow. A record
    that gave up waiting behind one must not vanish when that one succeeds:
    silent loss is the single outcome the audit contract forbids.

    The first record is driven through the locked path directly so it cannot
    time out; the second goes through the public one with the timeout at zero,
    which is the interleaving without the clock.
    """
    released = asyncio.Event()
    inserted: list[list] = []

    async def slow_insert(events):
        inserted.append(list(events))
        await released.wait()

    async def exercise() -> None:
        audit._spool.clear()
        audit._fell_back = 0
        audit._dropped = 0
        first = asyncio.create_task(audit._locked(_pending("first.action")))
        await asyncio.sleep(0)
        audit.DELIVERY_TIMEOUT = 0
        await audit.record("second.action")
        released.set()
        await first

    original_insert, original_timeout = audit._insert, audit.DELIVERY_TIMEOUT
    audit._insert = slow_insert
    try:
        accounts(exercise())
        spooled = [event.action for event in audit._spool]
        fell_back = audit._fell_back
    finally:
        audit._insert, audit.DELIVERY_TIMEOUT = original_insert, original_timeout
        audit._spool.clear()
        audit._fell_back = 0

    assert [event.action for event in inserted[0]] == ["first.action"]
    assert spooled == ["second.action"], "the record that gave up was cleared by the insert"
    # And the count that makes the gap visible in the seven-day summary.
    assert fell_back == 1


@pytest.mark.db
def test_a_flush_clears_what_it_carried_and_keeps_what_arrived_behind_it(accounts):
    """The counts go down by what the markers reported, not to zero: a record
    that fell back while the flush was in flight is still a gap nobody has
    seen, and zeroing the count is how it stops being visible."""
    released = asyncio.Event()
    inserted: list[list] = []

    async def slow_insert(events):
        inserted.append(list(events))
        await released.wait()

    async def exercise() -> None:
        audit._spool.clear()
        audit._spool.append(_pending("waiting.since.the.outage"))
        audit._fell_back = 1
        audit._dropped = 4
        flushing = asyncio.create_task(audit._locked(_pending("the.flush")))
        await asyncio.sleep(0)
        audit.DELIVERY_TIMEOUT = 0
        await audit.record("arrived.behind.it")
        released.set()
        await flushing

    original_insert, original_timeout = audit._insert, audit.DELIVERY_TIMEOUT
    audit._insert = slow_insert
    try:
        accounts(exercise())
        spooled = [event.action for event in audit._spool]
        fell_back, dropped = audit._fell_back, audit._dropped
    finally:
        audit._insert, audit.DELIVERY_TIMEOUT = original_insert, original_timeout
        audit._spool.clear()
        audit._fell_back = audit._dropped = 0

    carried = [event.action for event in inserted[0]]
    assert carried[:2] == ["waiting.since.the.outage", "the.flush"]
    assert {"audit.fallback", "audit.overflow"} <= set(carried), carried
    assert spooled == ["arrived.behind.it"]
    assert (fell_back, dropped) == (1, 0)


def _pending(action: str) -> audit.Pending:
    return audit.Pending(action=action, occurred_at=datetime.now(timezone.utc))


@pytest.mark.db
def test_a_password_reset_closes_the_sockets_the_stolen_session_held(accounts, monkeypatch):
    """The row is revoked either way. The socket bound its principal at the
    handshake, so nothing reaches it unless somebody says so."""
    from app import recovery

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "reset@example.com")
        token = client.portal.call(recovery.mint_reset, "reset@example.com")
        closed = _watch_closures(monkeypatch)
        done = client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": "a-brand-new-long-enough-password"})
        assert done.status_code == 204
    assert closed == [(user.id, None, None)]


@pytest.mark.db
def test_a_password_change_closes_every_other_socket_and_keeps_its_own(accounts, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "evictor@example.com")
        assert _login(client, "evictor@example.com").status_code == 204
        closed = _watch_closures(monkeypatch)
        changed = client.post("/api/v1/account/password", headers=_csrf(client),
                              json={"current_password": "a-long-enough-account-password",
                                    "password": "a-different-long-enough-password"})
        assert changed.status_code == 204
        principal = client.portal.call(
            sessions.resolve,
            next(c.value for c in client.cookies.jar if c.name.endswith("potocolom_session")))
    # Every socket the account holds, minus the one this browser is using.
    assert closed == [(user.id, None, principal.session.id)]


def _watch_closures(monkeypatch) -> list:
    """Through monkeypatch: left in place, this would follow every later test
    in the session into whatever it does with a socket."""
    from app import realtime

    closed: list = []
    original = realtime.close_revoked

    async def record(user_id, auth_session_id=None, keep=None):
        closed.append((user_id, auth_session_id, keep))
        await original(user_id, auth_session_id, keep)

    monkeypatch.setattr(realtime, "close_revoked", record)
    return closed


