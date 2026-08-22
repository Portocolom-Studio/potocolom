import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from starlette.websockets import WebSocketDisconnect

from app import audit, db, enable
from app.main import app
from app.passwords import verify_password
from app.settings import get_settings
from app.tables import AuthIdentity, AuthToken, Session, User

PASSWORD = "a-long-enough-first-admin-password"
EMAIL = "Owner@Example.com"
ROOT_KEYS = "1:" + "A" * 43 + "="


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    """An installation that has enabled accounts, as make auth-enable leaves it.

    The order matters and is the real one: an install records that it enabled
    accounts while still running in none mode, and only then restarts into
    accounts mode, because the startup guard refuses the other way round.
    """
    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())

    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    # A claim renames this row, so a later connect no longer finds
    # local@localhost and makes a second one. Put the original back by id.
    original_id = db.local_user_id

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM auth_tokens"))
            await session.execute(text("DELETE FROM auth_identities"))
            await session.execute(text("DELETE FROM sessions"))
            await session.execute(text("DELETE FROM audit_events"))
            await session.execute(text("DELETE FROM installation_auth_state"))
            await session.execute(text("DELETE FROM users WHERE id <> :id"),
                                  {"id": original_id})
            await session.execute(
                text("UPDATE users SET email = :local, role = 'admin', state = 'active', "
                     "mail_verified = false WHERE id = :id"),
                {"local": db.LOCAL_USER_EMAIL, "id": original_id},
            )
            await session.commit()

    try:
        yield portal_runner
    finally:
        # Still in accounts mode here: monkeypatch unwinds after this fixture,
        # and reconnecting in none mode would hit the one-way guard, which is
        # the guard working. The clear below is what releases the install.
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(clear())
        portal_runner(db.dispose())
        get_settings.cache_clear()


async def _user() -> User:
    async with db.session_factory() as session:
        return (await session.execute(
            select(User).where(User.id == db.local_user_id)
        )).scalar_one()


async def _identities() -> list[AuthIdentity]:
    async with db.session_factory() as session:
        return list((await session.execute(select(AuthIdentity))).scalars().all())


async def _tokens() -> list[AuthToken]:
    async with db.session_factory() as session:
        return list((await session.execute(select(AuthToken))).scalars().all())


async def _sessions() -> list[Session]:
    async with db.session_factory() as session:
        return list((await session.execute(select(Session))).scalars().all())


@pytest.mark.db
def test_the_setup_link_is_one_use_and_lasts_an_hour(accounts):
    before = datetime.now(timezone.utc)
    token = accounts(enable.mint_setup_token())
    stored = accounts(_tokens())
    assert len(stored) == 1 and stored[0].purpose == "setup"
    assert stored[0].user_id is None
    assert stored[0].consumed_at is None
    assert timedelta(minutes=59) < stored[0].expires_at - before < timedelta(minutes=61)
    # Only the hash is durable; the token exists in the operator's terminal.
    assert token not in str(stored[0].token_hash)


@pytest.mark.db
def test_minting_again_invalidates_the_link_it_replaces(accounts):
    first = accounts(enable.mint_setup_token())
    second = accounts(enable.mint_setup_token())
    assert first != second
    with TestClient(app) as client:
        assert _claim(client, first).status_code == 403
        assert _claim(client, second).status_code == 204


def _claim(client, token, email=EMAIL, password=PASSWORD):
    return client.post("/api/v1/auth/setup",
                       json={"token": token, "email": email, "password": password})


@pytest.mark.db
def test_the_first_claimant_adopts_the_implicit_account(accounts):
    original = accounts(_user()).id
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 204
        adopted = client.portal.call(_user)
        identities = client.portal.call(_identities)
    # The UUID is the whole point: every job and asset the implicit user owns
    # belongs to the administrator who claims the install.
    assert adopted.id == original
    assert adopted.email == EMAIL
    assert adopted.role == "admin"
    assert adopted.state == "active"
    assert adopted.mail_verified is False
    assert len(identities) == 1
    assert identities[0].provider == "password"
    assert identities[0].user_id == original
    assert verify_password(identities[0].password_hash, PASSWORD) is True


@pytest.mark.db
def test_a_claimed_link_cannot_be_replayed(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 204
        assert _claim(client, token, email="second@example.com").status_code == 403
        assert client.portal.call(_user).email == EMAIL
        assert len(client.portal.call(_identities)) == 1


@pytest.mark.db
def test_only_one_of_two_concurrent_claims_wins(accounts):
    """Two browsers on the same link must not both become the administrator."""
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        async def race():
            return await asyncio.gather(
                enable.claim(token, "first@example.com", PASSWORD),
                enable.claim(token, "second@example.com", PASSWORD),
                return_exceptions=True,
            )

        results = client.portal.call(race)
        won = [result for result in results if not isinstance(result, Exception)]
        assert len(won) == 1
        assert len(client.portal.call(_identities)) == 1
        assert client.portal.call(_user).email in {"first@example.com", "second@example.com"}


@pytest.mark.db
def test_an_expired_link_is_refused(accounts):
    token = accounts(enable.mint_setup_token())

    async def age_it() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("UPDATE auth_tokens SET expires_at = :past"),
                {"past": datetime.now(timezone.utc) - timedelta(minutes=1)},
            )
            await session.commit()

    accounts(age_it())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 403


@pytest.mark.db
def test_an_unknown_link_is_refused_the_same_way_as_a_spent_one(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        unknown = _claim(client, "not-a-real-token")
        assert _claim(client, token).status_code == 204
        spent = _claim(client, token)
    assert unknown.status_code == spent.status_code == 403
    assert unknown.json() == spent.json()


@pytest.mark.db
def test_a_weak_password_is_refused_and_claims_nothing(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token, password="short").status_code == 400
        assert client.portal.call(_identities) == []
        assert client.portal.call(_user).email == db.LOCAL_USER_EMAIL
        # The link survives a rejected password; the operator gets to retry.
        assert _claim(client, token).status_code == 204


@pytest.mark.db
def test_setup_grants_a_clean_session_and_no_recent_authentication(accounts):
    """Setup proves a capability, not a person. It signs the claimant in, and
    withholds the recent-authentication window that guards credential
    changes."""
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        claimed = _claim(client, token)
        assert claimed.status_code == 204
        assert any(header.startswith("potocolom_session=")
                   for header in claimed.headers.get_list("set-cookie"))
        live = client.portal.call(_sessions)
        assert len(live) == 1
        assert live[0].recent_auth_at is None
        assert live[0].remember_me is False


@pytest.mark.db
def test_nothing_secret_reaches_the_log_or_the_audit(accounts, caplog):
    token = accounts(enable.mint_setup_token())
    with caplog.at_level("DEBUG"):
        with TestClient(app) as client:
            assert _claim(client, token).status_code == 204
            recorded = client.portal.call(audit_actions)
    assert token not in caplog.text
    assert PASSWORD not in caplog.text
    assert "POST /api/v1/auth/setup" in recorded


async def audit_actions() -> list[str]:
    return [row["action"] for row in await audit.search()]


@pytest.mark.db
def test_the_claim_is_audited_against_the_account_it_created(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 204
        rows = client.portal.call(audit.search)
        # dispose clears local_user_id, so read it while the client is live.
        adopted = str(db.local_user_id)
    setup = [row for row in rows if row["action"] == "POST /api/v1/auth/setup"]
    assert len(setup) == 1
    assert setup[0]["target_user_id"] == adopted


@pytest.mark.db
def test_accounts_mode_authenticates_nobody_through_the_implicit_admin(accounts):
    """The catastrophe this guards: accounts on, no login yet, and every
    request still resolving to the implicit local administrator."""
    with TestClient(app) as client:
        assert client.get("/api/v1/telemetry/preview").status_code == 401
        assert client.get("/api/v1/models").status_code == 401


@pytest.mark.db
def test_setup_is_refused_once_the_installation_is_claimed(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 204
        # A fresh link is valid, and still refused: the installation has an
        # owner, and a second setup would hand it to someone else.
        second = client.portal.call(enable.mint_setup_token)
        assert _claim(client, second, email="later@example.com").status_code == 409


@pytest.mark.db
def test_the_realtime_socket_refuses_accounts_mode(accounts):
    """Its only gate is the Origin header, which a non-browser client simply
    omits, so in accounts mode it would drive the GPU and record the work
    against whatever row the implicit administrator points at."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/realtime") as socket:
                socket.receive_json()


@pytest.mark.db
def test_a_restart_after_the_claim_creates_no_second_administrator(accounts):
    """The claim renames the implicit row, so a startup that recreates it by
    email would leave a standing admin nobody claimed."""
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        assert _claim(client, token).status_code == 204
        claimed_id = db.local_user_id

    async def everyone() -> list[tuple]:
        async with db.session_factory() as session:
            return list((await session.execute(
                text("SELECT id, email, role FROM users ORDER BY created_at")
            )).all())

    accounts(db.connect())
    rows = accounts(everyone())
    assert len(rows) == 1
    assert rows[0][0] == claimed_id
    assert rows[0][1] == EMAIL


@pytest.mark.db
def test_a_rejected_body_never_echoes_the_password(accounts):
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/setup",
                               json={"email": "a@b.co", "password": PASSWORD})
    assert response.status_code == 422
    assert PASSWORD not in response.text


@pytest.mark.db
def test_a_padded_password_cannot_walk_past_the_length_cap(accounts):
    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        padded = " " * 100000 + PASSWORD + " " * 100000
        assert _claim(client, token, password=padded).status_code == 400


def test_setup_answers_nothing_when_accounts_are_off(monkeypatch):
    """An install that never enabled accounts has no link to claim it with."""
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/setup",
                               json={"token": "x", "email": "a@b.co", "password": PASSWORD})
    assert response.status_code == 404


@pytest.mark.db
def test_a_clean_install_can_be_claimed(portal_runner, monkeypatch):
    """An install whose API never ran in none mode has no implicit user, and
    the enable tool skips the startup path that would create one. Without it
    the setup call answers 503 and the installation can never be claimed."""
    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    get_settings.cache_clear()
    assert portal_runner(db.connect(serving=False)) is True

    async def wipe() -> None:
        async with db.session_factory() as session:
            for table in ("auth_tokens", "auth_identities", "sessions", "audit_events",
                          "installation_auth_state"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(text("DELETE FROM users"))
            await session.commit()

    portal_runner(wipe())
    portal_runner(db.dispose())
    token = portal_runner(enable._enable())

    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert _claim(client, token).status_code == 204
            assert client.portal.call(_user).email == EMAIL
    finally:
        # serving=False: the install is still marked accounts here, and the
        # startup guard would refuse a none-mode connect until wipe clears it.
        portal_runner(db.connect(serving=False))
        portal_runner(wipe())
        portal_runner(db.dispose())
        monkeypatch.delenv("AUTH_MODE", raising=False)
        get_settings.cache_clear()
