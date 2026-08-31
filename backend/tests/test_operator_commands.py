"""The commands an operator runs at the machine.

None of these is reachable over HTTP. They are the way back into an install
that locked its administrators out, the way out of accounts mode, and the way
to change the key everything else is sealed with.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import db, keyring, operator, sessions
from app.main import app
from app.tables import Job, User
from app.passwords import hash_password
from tests.test_totp_flow import (
    ORIGIN, PASSWORD, ROOT_KEYS, _csrf, _factors, _login, _make, accounts,
)

__all__ = ["accounts"]

SECOND_KEY = "2:" + "B" * 43 + "=," + ROOT_KEYS


@pytest.fixture
def library(accounts, monkeypatch):
    """Puts the installation back the way the accounts fixture expects it.

    Two of these commands change what the install is: a collapse turns
    accounts off, and a rotation leaves the key ring somewhere else. The next
    test's connect refuses both, so they are undone here rather than left for
    whatever runs next to trip over.
    """
    yield accounts

    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    keyring.get_key_ring.cache_clear()
    from app.settings import get_settings

    get_settings.cache_clear()

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("asset_shares", "assets", "jobs"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(
                text("UPDATE installation_auth_state SET auth_mode = 'accounts', "
                     "root_key_version = 1 WHERE id = 1"))
            await session.commit()

    if db.session_factory is None:
        # serving=False: a rotation left the recorded key version ahead of the
        # ring this fixture just put back, and the serving path refuses that
        # before it can be undone.
        accounts(db.connect(serving=False))
    accounts(clear())
    accounts(db.dispose())


async def _count(table: str) -> int:
    async with db.session_factory() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


async def _mode() -> str:
    async with db.session_factory() as session:
        return str(await session.scalar(
            text("SELECT auth_mode FROM installation_auth_state WHERE id = 1")))


@pytest.mark.db
def test_collapsing_needs_the_phrase_typed_out(library):
    """It destroys every account on the installation. A flag is too easy to
    pass by accident and too easy to copy out of a forum post."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "kept@example.com")
        for wrong in ("", "yes", "COLLAPSE", operator.COLLAPSE_PHRASE.upper()):
            with pytest.raises(ValueError):
                client.portal.call(operator.collapse, wrong)
        assert client.portal.call(_count, "users") >= 2
        assert client.portal.call(_mode) == "accounts"


@pytest.mark.db
def test_collapsing_destroys_the_accounts_and_keeps_the_work(library):
    from tests.test_account_deletion import _owned_work

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "collapsing@example.com")
        client.portal.call(_owned_work, subject.id)
        assert _login(client, "collapsing@example.com").status_code == 204
        assert client.portal.call(_count, "sessions") == 1

        counted = client.portal.call(operator.collapse, operator.COLLAPSE_PHRASE)

        assert counted["accounts"] == 1
        assert counted["generations"] == 1
        assert client.portal.call(_count, "users") == 1
        # Every credential the accounts held, and the invitations that would
        # have made more of them.
        for emptied in ("sessions", "auth_identities", "auth_tokens", "auth_factors",
                        "recovery_codes", "invitations"):
            assert client.portal.call(_count, emptied) == 0, emptied
        assert client.portal.call(_count, "jobs") == 1
        assert client.portal.call(_count, "assets") == 1
        assert client.portal.call(_mode) == "none"

        async def owner() -> uuid.UUID:
            async with db.session_factory() as session:
                return (await session.execute(select(Job.user_id))).scalar_one()

        async def local() -> uuid.UUID:
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User.id).where(User.email == db.LOCAL_USER_EMAIL))).scalar_one()

        assert client.portal.call(owner) == client.portal.call(local)


@pytest.mark.db
def test_reclaiming_restores_one_account_to_an_active_administrator(library):
    """Every administrator suspended at once leaves nobody to press the
    button that would fix it."""
    with TestClient(app, base_url=ORIGIN) as client:
        locked = client.portal.call(_make, "lockedout@example.com")

        async def suspend() -> None:
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE users SET state = 'suspended' WHERE id = :id"),
                    {"id": locked.id})
                await session.commit()

        client.portal.call(suspend)
        was = client.portal.call(operator.reclaim_restore, "  LockedOut@Example.com ")
        assert was == "suspended"

        async def reread() -> User:
            async with db.session_factory() as session:
                return await session.get(User, locked.id)

        row = client.portal.call(reread)
        assert (row.state, row.role) == ("active", "admin")
        assert _login(client, "lockedout@example.com").status_code == 204


@pytest.mark.db
def test_reclaiming_an_address_nobody_holds_says_so(library):
    with TestClient(app, base_url=ORIGIN) as client:
        with pytest.raises(LookupError):
            client.portal.call(operator.reclaim_restore, "nobody@example.com")


@pytest.mark.db
def test_a_setup_link_is_refused_while_the_install_still_has_accounts(library):
    """The setup link adopts the implicit local user and the claim route
    refuses once anybody holds an identity, so minting one here would hand
    over something that fails when it is spent."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "holder@example.com")
        with pytest.raises(LookupError):
            client.portal.call(operator.reclaim_claim)


@pytest.mark.db
def test_reclaiming_mints_a_setup_link_and_retires_the_old_one(library):
    from app.tables import AuthToken

    with TestClient(app, base_url=ORIGIN) as client:

        async def strip_identities() -> None:
            async with db.session_factory() as session:
                await session.execute(text("DELETE FROM auth_identities"))
                await session.commit()

        client.portal.call(strip_identities)
        first = client.portal.call(operator.reclaim_claim)
        second = client.portal.call(operator.reclaim_claim)
        assert first != second

        async def live_setup_tokens() -> int:
            async with db.session_factory() as session:
                return int(await session.scalar(
                    select(func.count()).select_from(AuthToken)
                    .where(AuthToken.purpose == "setup",
                           AuthToken.consumed_at.is_(None))) or 0)

        assert client.portal.call(live_setup_tokens) == 1


@pytest.mark.db
def test_rotating_re_encrypts_every_secret_under_the_newest_key(library, monkeypatch):
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "rotated@example.com")
        assert _login(client, "rotated@example.com").status_code == 204
        secret, _ = _enrol(client)
        before = client.portal.call(_factors)[0]
        assert before.key_version == 1

        monkeypatch.setenv("ROOT_KEYS", SECOND_KEY)
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        result = client.portal.call(operator.rotate_keys)
        after = client.portal.call(_factors)[0]

    assert result == {"reencrypted": 1, "active_version": 2}
    assert after.key_version == 2
    assert after.secret_ciphertext != before.secret_ciphertext
    # The same secret, readable under the new key.
    ring = keyring.get_key_ring()
    assert ring.decrypt("totp-factors", after.secret_ciphertext,
                        after.user_id.bytes).decode() == secret


@pytest.mark.db
def test_rotating_refuses_when_a_key_it_would_need_is_gone(library, monkeypatch):
    """Rewriting a blob this install cannot read destroys the secret behind
    it, which is somebody's second factor."""
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lostkey@example.com")
        assert _login(client, "lostkey@example.com").status_code == 204
        _enrol(client)

        # A ring that has moved on and left version 1 behind.
        monkeypatch.setenv("ROOT_KEYS", "2:" + "B" * 43 + "=")
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        with pytest.raises(keyring.KeyRingError):
            client.portal.call(operator.rotate_keys)
        assert client.portal.call(_factors)[0].key_version == 1


@pytest.mark.db
def test_the_check_says_which_keys_are_still_holding_something(library, monkeypatch):
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "checked@example.com")
        assert _login(client, "checked@example.com").status_code == 204
        _enrol(client)
        assert client.portal.call(operator.retired_versions) == []

        monkeypatch.setenv("ROOT_KEYS", SECOND_KEY)
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        assert client.portal.call(operator.retired_versions) == [1]
        client.portal.call(operator.rotate_keys)
        assert client.portal.call(operator.retired_versions) == []


def test_the_configuration_report_says_what_would_happen(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        report = operator._configured()
    finally:
        get_settings.cache_clear()
    assert report["public_url"] == ORIGIN
    assert "SMTP_HOST" in report["mail"]
    assert report["oauth"] == "ok"
    # The resolved list, not the property object behind it.
    assert isinstance(report["auth_methods"], list)


def test_reclaim_takes_one_of_the_two_things_it_can_do():
    with pytest.raises(SystemExit):
        operator.main(["reclaim"])
    with pytest.raises(SystemExit):
        operator.main(["reclaim", "--claim", "--restore", "someone@example.com"])


@pytest.mark.db
def test_the_commands_reach_their_work_through_the_command_line(library, capsys):
    """Through main, not the functions under it: an argument parsed into the
    wrong call is exactly the kind of thing that only shows up here."""
    with TestClient(app, base_url=ORIGIN) as client:
        locked = client.portal.call(_make, "cli@example.com")

        async def suspend() -> None:
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE users SET state = 'suspended' WHERE id = :id"),
                    {"id": locked.id})
                await session.commit()

        client.portal.call(suspend)
        client.portal.call(db.dispose)

        operator.main(["reclaim", "--restore", "cli@example.com"])
        assert "active administrator again" in capsys.readouterr().out

        operator.main(["rotate-keys", "--check"])
        assert "can be removed" in capsys.readouterr().out

        operator.main(["configure"])
        assert "public_url" in capsys.readouterr().out

        with pytest.raises(SystemExit):
            operator.main(["collapse", "--confirm", "no"])

        client.portal.call(db.connect)


@pytest.mark.db
def test_a_reclaim_refuses_an_identity_that_landed_while_it_waited(library):
    """The check and the mint are one transaction behind the setup lock, so
    an identity committed while the reclaim waits is one the reclaim sees.

    Deterministic on purpose: the blocker takes the lock first, so a reclaim
    that read outside the lock would look before the insert and mint a link
    that is refused the moment somebody spends it.
    """
    import asyncio

    from app import enable

    with TestClient(app, base_url=ORIGIN) as client:

        async def strip_identities() -> None:
            async with db.session_factory() as session:
                await session.execute(text("DELETE FROM auth_identities"))
                await session.commit()

        client.portal.call(strip_identities)
        holding = asyncio.Event()

        async def claim_it_first() -> None:
            async with db.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": enable.SETUP_LOCK})
                    holding.set()
                    # Long enough that the reclaim is certainly queued behind
                    # this lock rather than merely slower.
                    await asyncio.sleep(0.3)
                    local = (await session.execute(
                        text("SELECT id FROM users LIMIT 1"))).scalar_one()
                    await session.execute(
                        text("INSERT INTO auth_identities "
                             "(id, user_id, provider, subject, password_hash) "
                             "VALUES (gen_random_uuid(), :id, 'password', "
                             "'raced@example.com', :hash)"),
                        {"id": local, "hash": hash_password(PASSWORD)})

        async def reclaim_behind_it():
            first = asyncio.create_task(claim_it_first())
            await holding.wait()
            try:
                return await operator.reclaim_claim()
            finally:
                await first

        with pytest.raises(LookupError):
            client.portal.call(reclaim_behind_it)

        async def live_tokens() -> int:
            async with db.session_factory() as session:
                return int(await session.scalar(text(
                    "SELECT count(*) FROM auth_tokens WHERE purpose = 'setup' "
                    "AND consumed_at IS NULL")) or 0)

        # And nothing was minted on the way to refusing.
        assert client.portal.call(live_tokens) == 0


@pytest.mark.db
def test_claiming_the_installation_waits_for_the_setup_lock(library):
    """The reclaim's check means nothing unless the claim route takes the same
    lock: without it a claim commits between that check and its mint."""
    import asyncio
    import time

    from app import enable
    from tests.test_first_admin_setup import PASSWORD as SETUP_PASSWORD

    with TestClient(app, base_url=ORIGIN) as client:

        async def strip_identities() -> None:
            async with db.session_factory() as session:
                await session.execute(text("DELETE FROM auth_identities"))
                await session.commit()

        client.portal.call(strip_identities)
        token = client.portal.call(enable.mint_setup_token)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def hold_the_lock() -> None:
            async with db.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": enable.SETUP_LOCK})
                    holding.set()
                    await release.wait()

        held = client.portal.start_task_soon(hold_the_lock)
        client.portal.call(holding.wait)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            claiming = pool.submit(
                client.post, "/api/v1/auth/setup",
                json={"token": token, "email": "locked@example.com",
                      "password": SETUP_PASSWORD})
            try:
                time.sleep(0.4)
                waited = not claiming.done()
            finally:
                # Always, even when the assertion below is going to fail: a
                # transaction left holding this lock wedges every test after it.
                client.portal.call(_set, release)
                answered = claiming.result(timeout=10)
                held.cancel()

        assert waited, "the claim did not wait for the lock"
        assert answered.status_code == 204
        assert time.monotonic() - started >= 0.4


async def _set(event) -> None:
    event.set()


@pytest.mark.db
def test_clear_factor_command_removes_it_and_ends_the_sessions(library, capsys):
    """The way back for somebody who lost the authenticator and every code.
    Driven through main() rather than the helper underneath it: the first
    version of this command built a coroutine, never awaited it, and printed
    success while the factor stayed exactly where it was."""
    from app import factors
    from app.tables import AuditEvent, AuthFactor, RecoveryCode, Session

    async def enrolled(user_id: uuid.UUID) -> None:
        ring = keyring.get_key_ring()
        async with db.session_factory() as session:
            session.add(AuthFactor(
                user_id=user_id, kind="totp",
                secret_ciphertext=ring.encrypt(factors.TOTP_PURPOSE, b"S" * 32, user_id.bytes),
                key_version=ring.active_version, confirmed_at=func.now()))
            session.add(RecoveryCode(user_id=user_id, code_hash=b"h" * 32))
            await session.commit()

    async def counts() -> tuple[int, int, int]:
        async with db.session_factory() as session:
            return (
                len((await session.execute(select(AuthFactor))).scalars().all()),
                len((await session.execute(select(RecoveryCode))).scalars().all()),
                len((await session.execute(
                    select(Session).where(Session.revoked_at.is_(None)))).scalars().all()),
            )

    async def enrolled_with_a_session() -> uuid.UUID:
        user = await _make("nophone@example.com")
        await enrolled(user.id)
        await sessions.mint(user, remember_me=False, authenticated=True)
        return user.id

    async def recorded() -> list[str]:
        async with db.session_factory() as session:
            return [row.action for row in (await session.execute(
                select(AuditEvent).order_by(AuditEvent.occurred_at))).scalars().all()]

    async def a_code_and_a_session(user_id: uuid.UUID) -> None:
        """What the account still has when there is no factor to clear."""
        async with db.session_factory() as session:
            session.add(RecoveryCode(user_id=user_id, code_hash=b"k" * 32))
            await session.commit()
        async with db.session_factory() as session:
            user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        await sessions.mint(user, remember_me=False, authenticated=True)

    # One loop throughout: mixing the portal runner's with a TestClient's is
    # how a coroutine ends up awaiting a future attached to the other one.
    user_id = library(enrolled_with_a_session())
    factors_before, codes_before, sessions_before = library(counts())
    assert (factors_before, codes_before) == (1, 1)
    assert sessions_before >= 1

    operator.main(["clear-factor", "nophone@example.com"])
    assert "Removed" in capsys.readouterr().out
    # The command opens its own connection and disposes it on the way out, so
    # anything read afterwards needs one of its own.
    library(db.connect(serving=False))
    assert library(counts()) == (0, 0, 0)
    # Also the proof that this suite can see an audit event at all, which is
    # what makes the empty reading after the second run below mean something.
    assert library(recorded()) == ["factor.cleared"]

    # Saying so when there is nothing to do, rather than reporting work.
    library(a_code_and_a_session(user_id))
    operator.main(["clear-factor", "nophone@example.com"])
    assert "no second factor" in capsys.readouterr().out
    library(db.connect(serving=False))
    # Nothing was cleared, so nothing was written: the codes and the sessions
    # of an account that never had a factor are not this command's to take,
    # and a history saying a second factor was removed from an account that
    # had none is a history that disagrees with what was printed.
    assert library(counts()) == (0, 1, 1)
    assert library(recorded()) == ["factor.cleared"]

    with pytest.raises(SystemExit):
        operator.main(["clear-factor", "nobody@example.com"])


@pytest.mark.db
def test_clear_factor_waits_for_the_account_lock(library):
    """This command and an enrolment exclude each other on one key.

    A first enrolment has no factor row to lock, so the row lock every other
    route waits behind holds nothing against it, and the two interleave: this
    command deletes the recovery codes after an enrolment committed its own
    and leaves a factor with no way back into the account. The lock is what
    the exclusion rests on instead.

    Proved as exclusion rather than by staging that interleave, because a
    command written to write nothing when it removed nothing cannot be made
    to perform the damaging half.
    """
    import asyncio

    from app import factors
    from app.tables import AuthFactor

    async def with_a_factor() -> uuid.UUID:
        user = await _make("waiting@example.com")
        ring = keyring.get_key_ring()
        async with db.session_factory() as session:
            session.add(AuthFactor(
                user_id=user.id, kind="totp",
                secret_ciphertext=ring.encrypt(factors.TOTP_PURPOSE, b"S" * 32, user.id.bytes),
                key_version=ring.active_version, confirmed_at=func.now()))
            await session.commit()
        return user.id

    user_id = library(with_a_factor())
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold_the_lock() -> None:
        async with db.session_factory() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                      {"key": factors._budget_lock(user_id)})
                holding.set()
                await release.wait()

    async def race() -> tuple[bool, bool]:
        held = asyncio.create_task(hold_the_lock())
        await holding.wait()
        clearing = asyncio.create_task(operator.clear_factor("waiting@example.com"))
        try:
            await asyncio.sleep(0.4)
            waited = not clearing.done()
        finally:
            # Always, even when the assertion below is going to fail: a
            # transaction left holding this lock wedges every test after it.
            release.set()
        removed = await asyncio.wait_for(clearing, 10)
        await held
        return waited, removed

    waited, removed = library(race())
    assert waited, "the command did not wait for the account lock"
    assert removed
    assert library(_factors()) == []


COLLAPSE_ORDER = ("auth_tokens", "auth_factors", "recovery_codes", "sessions",
                  "auth_identities")


@pytest.mark.db
def test_collapse_deletes_in_the_order_the_routes_lock(library):
    """One lock order with the routes a collapse can overlap, or it deadlocks.

    The command is offline, but nothing stops the API serving while somebody
    runs it, and the routes it meets there are arranged among themselves
    already. A challenge claims its token before it locks the factor, and
    enrolling or removing a factor holds auth_factors and recovery_codes while
    it rotates the session making the change. Deleting sessions first, which
    is how this read until #443, put the collapse on the far side of that
    pair: measured on PostgreSQL, a confirm or a removal overlapping a
    collapse was a DeadlockDetected, which reaches the operator as a failed
    command and the caller as a 500.

    Asserted on the order the deletes go out rather than by staging a real
    deadlock, the way test_every_route_takes_the_factor_before_the_codes is:
    making one happen means holding a transaction open while another starts,
    which is how a suite hangs instead of failing.
    """
    from sqlalchemy import event

    from tests.test_totp_flow import _lock_orders

    transactions: list[list[str]] = []
    open_now: dict[int, list[str]] = {}

    def record(conn, cursor, statement, parameters, context, executemany):
        open_now.setdefault(id(conn), []).append(statement)

    def close(conn):
        transactions.append(open_now.pop(id(conn), []))

    with TestClient(app, base_url=ORIGIN) as client:
        # Inside the lifespan, never before it: startup builds a new engine,
        # so a listener attached to the one that is here now records nothing
        # and the assertion below passes on no evidence.
        engine = db.engine.sync_engine
        client.portal.call(_make, "ordered-collapse@example.com")
        assert _login(client, "ordered-collapse@example.com").status_code == 204
        listeners = (("before_cursor_execute", record), ("commit", close),
                     ("rollback", close))
        for name, handler in listeners:
            event.listen(engine, name, handler)
        try:
            client.portal.call(operator.collapse, operator.COLLAPSE_PHRASE)
        finally:
            for name, handler in listeners:
                event.remove(engine, name, handler)

    orders = _lock_orders(transactions + list(open_now.values()), COLLAPSE_ORDER)
    assert orders, "no transaction touched all five tables, so this proved nothing"
    assert all(order == list(COLLAPSE_ORDER) for order in orders), orders


ROTATION_ORDER = ("auth_tokens", "sessions")


@pytest.mark.db
@pytest.mark.parametrize("change", ["password", "role", "state", "deletion"])
def test_every_change_spends_the_capabilities_before_it_takes_the_sessions(accounts, change):
    """The route side of the order the collapse deletes in.

    Four transactions end the mailed reset and recovery links and revoke the
    account's sessions in the same breath: the rotation every credential change
    shares, and the role, state and deletion changes that each write the two
    statements themselves. Taken the other way round they are a cycle with a
    collapse, which deletes auth_tokens first: measured on PostgreSQL, each of
    these overlapping a collapse with a live reset link present was a
    DeadlockDetected that killed the collapse (issue #443).

    Asserted on the order the locks go out rather than by staging a real
    deadlock, for the reason the collapse order above is: making one happen
    means holding a transaction open while another runs into it, which is how a
    suite hangs instead of failing.
    """
    from sqlalchemy import event

    from tests.test_totp_flow import _lock_orders

    transactions: list[list[str]] = []
    open_now: dict[int, list[str]] = {}

    def record(conn, cursor, statement, parameters, context, executemany):
        open_now.setdefault(id(conn), []).append(statement)

    def close(conn):
        transactions.append(open_now.pop(id(conn), []))

    with TestClient(app, base_url=ORIGIN) as client:
        # Inside the lifespan, never before it: startup builds a new engine, so
        # a listener attached to the one that is here now records nothing and
        # the assertion below passes on no evidence.
        engine = db.engine.sync_engine
        if change in ("role", "state"):
            actor = "order-actor@example.com"
            client.portal.call(_make, actor, "admin")
            target = client.portal.call(_make, "order-target@example.com")
        else:
            actor = "order-self@example.com"
            target = client.portal.call(_make, actor)
        assert _login(client, actor).status_code == 204
        listeners = (("before_cursor_execute", record), ("commit", close),
                     ("rollback", close))
        for name, handler in listeners:
            event.listen(engine, name, handler)
        try:
            if change == "password":
                answered = client.post(
                    "/api/v1/account/password", headers=_csrf(client),
                    json={"current_password": PASSWORD,
                          "password": "another-long-enough-password"})
            elif change == "role":
                answered = client.post(f"/api/v1/users/{target.id}/role",
                                       headers=_csrf(client),
                                       json={"role": "admin", "attested": True})
            elif change == "state":
                answered = client.post(f"/api/v1/users/{target.id}/state",
                                       headers=_csrf(client),
                                       json={"state": "suspended"})
            else:
                answered = client.request("DELETE", "/api/v1/account",
                                          headers=_csrf(client))
            assert answered.status_code == 204, answered.text
        finally:
            for name, handler in listeners:
                event.remove(engine, name, handler)

    orders = _lock_orders(transactions + list(open_now.values()), ROTATION_ORDER)
    assert orders, "no transaction touched both tables, so this proved nothing"
    assert all(order == list(ROTATION_ORDER) for order in orders), orders
