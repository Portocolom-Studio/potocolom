import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import db, oauth, recovery, sessions, totp
from app.main import app
from app.passwords import hash_password
from app.settings import Settings, get_settings
from app.tables import AuthFactor, AuthIdentity, OAuthFlow, User
from tests.test_totp_flow import (
    _caller,
    _csrf_for,
    _live_sessions_for,
    _login,
    _session_cookie,
    _while_the_gate_moves,
)

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


async def _enrolled(user_id: uuid.UUID) -> None:
    """A confirmed factor, without the enrolment dance. This file is about what
    the provider gate does with one, not about how it got there."""
    async with db.session_factory() as session:
        session.add(AuthFactor(user_id=user_id, kind="totp", secret_ciphertext=b"sealed",
                               key_version=1, confirmed_at=func.now()))
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
def test_a_linked_identity_with_a_factor_is_sent_to_the_challenge(accounts, monkeypatch):
    """A provider proving who somebody is does not answer for the factor they
    enrolled. The gate is read in the transaction that would have minted, so an
    enrolment cannot commit between the two (issue #435)."""
    _fake_provider(monkeypatch, "google", subject="g-2", email="gated@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "gated@example.com")
        client.portal.call(_link, user.id, "google", "g-2")
        client.portal.call(_enrolled, user.id)
        gated = _callback(client, "google", _start(client, "google"))
        assert gated.status_code == 307
        assert gated.headers["location"] == f"{ORIGIN}/?totp=required"
        # A capability to answer a challenge, and no session behind it.
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_a_callback_in_flight_when_a_factor_arrives_is_sent_to_the_challenge(
        accounts, monkeypatch):
    """The test above enrols before the callback starts, so the gate is already
    up when the callback reads it, which the read-then-mint shape answered
    correctly too. Here the enrolment commits while the callback is in flight:
    a callback that read no factor must not come away with a session the
    enrolment's revocation could not reach (issue #435)."""
    _fake_provider(monkeypatch, "google", subject="g-race",
                   email="inflight-oauth@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "inflight-oauth@example.com")
        client.portal.call(_link, user.id, "google", "g-race")
        assert _login(client, "inflight-oauth@example.com").status_code == 204
        owner = {cookie.name: cookie.value for cookie in client.cookies.jar}
        started = client.post("/api/v1/account/totp", headers=_csrf(client)).json()

        async def signing_in():
            # One browser for both halves: the redirect plants the flow cookie
            # the callback is refused without, and it is the callback that has
            # to reach the gate, so the pair cannot be split across clients.
            async with _caller() as caller:
                begun = await caller.get("/api/v1/auth/redirect/google",
                                         follow_redirects=False)
                state = parse_qs(urlsplit(begun.headers["location"]).query)["state"][0]
                return await caller.get("/api/v1/auth/callback/google",
                                        params={"state": state, "code": "provider-code"},
                                        follow_redirects=False)

        async def enrolling():
            async with _caller(owner) as caller:
                return await caller.post(
                    "/api/v1/account/totp/confirm", headers=_csrf_for(owner),
                    json={"enrolment": started["enrolment"],
                          "code": totp.code_at(
                              started["secret"],
                              int(datetime.now(timezone.utc).timestamp()))})

        called_back, enrolled = client.portal.call(
            _while_the_gate_moves(monkeypatch, signing_in, enrolling))

        assert enrolled.status_code == 204, enrolled.text
        # The challenge, never a session: the account has a factor now.
        assert called_back.status_code == 307, called_back.text
        assert called_back.headers["location"] == f"{ORIGIN}/?totp=required"
        assert not [cookie for cookie in called_back.cookies.jar
                    if cookie.name.endswith("potocolom_session")]
        # The owner's own, rotated by the enrolment, and nothing else.
        assert len(client.portal.call(_live_sessions_for, user.id)) == 1


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


def _linked_through(client, provider):
    """The whole link flow from one browser: the redirect that plants the flow
    cookie, and the callback that carries it back."""
    started = client.post(f"/api/v1/account/identities/{provider}", headers=_csrf(client))
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
    return _callback(client, provider, state)


async def _suspend(user_id: uuid.UUID) -> None:
    """Suspension only, and directly, because it is the one state that leaves
    the account's sessions alive. Every other state a link must not land on
    revokes them, so the callback's live-session guard already refuses those
    and this is the case that slips past it."""
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE users SET prior_state = state, state = 'suspended' WHERE id = :id"),
            {"id": user_id})
        await session.commit()


@pytest.mark.db
def test_a_link_started_while_active_does_not_land_after_a_suspension(accounts, monkeypatch):
    """start_link asks for an active account and then sends the browser away
    for up to ten minutes, which is long enough for an administrator to close
    the account in. Suspension keeps its sessions, so the callback's own guard
    lets that browser back in (issue #448)."""
    _fake_provider(monkeypatch, "github", subject="h-susp", email="susp@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "susp@example.com")
        _sign_in(client, "susp@example.com")
        started = client.post("/api/v1/account/identities/github", headers=_csrf(client))
        assert started.status_code == 200, started.text
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]

        client.portal.call(_suspend, user.id)

        assert _callback(client, "github", state).status_code == 403
        assert [row.provider for row in client.portal.call(_identities, user.id)] == ["password"]


@pytest.mark.db
def test_linking_a_provider_rotates_the_token_that_linked_it(accounts, monkeypatch):
    """A link adds a way into the account, which is the same kind of event as
    changing a credential, and the browser making it may be holding a copy of
    a cookie somebody else took.

    Asserted by presenting the old token, not by seeing a new cookie come
    back: a response that sets a cookie for a session nobody revoked would
    satisfy that and change nothing (issue #445).
    """
    _fake_provider(monkeypatch, "github", subject="h-rotate", email="rotlink@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "rotlink@example.com")
        _sign_in(client, "rotlink@example.com")
        stolen, stolen_csrf = _session_cookie(client), _csrf(client)["X-CSRF-Token"]
        elsewhere = client.portal.call(sessions.mint, user, False)
        assert len(client.portal.call(_live_sessions_for, user.id)) == 2

        assert _linked_through(client, "github").status_code == 307

        # The copy is dead, and so is every other session on the account.
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {stolen}"}).status_code == 401
        assert client.get(
            "/api/v1/account",
            headers={"Authorization": f"Bearer {elsewhere.token}"}).status_code == 401
        assert len(client.portal.call(_live_sessions_for, user.id)) == 1
        # And whoever linked is still signed in, reading and writing with the
        # CSRF token that came back beside the new session.
        assert _session_cookie(client) != stolen
        assert _csrf(client)["X-CSRF-Token"] != stolen_csrf
        assert client.get("/api/v1/account").status_code == 200
        assert client.post("/api/v1/account/identities/google",
                           headers=_csrf(client)).status_code == 200


@pytest.mark.db
def test_linking_leaves_its_own_canvas_up(accounts, monkeypatch):
    """A revoked row stops the next request and never reaches a socket that
    bound its principal at the handshake, so the eviction has to name the
    sockets, and it has to spare the one in front of the person linking."""
    _fake_provider(monkeypatch, "github", subject="h-canvas", email="canvaslink@example.com")
    closed: list[tuple] = []

    async def close(user_id, session_id=None, keep=None):
        closed.append((user_id, session_id, keep))

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "canvaslink@example.com")
        _sign_in(client, "canvaslink@example.com")
        kept = client.portal.call(sessions.resolve, _session_cookie(client)).session.id
        monkeypatch.setattr(oauth.sessions, "close_sockets", close)
        assert _linked_through(client, "github").status_code == 307
    assert [call[0] for call in closed] == [user.id], closed
    # Named, not merely present: keeping the wrong session takes the canvas
    # down in front of the person linking and leaves the evicted one drawing.
    assert closed[0][2] == kept, closed


@pytest.mark.db
def test_linking_spends_an_outstanding_reset_link(accounts, monkeypatch):
    """Unlike a second factor, a linked provider gates nothing. A factor stands
    in front of the password a reset link sets, so the factor routes can leave
    the link alone; nothing stands in front of it here, so a link left alive
    still hands the account to whoever holds the mailbox, which is the door
    somebody linking to secure their account means to close."""
    _fake_provider(monkeypatch, "github", subject="h-reset", email="resetlink@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "resetlink@example.com")
        _sign_in(client, "resetlink@example.com")
        emailed = client.portal.call(recovery.mint_reset, "resetlink@example.com")
        assert _linked_through(client, "github").status_code == 307
        spent = client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                            json={"token": emailed, "password": "a-second-account-password"})
    assert spent.status_code == 403


@pytest.mark.db
def test_a_link_completed_after_its_session_ended_does_not_land(accounts, monkeypatch):
    """The rotation needs the session the flow started from. A link arriving
    from a browser that is no longer signed in would add a way into the
    account and end nothing, which is the reverse of what revoking that
    session meant."""
    _fake_provider(monkeypatch, "github", subject="h-late", email="latelink@example.com")
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "latelink@example.com")
        _sign_in(client, "latelink@example.com")
        started = client.post("/api/v1/account/identities/github", headers=_csrf(client))
        state = parse_qs(urlsplit(started.json()["redirect"]).query)["state"][0]
        client.portal.call(sessions.revoke_all, user.id)
        assert _callback(client, "github", state).status_code == 403
        linked = client.portal.call(_identities, user.id)
    assert {row.provider for row in linked} == {"password"}


@pytest.mark.db
def test_a_link_whose_rotation_is_refused_does_not_land(accounts, monkeypatch):
    """The rotation is the last statement of the transaction that writes the
    identity, so the 409 it raises when this token is no longer the stored one
    takes the link out with it. A link that stands while its rotation fails
    leaves a new way into the account and every old session alive, which is
    worse than either outcome on its own."""
    _fake_provider(monkeypatch, "github", subject="h-lost", email="lostlink@example.com")

    async def refuse(db_session, resolved, spend_capabilities=True):
        raise HTTPException(status_code=409,
                            detail="this session changed while that was in flight")

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "lostlink@example.com")
        _sign_in(client, "lostlink@example.com")
        monkeypatch.setattr(oauth.sessions, "rotate_and_revoke_others", refuse)
        assert _linked_through(client, "github").status_code == 409
        linked = client.portal.call(_identities, user.id)
    assert {row.provider for row in linked} == {"password"}


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
