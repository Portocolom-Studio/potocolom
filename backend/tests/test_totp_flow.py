import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from app import db, factors, keyring, sessions, totp
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


async def _codes() -> list[RecoveryCode]:
    async with db.session_factory() as session:
        return list((await session.execute(select(RecoveryCode))).scalars().all())


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
                            json={"enrolment": started.json()["enrolment"],
                                  "code": totp.code_at(secret, int(_now()))})
    assert confirmed.status_code == 204
    return secret, codes


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _next_code(secret: str) -> str:
    """Confirming an enrolment spends the code that confirmed it, so a sign-in
    in the same thirty seconds needs the next one."""
    return totp.code_at(secret, int(_now()) + totp.STEP)


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
        # Nothing is written until a code proves the authenticator has it.
        assert client.portal.call(_factors) == []
        assert client.portal.call(_codes) == []


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
        started = client.post("/api/v1/account/totp", headers=_csrf(client)).json()
        refused = client.post("/api/v1/account/totp/confirm", headers=_csrf(client),
                              json={"enrolment": started["enrolment"], "code": "000000"})
        assert refused.status_code == 403
        assert client.portal.call(_factors) == []


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
                            json={"code": _next_code(secret)})
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
        code = _next_code(secret)
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


@pytest.mark.db
def test_a_new_login_does_not_hand_back_a_fresh_ten_guesses(accounts):
    """The ceiling is what makes six digits safe. Counted per challenge and
    with a free challenge available on every login, it counts nothing: an
    attacker holding the password loops login, ten guesses, login."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "bruteforced@example.com")
        assert _login(client, "bruteforced@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "bruteforced@example.com").status_code == 200
        for _ in range(10):
            assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                              json={"code": "000000"}).status_code == 403
        # Start again, which is free, and the budget must not come back.
        assert _login(fresh, "bruteforced@example.com").status_code == 200
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": "000000"}).status_code == 403
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": totp.code_at(secret, int(_now()))}).status_code == 403


@pytest.mark.db
def test_a_code_that_worked_once_never_works_again(accounts):
    """RFC 6238 is explicit: a validated code must not be accepted twice. It
    stays valid for ninety seconds otherwise, which is long enough for a
    proxy that phished it to spend it."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "replayed@example.com")
        assert _login(client, "replayed@example.com").status_code == 204
        secret, _ = _enrol(client)
    code = _next_code(secret)
    with TestClient(app, base_url=ORIGIN) as first:
        assert _login(first, "replayed@example.com").status_code == 200
        assert first.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": code}).status_code == 204
    with TestClient(app, base_url=ORIGIN) as second:
        assert _login(second, "replayed@example.com").status_code == 200
        assert second.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                           json={"code": code}).status_code == 403


def test_a_recovery_code_is_long_enough_to_survive_a_stolen_database():
    """It is the one credential here that is both low entropy and fast
    hashed, so its length is the only thing standing behind it."""
    codes = totp.new_recovery_codes()
    alphabet = 32
    symbols = sum(len(part) for part in codes[0].split("-"))
    assert symbols * (alphabet.bit_length() - 1) >= 100


@pytest.mark.db
def test_an_abandoned_replacement_leaves_the_working_factor_alone(accounts):
    """Starting a second enrolment and walking away used to turn the second
    factor off: one request from a stolen session, no code, and no notice.
    """
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "keeper@example.com")
        assert _login(client, "keeper@example.com").status_code == 204
        secret, codes = _enrol(client)
        before = client.portal.call(_factors)[0]

        client.post("/api/v1/account/totp", headers=_csrf(client))

        after = client.portal.call(_factors)
        assert len(after) == 1
        assert after[0].id == before.id
        assert after[0].secret_ciphertext == before.secret_ciphertext
        assert len(client.portal.call(_codes)) == totp.RECOVERY_CODES
    with TestClient(app, base_url=ORIGIN) as fresh:
        gated = _login(fresh, "keeper@example.com")
        assert gated.status_code == 200
        assert gated.json()["totp_required"] is True
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": _next_code(secret)}).status_code == 204
    with TestClient(app, base_url=ORIGIN) as recovering:
        assert _login(recovering, "keeper@example.com").status_code == 200
        assert recovering.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                               json={"code": codes[0]}).status_code == 204


@pytest.mark.db
def test_an_enrolment_belongs_to_the_account_that_started_it(accounts):
    """The pending enrolment is held by the browser, so it is bound to the
    account it was minted for and useless anywhere else."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "minter@example.com")
        assert _login(client, "minter@example.com").status_code == 204
        started = client.post("/api/v1/account/totp", headers=_csrf(client)).json()
    with TestClient(app, base_url=ORIGIN) as other:
        other.portal.call(_make, "borrower@example.com")
        assert _login(other, "borrower@example.com").status_code == 204
        refused = other.post("/api/v1/account/totp/confirm", headers=_csrf(other),
                             json={"enrolment": started["enrolment"],
                                   "code": totp.code_at(started["secret"], int(_now()))})
        assert refused.status_code == 403
        assert other.portal.call(_factors) == []


@pytest.mark.db
def test_an_enrolment_nobody_confirmed_in_time_is_no_longer_one(accounts, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "slow@example.com")
        assert _login(client, "slow@example.com").status_code == 204
        monkeypatch.setattr(factors, "ENROLMENT_TTL", timedelta(seconds=-1))
        started = client.post("/api/v1/account/totp", headers=_csrf(client)).json()
        refused = client.post("/api/v1/account/totp/confirm", headers=_csrf(client),
                              json={"enrolment": started["enrolment"],
                                    "code": totp.code_at(started["secret"], int(_now()))})
        assert refused.status_code == 403
        assert client.portal.call(_factors) == []


async def _live_challenges() -> list[int]:
    async with db.session_factory() as session:
        return sorted((await session.execute(
            select(AuthToken.attempts).where(AuthToken.purpose == "challenge",
                                             AuthToken.consumed_at.is_(None))
        )).scalars().all())


async def _spend(count: int) -> None:
    """What answering wrong `count` times leaves on the live challenge."""
    async with db.session_factory() as session:
        await session.execute(
            update(AuthToken)
            .where(AuthToken.purpose == "challenge", AuthToken.consumed_at.is_(None))
            .values(attempts=count))
        await session.commit()


@pytest.mark.db
def test_the_guess_budget_survives_logins_that_overlap(accounts):
    """Ten guesses per account, not ten per login. Starting a login costs
    nothing to whoever already has the password, so a budget that resets on a
    new challenge is no budget: they open more.

    One at a time this held. Overlapping, every login but one used to come
    back with a fresh challenge at zero, which is a six-digit code with
    unlimited tries against it.
    """
    import asyncio

    user = accounts(_make("racer@example.com"))
    accounts(factors.begin_challenge(user, remember_me=False))
    accounts(_spend(7))

    started = asyncio.Event()
    waiting = 0

    async def begin() -> None:
        """Every caller reaches begin_challenge before any of them proceeds.

        Without this the scheduler is free to run them one after another,
        which is the case that always worked, so the test would pass against
        the unlocked code whenever it happened to serialise them.
        """
        nonlocal waiting
        waiting += 1
        if waiting == 4:
            started.set()
        await started.wait()
        await factors.begin_challenge(user, remember_me=False)

    async def overlapping() -> None:
        await asyncio.gather(*[begin() for _ in range(4)])

    accounts(overlapping())
    # Exactly one, not merely several that each carried the seven: each begin
    # consumes the one before it, so four that overlap must still leave one
    # challenge behind. Four rows at seven would be twelve guesses, not three.
    assert accounts(_live_challenges()) == [7]


@pytest.mark.db
def test_one_challenge_answered_twice_at_once_gets_in_once(accounts):
    """A challenge is one use. The attempt was counted and committed, then the
    code checked, then the token consumed, and between the first commit and
    the last a second request carrying the same cookie still found it
    unspent, so two valid recovery codes both bought a session (issue #421).
    """
    import asyncio

    import httpx

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "twice@example.com")
        assert _login(client, "twice@example.com").status_code == 204
        _, codes = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "twice@example.com").status_code == 200
        held = {cookie.name: cookie.value for cookie in fresh.cookies.jar}

        ready = asyncio.Event()
        arrived = 0

        async def answer(code: str) -> int:
            """Both requests are in flight before either is allowed to post.

            Left to the scheduler one can finish before the other starts,
            which is the sequential case that always worked, so the test
            would pass against the unconditional consume by luck.
            """
            nonlocal arrived
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=ORIGIN, cookies=held) as caller:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
                answered = await caller.post("/api/v1/auth/totp",
                                             headers={"Origin": ORIGIN},
                                             json={"code": code})
                return answered.status_code

        async def together() -> list[int]:
            return list(await asyncio.gather(answer(codes[0]), answer(codes[1])))

        answered = fresh.portal.call(together)
    assert sorted(answered) == [204, 403], answered


def _replace(client, secret_and_code, current=None):
    """Enrol again on an account that already has a factor."""
    started = client.post("/api/v1/account/totp", headers=_csrf(client))
    assert started.status_code == 200
    body = {"enrolment": started.json()["enrolment"],
            "code": totp.code_at(started.json()["secret"], int(_now()))}
    if current is not None:
        body["current_code"] = current
    return started.json()["secret"], client.post(
        "/api/v1/account/totp/confirm", headers=_csrf(client), json=body)


@pytest.mark.db
def test_replacing_a_factor_needs_the_one_being_replaced(accounts):
    """A session with recent authentication could enrol a factor it controlled
    and the account's real one was deleted to make room for it, in two
    requests, with nothing sent to anybody. The factor stopped an attacker at
    the challenge and then did not stop them here."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "replaced@example.com")
        assert _login(client, "replaced@example.com").status_code == 204
        secret, _ = _enrol(client)
        _, refused = _replace(client, None)
        assert refused.status_code == 403
        # The factor they already had is untouched.
        stored = client.portal.call(_factors)
        assert len(stored) == 1
        assert keyring.get_key_ring().decrypt(
            "totp-factors", stored[0].secret_ciphertext, stored[0].user_id.bytes).decode() == secret


@pytest.mark.db
def test_a_code_from_the_old_factor_replaces_it(accounts):
    """Somebody moving to a new phone still has the old one in their hand."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "newphone@example.com")
        assert _login(client, "newphone@example.com").status_code == 204
        old_secret, _ = _enrol(client)
        new_secret, replaced = _replace(client, None, current=_next_code(old_secret))
        assert replaced.status_code == 204
        stored = client.portal.call(_factors)
        assert len(stored) == 1
        assert keyring.get_key_ring().decrypt(
            "totp-factors", stored[0].secret_ciphertext,
            stored[0].user_id.bytes).decode() == new_secret


@pytest.mark.db
def test_a_recovery_code_replaces_the_factor_too(accounts):
    """The phone is gone, the codes are not, and enrolling a new authenticator
    is exactly what somebody in that position wants to do."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lostphone2@example.com")
        assert _login(client, "lostphone2@example.com").status_code == 204
        _, codes = _enrol(client)
        _, replaced = _replace(client, None, current=codes[0])
        assert replaced.status_code == 204
        assert len(client.portal.call(_factors)) == 1


@pytest.mark.db
def test_a_wrong_code_for_the_old_factor_changes_nothing(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "wrongold@example.com")
        assert _login(client, "wrongold@example.com").status_code == 204
        old_secret, _ = _enrol(client)
        _, refused = _replace(client, None, current="000000")
        assert refused.status_code == 403
        stored = client.portal.call(_factors)
        assert keyring.get_key_ring().decrypt(
            "totp-factors", stored[0].secret_ciphertext,
            stored[0].user_id.bytes).decode() == old_secret


@pytest.mark.db
def test_a_first_enrolment_has_nothing_to_prove(accounts):
    """Nothing to replace, so nothing to ask for. Requiring a code here would
    mean an account could never enrol at all."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "firsttime@example.com")
        assert _login(client, "firsttime@example.com").status_code == 204
        _enrol(client)
        assert len(client.portal.call(_factors)) == 1


@pytest.mark.db
def test_two_replacements_at_once_leave_one_factor(accounts):
    """Both prove the factor they are replacing, and only one of them can
    have removed it. Deleting by account rather than by the factor that was
    proved would have left two, with nothing to say which gates a login."""
    import asyncio

    import httpx

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "racer2@example.com")
        assert _login(client, "racer2@example.com").status_code == 204
        _, codes = _enrol(client)
        held = {cookie.name: cookie.value for cookie in client.cookies.jar}
        starts = [client.post("/api/v1/account/totp", headers=_csrf(client)).json()
                  for _ in range(2)]
        ready = asyncio.Event()
        arrived = 0

        async def confirm(started: dict, recovery: str) -> int:
            nonlocal arrived
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=ORIGIN, cookies=held) as caller:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
                answered = await caller.post(
                    "/api/v1/account/totp/confirm",
                    headers={"Origin": ORIGIN, "X-CSRF-Token": held[
                        next(name for name in held if name.endswith("potocolom_csrf"))]},
                    json={"enrolment": started["enrolment"],
                          "code": totp.code_at(started["secret"], int(_now())),
                          "current_code": recovery})
                return answered.status_code

        async def together() -> list[int]:
            return list(await asyncio.gather(confirm(starts[0], codes[0]),
                                             confirm(starts[1], codes[1])))

        answered = client.portal.call(together)
        assert len(client.portal.call(_factors)) == 1, answered
    assert sorted(answered) == [204, 403], answered
