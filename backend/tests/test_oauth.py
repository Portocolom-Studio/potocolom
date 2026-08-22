import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, oauth, sessions
from app.main import app
from app.passwords import hash_password
from app.settings import Settings, get_settings
from app.tables import AuthIdentity, OAuthFlow, User

PASSWORD = "a-long-enough-account-password"
ORIGIN = "https://studio.example.com"


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    monkeypatch.setenv("ROOT_KEYS", "1:" + "A" * 43 + "=")
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("OAUTH_PROVIDERS", "google,github")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-secret")
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
            for table in ("oauth_flows", "sessions", "auth_identities", "audit_events",
                          "mail_outbox", "installation_auth_state"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(text("DELETE FROM users WHERE id <> :id"), {"id": original})
            await session.execute(
                text("UPDATE users SET email = :local, role = 'admin', state = 'active', "
                     "mail_verified = false WHERE id = :id"),
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


async def _make(email: str, mail_verified: bool = False) -> User:
    async with db.session_factory() as session:
        user = User(id=uuid.uuid4(), email=email, role="user", mail_verified=mail_verified)
        session.add(user)
        await session.flush()
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                 subject=email.lower(), password_hash=hash_password(PASSWORD)))
        await session.commit()
        return user


async def _link(user_id: uuid.UUID, provider: str, subject: str) -> None:
    async with db.session_factory() as session:
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user_id, provider=provider,
                                 subject=subject))
        await session.commit()


async def _flows() -> list[OAuthFlow]:
    async with db.session_factory() as session:
        return list((await session.execute(select(OAuthFlow))).scalars().all())


async def _identities(user_id: uuid.UUID) -> list[AuthIdentity]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthIdentity).where(AuthIdentity.user_id == user_id)
        )).scalars().all())


def test_a_provider_without_credentials_is_never_offered():
    """A button that cannot complete is worse than no button."""
    named = Settings(auth_mode="accounts", oauth_providers="google,github")
    assert named.auth_methods == ["password"]
    half = Settings(auth_mode="accounts", oauth_providers="google",
                    google_client_id="id")
    assert half.auth_methods == ["password"]
    ready = Settings(auth_mode="accounts", oauth_providers="google",
                     google_client_id="id", google_client_secret="secret")
    assert ready.auth_methods == ["password", "google"]


@pytest.mark.db
def test_the_redirect_carries_pkce_and_a_state_this_server_minted(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/v1/auth/redirect/google", follow_redirects=False)
        assert response.status_code == 307
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["google-client"]
        assert query["redirect_uri"] == [f"{ORIGIN}/api/v1/auth/callback/google"]
        flow = client.portal.call(_flows)[0]
        assert flow.provider == "google"
        # The verifier stays here. Only its challenge goes to the provider.
        assert query["code_challenge"][0] != flow.verifier
        assert oauth.challenge_for(flow.verifier) == query["code_challenge"][0]
        # And the state on the wire is not what is stored.
        assert query["state"][0].encode() not in flow.state_hash
        assert query["nonce"] == [flow.nonce]


@pytest.mark.db
def test_an_unconfigured_provider_has_no_redirect(accounts, monkeypatch):
    monkeypatch.setenv("OAUTH_PROVIDERS", "google")
    get_settings.cache_clear()
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/auth/redirect/github",
                          follow_redirects=False).status_code == 404
        assert client.get("/api/v1/auth/redirect/gitlab",
                          follow_redirects=False).status_code == 404


def _callback(client, provider, state, code="provider-code"):
    return client.get(f"/api/v1/auth/callback/{provider}",
                      params={"state": state, "code": code}, follow_redirects=False)


def _start(client, provider="google"):
    response = client.get(f"/api/v1/auth/redirect/{provider}", follow_redirects=False)
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


@pytest.mark.db
def test_a_callback_without_a_flow_of_ours_is_refused(accounts):
    """Provider-bound state: a callback has to match a redirect this server
    started, or anyone can post one."""
    with TestClient(app, base_url=ORIGIN) as client:
        assert _callback(client, "google", "a-state-we-never-minted").status_code == 403


@pytest.mark.db
def test_a_state_from_one_provider_cannot_be_spent_at_another(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        state = _start(client, "google")
        assert _callback(client, "github", state).status_code == 403


@pytest.mark.db
def test_a_state_is_good_once(accounts, monkeypatch):
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com")
    user = None
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "known@example.com")
        client.portal.call(_link, user.id, "google", "g-1")
        state = _start(client, "google")
        assert _callback(client, "google", state).status_code == 307
        assert _callback(client, "google", state).status_code == 403


@pytest.mark.db
def test_an_expired_flow_is_refused(accounts, monkeypatch):
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        state = _start(client, "google")

        async def age():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE oauth_flows SET expires_at = :past"),
                                      {"past": datetime.now(timezone.utc) - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(age)
        assert _callback(client, "google", state).status_code == 403


def _fake_provider(monkeypatch, provider, subject, email, verified=True, nonce_ok=True):
    async def exchange(name, code, verifier, nonce, settings):
        assert name == provider
        assert code and verifier
        if not nonce_ok:
            raise oauth.ProviderRefused("nonce does not match")
        if not verified:
            raise oauth.ProviderRefused("the provider has not verified this address")
        return oauth.ProviderIdentity(subject=subject, email=email)

    monkeypatch.setattr(oauth, "exchange", exchange)


@pytest.mark.db
def test_signing_in_needs_an_identity_somebody_already_linked(accounts, monkeypatch):
    """Never find or create an account by email. An unlinked provider account
    that happens to know an address must not become that person."""
    _fake_provider(monkeypatch, "google", subject="g-stranger", email="known@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "known@example.com")
        state = _start(client, "google")
        refused = _callback(client, "google", state)
        assert refused.status_code == 403
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_a_linked_identity_signs_in(accounts, monkeypatch):
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "known@example.com")
        client.portal.call(_link, user.id, "google", "g-1")
        state = _start(client, "google")
        signed_in = _callback(client, "google", state)
        assert signed_in.status_code == 307
        me = client.get("/api/v1/account").json()
        assert me["email"] == "known@example.com"
        assert me["recent_auth"] is True


@pytest.mark.db
def test_a_provider_that_refuses_its_own_claims_signs_nobody_in(accounts, monkeypatch):
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com",
                   nonce_ok=False)
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "known@example.com")
        client.portal.call(_link, user.id, "google", "g-1")
        state = _start(client, "google")
        assert _callback(client, "google", state).status_code == 403
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_linking_needs_a_session_and_recent_authentication(accounts, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "linker@example.com")
        assert client.post("/api/v1/account/identities/google",
                           headers={"Origin": ORIGIN}).status_code == 401
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        csrf = _csrf(client)
        assert client.post("/api/v1/account/identities/google",
                           headers=csrf).status_code == 403


def _csrf(client):
    value = next((c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf")), "x")
    return {"Origin": ORIGIN, "X-CSRF-Token": value}


@pytest.mark.db
def test_a_link_attaches_the_identity_to_the_account_that_asked(accounts, monkeypatch):
    _fake_provider(monkeypatch, "github", subject="h-42", email="linker@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "linker@example.com")
        _sign_in(client, "linker@example.com")
        started = client.post("/api/v1/account/identities/github", headers=_csrf(client))
        assert started.status_code == 200
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
        assert _callback(client, "github", state).status_code == 307
        linked = client.portal.call(_identities, user.id)
    assert {row.provider for row in linked} == {"password", "github"}
    assert [row.subject for row in linked if row.provider == "github"] == ["h-42"]


def _sign_in(client, email):
    assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                       json={"email": email, "password": PASSWORD,
                             "remember_me": False}).status_code == 204


@pytest.mark.db
def test_one_provider_account_cannot_be_linked_to_two_accounts(accounts, monkeypatch):
    _fake_provider(monkeypatch, "github", subject="h-shared", email="second@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        first = client.portal.call(_make, "first@example.com")
        client.portal.call(_link, first.id, "github", "h-shared")
        client.portal.call(_make, "second@example.com")
        _sign_in(client, "second@example.com")
        started = client.post("/api/v1/account/identities/github", headers=_csrf(client))
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
        assert _callback(client, "github", state).status_code == 409


@pytest.mark.db
def test_a_provider_verified_address_only_raises_assurance_when_it_matches(accounts, monkeypatch):
    """A provider proving some other address says nothing about this account's
    primary one."""
    _fake_provider(monkeypatch, "google", subject="g-other", email="Someone.Else@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "owner@example.com")
        _sign_in(client, "owner@example.com")
        started = client.post("/api/v1/account/identities/google", headers=_csrf(client))
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
        assert _callback(client, "google", state).status_code == 307
        assert client.get("/api/v1/account").json()["mail_verified"] is False


@pytest.mark.db
def test_a_matching_provider_address_does_raise_assurance(accounts, monkeypatch):
    _fake_provider(monkeypatch, "google", subject="g-owner", email="Owner@Example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "owner@example.com")
        _sign_in(client, "owner@example.com")
        started = client.post("/api/v1/account/identities/google", headers=_csrf(client))
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
        assert _callback(client, "google", state).status_code == 307
        assert client.get("/api/v1/account").json()["mail_verified"] is True


@pytest.mark.db
def test_the_provider_token_is_never_kept(accounts, monkeypatch):
    """Discarded provider tokens: nothing here is an agent for the provider."""
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "known@example.com")
        client.portal.call(_link, user.id, "google", "g-1")
        state = _start(client, "google")
        assert _callback(client, "google", state).status_code == 307
        rows = client.portal.call(_identities, user.id)
    google = next(row for row in rows if row.provider == "google")
    assert google.password_hash is None
    columns = {column.name for column in AuthIdentity.__table__.columns}
    assert not columns & {"access_token", "refresh_token", "provider_token"}


@pytest.mark.db
@pytest.mark.parametrize("state", ["disabled", "deletion_pending", "purging"])
def test_a_dead_account_cannot_sign_in_through_a_provider_either(accounts, monkeypatch, state):
    """Otherwise a provider is a cheaper door than the password route for an
    account that is not allowed through any door."""
    _fake_provider(monkeypatch, "google", subject=f"g-{state}", email=f"{state}@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, f"{state}@example.com")
        client.portal.call(_link, user.id, "google", f"g-{state}")

        async def kill():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE users SET state = :s WHERE id = :id"),
                                      {"s": state, "id": user.id})
                await session.commit()

        client.portal.call(kill)
        assert _callback(client, "google", _start(client, "google")).status_code == 403
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_a_flow_completed_in_another_browser_is_refused(accounts, monkeypatch):
    """The state proves this server started a flow, not that this browser did.

    Without binding, an attacker starts a link flow on their own account and
    sends the provider URL to someone else. That person's browser completes
    it, and their provider account is attached to the attacker's account for
    good, because one provider account links to one account and there is no
    unlink route.
    """
    _fake_provider(monkeypatch, "github", subject="h-victim", email="victim@example.com")
    with TestClient(app, base_url=ORIGIN) as attacker:
        attacker.portal.call(_make, "attacker@example.com")
        _sign_in(attacker, "attacker@example.com")
        started = attacker.post("/api/v1/account/identities/github", headers=_csrf(attacker))
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]

        # Another browser: same server, no cookie from the redirect that
        # started this flow.
        with TestClient(app, base_url=ORIGIN) as victim:
            assert _callback(victim, "github", state).status_code == 403


@pytest.mark.db
def test_a_sign_in_flow_cannot_be_planted_in_someone_else_s_browser(accounts, monkeypatch):
    """Login CSRF: a callback URL captured from the attacker's own flow, then
    opened by someone else, would sign that person into the attacker's
    account and quietly collect everything they then made."""
    _fake_provider(monkeypatch, "google", subject="g-attacker", email="attacker@example.com")
    with TestClient(app, base_url=ORIGIN) as attacker:
        user = attacker.portal.call(_make, "attacker@example.com")
        attacker.portal.call(_link, user.id, "google", "g-attacker")
        state = _start(attacker, "google")
        with TestClient(app, base_url=ORIGIN) as victim:
            assert _callback(victim, "google", state).status_code == 403
            assert victim.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_signing_in_retires_the_session_the_browser_arrived_with(accounts, monkeypatch):
    """Parity with the password route: a token planted before authentication
    must not be the token that comes out of it."""
    _fake_provider(monkeypatch, "google", subject="g-1", email="known@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "known@example.com")
        client.portal.call(_link, user.id, "google", "g-1")
        planted = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", planted.token)
        state = _start(client, "google")
        assert _callback(client, "google", state).status_code == 307
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {planted.token}"}).status_code == 401


@pytest.mark.db
def test_a_suspended_account_cannot_add_a_new_way_to_sign_in(accounts, monkeypatch):
    """Suspended reads and settles, and changes nothing. A credential is a
    change."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "paused@example.com")
        _sign_in(client, "paused@example.com")

        async def pause():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE users SET state = 'suspended' WHERE id = :id"),
                                      {"id": user.id})
                await session.commit()

        client.portal.call(pause)
        assert client.post("/api/v1/account/identities/google",
                           headers=_csrf(client)).status_code == 403


@pytest.mark.db
def test_spent_and_expired_flows_are_reclaimed(accounts):
    """The redirect route is unauthenticated, so its rows are the one thing
    here a stranger can accumulate. Nothing was clearing them."""
    with TestClient(app, base_url=ORIGIN) as client:
        _start(client, "google")

        async def age():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE oauth_flows SET expires_at = :past"),
                                      {"past": datetime.now(timezone.utc) - timedelta(hours=2)})
                await session.commit()

        client.portal.call(age)
        assert len(client.portal.call(_flows)) == 1
        client.portal.call(oauth.prune)
        assert client.portal.call(_flows) == []


@pytest.mark.db
def test_a_flow_that_is_still_usable_survives_the_sweep(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        _start(client, "google")
        client.portal.call(oauth.prune)
        assert len(client.portal.call(_flows)) == 1
