import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db, sessions
from app.main import app
from app.passwords import hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, User

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
            for table in ("sessions", "auth_identities", "auth_tokens", "audit_events",
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
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(clear())
        portal_runner(db.dispose())
        get_settings.cache_clear()


def _account(portal_runner, email, role="user", state="active", password=PASSWORD) -> User:
    async def go():
        async with db.session_factory() as session:
            user = User(id=uuid.uuid4(), email=email, role=role, state=state)
            session.add(user)
            await session.flush()
            session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                     subject=email.lower(), password_hash=hash_password(password)))
            await session.commit()
            return user

    return portal_runner(go())


def _login(client, email, password=PASSWORD, remember_me=False):
    return client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                       json={"email": email, "password": password, "remember_me": remember_me})


@pytest.mark.db
def test_a_password_login_sets_a_session_and_a_readable_csrf_cookie(accounts):
    _account(accounts, "member@example.com")
    with TestClient(app) as client:
        response = _login(client, "member@example.com")
        assert response.status_code == 204
        jar = {cookie.name: cookie for cookie in client.cookies.jar}
        assert "potocolom_session" in jar and "potocolom_csrf" in jar
        raw = response.headers.get_list("set-cookie")
        session_header = next(h for h in raw if h.startswith("potocolom_session="))
        csrf_header = next(h for h in raw if h.startswith("potocolom_csrf="))
        assert "HttpOnly" in session_header
        assert "samesite=lax" in session_header.lower()
        assert "Path=/" in session_header
        # Plain HTTP: a Secure cookie would simply be dropped by the browser.
        assert "Secure" not in session_header
        # The browser has to read the CSRF value to echo it back.
        assert "HttpOnly" not in csrf_header


@pytest.mark.db
def test_the_session_cookie_authenticates_a_later_request(accounts):
    _account(accounts, "member2@example.com")
    with TestClient(app) as client:
        assert _login(client, "member2@example.com").status_code == 204
        me = client.get("/api/v1/account")
        assert me.status_code == 200
        assert me.json()["email"] == "member2@example.com"
        assert me.json()["role"] == "user"


@pytest.mark.db
def test_without_a_cookie_nothing_authenticates(accounts):
    with TestClient(app) as client:
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_an_unknown_address_and_a_wrong_password_answer_identically(accounts):
    _account(accounts, "real@example.com")
    with TestClient(app) as client:
        unknown = _login(client, "ghost@example.com")
        wrong = _login(client, "real@example.com", password="a-different-long-password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.db
def test_a_login_is_case_insensitive_about_the_address(accounts):
    _account(accounts, "mixed@example.com")
    with TestClient(app) as client:
        assert _login(client, "  MiXeD@Example.COM ").status_code == 204


@pytest.mark.db
@pytest.mark.parametrize("state", ["disabled", "deletion_pending", "purging"])
def test_an_account_that_cannot_sign_in_is_refused(accounts, state):
    _account(accounts, f"{state}@example.com", state=state)
    with TestClient(app) as client:
        assert _login(client, f"{state}@example.com").status_code == 401


@pytest.mark.db
def test_a_suspended_account_signs_in_read_only(accounts):
    """Suspended is a pause, not a deletion: they may read their own work and
    settle their account, and may change nothing."""
    _account(accounts, "paused@example.com", state="suspended")
    with TestClient(app) as client:
        assert _login(client, "paused@example.com").status_code == 204
        assert client.get("/api/v1/account").status_code == 200
        blocked = client.post("/api/v1/generations", headers=_csrf(client), json={})
        assert blocked.status_code == 403


def _csrf(client) -> dict:
    value = next(c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf"))
    return {"Origin": ORIGIN, "X-CSRF-Token": value}


@pytest.mark.db
def test_an_unsafe_cookie_request_needs_the_csrf_header(accounts):
    _account(accounts, "csrf@example.com")
    with TestClient(app) as client:
        assert _login(client, "csrf@example.com").status_code == 204
        assert client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
        wrong = client.post("/api/v1/auth/logout",
                            headers={"Origin": ORIGIN, "X-CSRF-Token": "not-the-value"})
        assert wrong.status_code == 403
        assert client.post("/api/v1/auth/logout", headers=_csrf(client)).status_code == 204


@pytest.mark.db
def test_an_unsafe_cookie_request_needs_an_exact_origin(accounts):
    _account(accounts, "origin@example.com")
    with TestClient(app) as client:
        assert _login(client, "origin@example.com").status_code == 204
        headers = _csrf(client)
        for forged in ("http://evil.example", ORIGIN + ".evil.example", "null"):
            assert client.post("/api/v1/auth/logout",
                               headers={**headers, "Origin": forged}).status_code == 403


@pytest.mark.db
def test_a_safe_request_needs_no_csrf(accounts):
    _account(accounts, "safe@example.com")
    with TestClient(app) as client:
        assert _login(client, "safe@example.com").status_code == 204
        assert client.get("/api/v1/account").status_code == 200


@pytest.mark.db
def test_a_bearer_credential_wins_and_never_falls_back_to_the_cookie(accounts):
    """A caller who presents a bearer is making a claim about who they are.
    Falling back would let a stolen page borrow the browser's cookie."""
    user = _account(accounts, "bearer@example.com")
    with TestClient(app) as client:
        assert _login(client, "bearer@example.com").status_code == 204
        issued = client.portal.call(sessions.mint, user, False)
        good = client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {issued.token}"})
        assert good.status_code == 200
        bad = client.get("/api/v1/account", headers={"Authorization": "Bearer nonsense"})
        assert bad.status_code == 401


@pytest.mark.db
def test_a_bearer_request_needs_no_csrf_header(accounts):
    user = _account(accounts, "bearer2@example.com")
    with TestClient(app) as client:
        issued = client.portal.call(sessions.mint, user, False)
        assert client.post("/api/v1/auth/logout",
                           headers={"Authorization": f"Bearer {issued.token}"}).status_code == 204


@pytest.mark.db
def test_logging_out_kills_the_session_it_used(accounts):
    _account(accounts, "bye@example.com")
    with TestClient(app) as client:
        assert _login(client, "bye@example.com").status_code == 204
        assert client.post("/api/v1/auth/logout", headers=_csrf(client)).status_code == 204
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_an_account_lists_and_revokes_its_other_sessions(accounts):
    user = _account(accounts, "many@example.com")
    with TestClient(app) as client:
        assert _login(client, "many@example.com").status_code == 204
        other = client.portal.call(sessions.mint, user, False)
        listed = client.get("/api/v1/account").json()["sessions"]
        assert len(listed) == 2
        target = next(row for row in listed if not row["current"])
        assert client.delete(f"/api/v1/account/sessions/{target['id']}",
                             headers=_csrf(client)).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {other.token}"}).status_code == 401


@pytest.mark.db
def test_one_account_cannot_revoke_another_account_session(accounts):
    victim = _account(accounts, "victim@example.com")
    _account(accounts, "attacker@example.com")
    with TestClient(app) as client:
        theirs = client.portal.call(sessions.mint, victim, False)
        theirs_id = client.portal.call(_session_id, theirs.token)
        assert _login(client, "attacker@example.com").status_code == 204
        assert client.delete(f"/api/v1/account/sessions/{theirs_id}",
                             headers=_csrf(client)).status_code == 404
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {theirs.token}"}).status_code == 200


async def _session_id(token: str):
    resolved = await sessions.resolve(token)
    return str(resolved.session.id)


@pytest.mark.db
def test_a_login_rotates_onto_a_new_session(accounts):
    """A session that existed before authentication must not survive it."""
    user = _account(accounts, "rotate@example.com")
    with TestClient(app) as client:
        stale = client.portal.call(sessions.mint, user, False)
        assert _login(client, "rotate@example.com").status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {stale.token}"}).status_code == 401


@pytest.mark.db
def test_claiming_the_installation_hands_back_a_session_with_no_recent_auth(accounts):
    from app import enable

    token = accounts(enable.mint_setup_token())
    with TestClient(app) as client:
        claimed = client.post("/api/v1/auth/setup", headers={"Origin": ORIGIN}, json={
            "token": token, "email": "owner@example.com", "password": PASSWORD})
        assert claimed.status_code == 204
        assert client.get("/api/v1/account").status_code == 200
        current = client.get("/api/v1/account").json()
        assert current["recent_auth"] is False


@pytest.mark.db
def test_a_password_login_grants_recent_authentication(accounts):
    _account(accounts, "recent@example.com")
    with TestClient(app) as client:
        assert _login(client, "recent@example.com").status_code == 204
        assert client.get("/api/v1/account").json()["recent_auth"] is True


@pytest.mark.db
def test_the_host_prefix_appears_when_the_install_is_served_over_https(accounts, monkeypatch):
    _account(accounts, "secure@example.com")
    monkeypatch.setenv("PUBLIC_URL", "https://studio.example.com")
    get_settings.cache_clear()
    with TestClient(app, base_url="https://studio.example.com") as client:
        response = client.post("/api/v1/auth/login",
                               headers={"Origin": "https://studio.example.com"},
                               json={"email": "secure@example.com", "password": PASSWORD,
                                     "remember_me": False})
        assert response.status_code == 204
        header = next(h for h in response.headers.get_list("set-cookie")
                      if h.startswith("__Host-potocolom_session="))
        assert "Secure" in header and "HttpOnly" in header and "Path=/" in header
        assert "Domain" not in header
