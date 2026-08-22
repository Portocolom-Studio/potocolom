import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, keyring, sessions, totp
from app.main import app
from app.passwords import hash_password
from app.settings import get_settings
from app.tables import AuthFactor, AuthIdentity, AuthToken, RecoveryCode, User

PASSWORD = "a-long-enough-account-password"
ORIGIN = "https://studio.example.com"
ROOT_KEYS = "1:" + "A" * 43 + "="


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    get_settings.cache_clear()
    keyring.get_key_ring.cache_clear()
    assert portal_runner(db.connect()) is True
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    original = db.local_user_id

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("recovery_codes", "auth_factors", "auth_tokens", "sessions",
                          "auth_identities", "audit_events", "mail_outbox", "oauth_flows",
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
        keyring.get_key_ring.cache_clear()


async def _make(email: str, role: str = "user") -> User:
    async with db.session_factory() as session:
        user = User(id=uuid.uuid4(), email=email, role=role)
        session.add(user)
        await session.flush()
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                 subject=email.lower(), password_hash=hash_password(PASSWORD)))
        await session.commit()
        return user


async def _factors() -> list[AuthFactor]:
    async with db.session_factory() as session:
        return list((await session.execute(select(AuthFactor))).scalars().all())


async def _challenges() -> list[AuthToken]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthToken).where(AuthToken.purpose == "challenge")
        )).scalars().all())


async def _live_sessions() -> list[sessions.Session]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(sessions.Session).where(sessions.Session.revoked_at.is_(None))
        )).scalars().all())


def _login(client, email, password=PASSWORD, remember_me=False):
    return client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                       json={"email": email, "password": password, "remember_me": remember_me})


def _csrf(client):
    value = next((c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf")), "x")
    return {"Origin": ORIGIN, "X-CSRF-Token": value}


def _enrol(client):
    """Enrol and confirm, the way Security settings would."""
    started = client.post("/api/v1/account/totp", headers=_csrf(client))
    assert started.status_code == 200
    secret = started.json()["secret"]
    codes = started.json()["recovery_codes"]
    confirmed = client.post("/api/v1/account/totp/confirm", headers=_csrf(client),
                            json={"code": totp.code_at(secret, int(_now()))})
    assert confirmed.status_code == 204
    return secret, codes


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


@pytest.mark.db
def test_an_account_without_totp_signs_in_exactly_as_before(accounts):
    """TOTP is optional. Nothing changes for anyone who has not enrolled."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "plain@example.com")
        assert _login(client, "plain@example.com").status_code == 204
        assert client.get("/api/v1/account").status_code == 200
        assert client.portal.call(_challenges) == []


@pytest.mark.db
def test_enrolling_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "stale@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert client.post("/api/v1/account/totp", headers=_csrf(client)).status_code == 403


@pytest.mark.db
def test_enrolment_gives_a_secret_a_uri_and_recovery_codes(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "enrol@example.com")
        assert _login(client, "enrol@example.com").status_code == 204
        started = client.post("/api/v1/account/totp", headers=_csrf(client)).json()
        assert started["secret"]
        assert started["uri"].startswith("otpauth://totp/")
        assert len(started["recovery_codes"]) == totp.RECOVERY_CODES
        # Unconfirmed until a code proves the authenticator actually has it.
        factor = client.portal.call(_factors)[0]
        assert factor.confirmed_at is None


@pytest.mark.db
def test_the_stored_secret_is_encrypted_under_the_key_ring(accounts):
    """A factor table anyone can read is not a second factor."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "sealed@example.com")
        assert _login(client, "sealed@example.com").status_code == 204
        secret, _ = _enrol(client)
        factor = client.portal.call(_factors)[0]
    assert secret.encode() not in factor.secret_ciphertext
    ring = keyring.get_key_ring()
    assert ring.decrypt("totp-factors", factor.secret_ciphertext,
                        user.id.bytes).decode() == secret
    assert factor.key_version == ring.active_version


@pytest.mark.db
def test_an_unconfirmed_factor_does_not_gate_the_next_login(accounts):
    """Enrolment that was started and abandoned must not lock anyone out."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "abandoned@example.com")
        assert _login(client, "abandoned@example.com").status_code == 204
        client.post("/api/v1/account/totp", headers=_csrf(client))
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "abandoned@example.com").status_code == 204
        assert fresh.get("/api/v1/account").status_code == 200


@pytest.mark.db
def test_a_wrong_code_does_not_confirm_enrolment(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "typo@example.com")
        assert _login(client, "typo@example.com").status_code == 204
        client.post("/api/v1/account/totp", headers=_csrf(client))
        refused = client.post("/api/v1/account/totp/confirm", headers=_csrf(client),
                              json={"code": "000000"})
        assert refused.status_code == 403
        assert client.portal.call(_factors)[0].confirmed_at is None


@pytest.mark.db
def test_an_enrolled_login_gets_a_challenge_and_no_session(accounts):
    """The primary login creates a pre-session capability, not a session."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "gated@example.com")
        assert _login(client, "gated@example.com").status_code == 204
        _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        answered = _login(fresh, "gated@example.com")
        assert answered.status_code == 200
        assert answered.json() == {"totp_required": True}
        # No session anywhere: the password alone is not enough now.
        assert fresh.get("/api/v1/account").status_code == 401
        assert len(fresh.portal.call(_challenges)) == 1


@pytest.mark.db
def test_the_right_code_turns_the_challenge_into_a_session(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "gated2@example.com")
        assert _login(client, "gated2@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "gated2@example.com").status_code == 200
        passed = fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                            json={"code": totp.code_at(secret, int(_now()))})
        assert passed.status_code == 204
        me = fresh.get("/api/v1/account").json()
        assert me["email"] == "gated2@example.com"
        assert me["recent_auth"] is True


@pytest.mark.db
def test_a_challenge_is_spent_once(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "once@example.com")
        assert _login(client, "once@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "once@example.com").status_code == 200
        code = totp.code_at(secret, int(_now()))
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": code}).status_code == 204
        replayed = fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                              json={"code": code})
        assert replayed.status_code == 403


@pytest.mark.db
def test_a_challenge_from_one_browser_is_useless_in_another(accounts):
    """The pre-session capability is a cookie, not a bearer anyone can quote."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "isolated@example.com")
        assert _login(client, "isolated@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as first:
        assert _login(first, "isolated@example.com").status_code == 200
        with TestClient(app, base_url=ORIGIN) as second:
            assert second.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                               json={"code": totp.code_at(secret, int(_now()))
                                     }).status_code == 403


@pytest.mark.db
def test_a_challenge_gives_up_after_ten_wrong_codes(accounts):
    """Six digits is a million guesses. Ten per challenge is what keeps that
    from being a number worth working through."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "guessed@example.com")
        assert _login(client, "guessed@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "guessed@example.com").status_code == 200
        for _ in range(10):
            assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                              json={"code": "000000"}).status_code == 403
        # Even the right code cannot revive it.
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": totp.code_at(secret, int(_now()))}).status_code == 403


@pytest.mark.db
def test_a_recovery_code_gets_someone_in_and_is_then_gone(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lostphone@example.com")
        assert _login(client, "lostphone@example.com").status_code == 204
        _, codes = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "lostphone@example.com").status_code == 200
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": codes[0]}).status_code == 204
        assert fresh.get("/api/v1/account").status_code == 200
    with TestClient(app, base_url=ORIGIN) as again:
        assert _login(again, "lostphone@example.com").status_code == 200
        assert again.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": codes[0]}).status_code == 403
        assert again.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": codes[1]}).status_code == 204


@pytest.mark.db
def test_recovery_codes_are_stored_as_hashes(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "hashed@example.com")
        assert _login(client, "hashed@example.com").status_code == 204
        _, codes = _enrol(client)

        async def stored():
            async with db.session_factory() as session:
                return list((await session.execute(select(RecoveryCode))).scalars().all())

        rows = client.portal.call(stored)
    assert len(rows) == totp.RECOVERY_CODES
    for row in rows:
        assert all(code.encode() not in row.code_hash for code in codes)


@pytest.mark.db
def test_an_expired_challenge_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "slow@example.com")
        assert _login(client, "slow@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "slow@example.com").status_code == 200

        async def age():
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE auth_tokens SET expires_at = :past WHERE purpose = 'challenge'"),
                    {"past": datetime.now(timezone.utc) - timedelta(minutes=1)})
                await session.commit()

        fresh.portal.call(age)
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": totp.code_at(secret, int(_now()))}).status_code == 403


@pytest.mark.db
def test_the_challenge_never_carries_administrator_capability(accounts):
    """A pre-session capability is not a session. It authorizes nothing."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "boss@example.com", "admin")
        assert _login(client, "boss@example.com").status_code == 204
        _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        before = len(fresh.portal.call(_live_sessions))
        assert _login(fresh, "boss@example.com").status_code == 200
        assert fresh.get("/api/v1/telemetry/preview").status_code == 401
        assert fresh.get("/api/v1/account").status_code == 401
        # The password was right and it minted nothing. The session the
        # enrolment browser still holds is not this login's doing.
        assert len(fresh.portal.call(_live_sessions)) == before
