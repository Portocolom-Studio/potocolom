import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import accounts as accounts_module
from app import db, gpu_samples, rate_limit
from app.main import app
from app.tables import AuthIdentity, LoginAttempt, User
from tests.test_session_policy import _account, _login, accounts

__all__ = ["accounts"]


def _request(host: str) -> Request:
    path = "/api/v1/auth/login"
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [], "server": ("testserver", 80), "client": (host, 4444),
    })


@pytest.fixture
def waits(monkeypatch):
    """Every delay the limiter computes, without any of them being served.

    Sleeping the real eight seconds would make this file the slowest in the
    suite and would fail on a loaded machine for a reason that has nothing to
    do with the limit (issue #429).
    """
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(rate_limit, "sleep", record)
    return recorded


async def _forget(scope: str | None = None) -> None:
    async with db.session_factory() as session:
        statement = LoginAttempt.__table__.delete()
        if scope is not None:
            statement = statement.where(LoginAttempt.scope == scope)
        await session.execute(statement)
        await session.commit()


async def _expire() -> None:
    async with db.session_factory() as session:
        await session.execute(update(LoginAttempt).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()


async def _expires_at(scope: str, value: str) -> datetime:
    async with db.session_factory() as session:
        return (await session.execute(
            select(LoginAttempt.expires_at).where(
                LoginAttempt.scope == scope,
                LoginAttempt.subject == rate_limit._digest(value))
        )).scalar_one()


async def _not_before(scope: str, value: str) -> datetime | None:
    async with db.session_factory() as session:
        return (await session.execute(
            select(LoginAttempt.not_before).where(
                LoginAttempt.scope == scope,
                LoginAttempt.subject == rate_limit._digest(value))
        )).scalar_one()


async def _attempts(scope: str, value: str) -> int | None:
    async with db.session_factory() as session:
        return (await session.execute(
            select(LoginAttempt.attempts).where(
                LoginAttempt.scope == scope,
                LoginAttempt.subject == rate_limit._digest(value))
        )).scalar_one_or_none()


def test_the_delay_is_free_for_five_then_doubles_to_an_eight_second_cap():
    assert [rate_limit.delay_for(n) for n in range(1, 6)] == [0.0] * 5
    assert [rate_limit.delay_for(n) for n in range(6, 11)] == [0.5, 1.0, 2.0, 4.0, 8.0]
    # The identifier ceiling and the cap arrive together, so an address, which
    # has no ceiling to reach, waits the same from its tenth attempt onwards.
    assert rate_limit.delay_for(rate_limit.IDENTIFIER_LIMIT) == rate_limit.MAX_DELAY_S


@pytest.mark.db
def test_an_identifier_is_refused_once_its_ten_are_spent(accounts, waits, monkeypatch):
    _account(accounts, "ceiling@example.com")
    hashed: list[str] = []
    searched: list[tuple] = []
    real = accounts_module.verify_password
    real_select = accounts_module.select

    def counting(stored: str, password: str) -> bool:
        hashed.append(stored)
        return real(stored, password)

    def recording(*entities):
        searched.append(entities)
        return real_select(*entities)

    monkeypatch.setattr(accounts_module, "verify_password", counting)
    monkeypatch.setattr(accounts_module, "select", recording)
    with TestClient(app) as client:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            assert _login(client, "ceiling@example.com", password="wrong").status_code == 401
        assert _login(client, "ceiling@example.com", password="wrong").status_code == 429
        # The right password does not buy past it: the attempt is charged
        # before the password is looked at, or the limit would bound only the
        # guesses that were going to fail anyway.
        assert _login(client, "ceiling@example.com").status_code == 429
    # A refusal is answered at once. The two that were turned away are not
    # also held for the delay the tenth attempt earned.
    assert len(waits) == rate_limit.IDENTIFIER_LIMIT
    # Argon2id is the cost this limit exists to bound, so a refused attempt has
    # to be refused before it: ten verifications for ten attempts, and not one
    # more for the two that were turned away.
    assert len(hashed) == rate_limit.IDENTIFIER_LIMIT
    # And before the account is looked up, not merely before the password is
    # checked. A limit consulted after the lookup would answer differently for
    # an address that exists and give away by refusal what the constant time
    # verification refuses to give away by timing.
    assert searched == [(AuthIdentity, User)] * rate_limit.IDENTIFIER_LIMIT


@pytest.mark.db
def test_one_address_queues_however_many_identifiers_it_uses(accounts, waits):
    spread = 16
    with TestClient(app) as client:
        # A different identifier every time, so each of those buckets holds one
        # attempt and owes no wait: every answer below is the address alone.
        codes = [_login(client, f"spread{n}@example.com", password="x").status_code
                 for n in range(spread)]
    # Never 429. One NAT, one proxy uvicorn has not been told to trust, or the
    # loopback publish the shipped compose file uses is a single address here,
    # so a ceiling would sign a whole installation out of this route ten
    # minutes at a time.
    assert 429 not in codes
    # The turns are handed out one at a time and each is deeper than the last,
    # so a peer that keeps knocking eventually asks for one past the cap and is
    # told to come back. The wait is stubbed here, so no turn is ever served
    # and the queue only grows: what this measures is what a burst is charged.
    assert codes[:rate_limit.FREE_ATTEMPTS] == [401] * rate_limit.FREE_ATTEMPTS
    assert codes[-1] == 503
    assert sorted(set(codes)) == [401, 503]
    assert waits, "no turn was ever served, so the order proves nothing"
    assert waits == sorted(waits)


@pytest.mark.db
def test_overlapping_attempts_from_one_address_take_turns(accounts, waits):
    # Eleven is what the queue holds: a twelfth turn falls past the cap.
    overlapping = 11

    async def go() -> None:
        await asyncio.gather(*[
            rate_limit.charge_login(f"turns{n}@example.com", _request("198.51.100.13"))
            for n in range(overlapping)])

    accounts(go())
    # Sleeping is latency for one task and not a rate. Attempts that overlap
    # used to serve the same eight seconds together, so twenty of them finished
    # inside one cap period and a flood paid for one attempt; what each is
    # charged now is its turn behind the one before it, and the last of these
    # is past a whole cap period on its own.
    assert sorted(waits) == pytest.approx(
        [0.0] * rate_limit.FREE_ATTEMPTS + [0.5, 1.5, 3.5, 7.5, 15.5, 23.5], abs=0.2)
    assert max(waits) > rate_limit.MAX_DELAY_S


@pytest.mark.db
def test_a_turn_past_the_cap_is_told_to_come_back_and_does_not_take_the_slot(accounts, waits):
    peer = "198.51.100.14"

    async def go() -> tuple[HTTPException, datetime | None, datetime | None]:
        for n in range(11):
            await rate_limit.charge_login(f"full{n}@example.com", _request(peer))
        before = await _not_before("address", peer)
        with pytest.raises(HTTPException) as refusal:
            await rate_limit.charge_login("full11@example.com", _request(peer))
        return refusal.value, before, await _not_before("address", peer)

    refused, before, after = accounts(go())
    # 503 and not 429: 429 on a sign-in reads as the account being shut, and
    # this has nothing to do with any account. Retry-After is honest because
    # the queue is never allowed to reach further out than the cap.
    assert refused.status_code == 503
    assert refused.headers == {"Retry-After": str(int(rate_limit.MAX_QUEUE_S))}
    # And the refused attempt does not push the queue out. If it did, a flood
    # would drive a shared peer past the cap for the rest of its window, which
    # is the outage the address bucket has no ceiling in order to avoid.
    assert before == after
    assert len(waits) == 11


@pytest.mark.db
def test_a_fresh_window_hands_back_the_turn_the_old_one_held(accounts, waits):
    """The queue belongs to the window that built it.

    Carried across a reset, a turn left deep by the last window is served
    again by the first attempt of the next one, which owes nothing.
    """
    peer = "198.51.100.18"

    async def go() -> list[float]:
        for _ in range(rate_limit.FREE_ATTEMPTS + 3):
            await rate_limit.charge_login("stale@example.com", _request(peer))
        assert await _not_before("address", peer) is not None
        await _expire()
        waits.clear()
        await rate_limit.charge_login("stale@example.com", _request(peer))
        return list(waits)

    served = accounts(go())
    assert served == [0.0]


@pytest.mark.db
def test_an_identifier_past_its_ceiling_takes_no_turn_on_its_address(accounts, waits):
    """One spent identifier must not be able to hold a whole peer's queue.

    Reserving the turn before the ceiling was checked meant every refusal
    moved the queue on without ever waiting for it, so the address stayed full
    while nobody was in it, and the next person behind that NAT was answered
    503 by attempts that had never queued. That is the shared-peer outage the
    address bucket gave up its ceiling to avoid, reached down the one path the
    rule about a refusal not extending the queue did not cover.
    """
    peer = "198.51.100.17"

    async def go() -> tuple[datetime | None, datetime | None]:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            await rate_limit.charge_login("spent@example.com", _request(peer))
        before = await _not_before("address", peer)
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            with pytest.raises(HTTPException) as refusal:
                await rate_limit.charge_login("spent@example.com", _request(peer))
            assert refusal.value.status_code == 429
        return before, await _not_before("address", peer)

    before, after = accounts(go())
    assert before is not None, "the address never took a turn, so this proved nothing"
    assert before == after


@pytest.mark.db
def test_the_wait_answers_to_the_identifier_when_the_address_is_new(accounts, waits):
    async def go() -> None:
        for _ in range(rate_limit.FREE_ATTEMPTS + 1):
            await rate_limit.charge_login("pinned@example.com", _request("198.51.100.15"))
        waits.clear()
        await rate_limit.charge_login("pinned@example.com", _request("198.51.100.16"))

    accounts(go())
    # An address on its first attempt owes nothing, so the wait left is what
    # the identifier owes for its seventh. A wait taken from the address alone
    # would be zero here, and an account already being ground would be free to
    # grind from the next address the attacker holds.
    assert waits == [rate_limit.delay_for(rate_limit.FREE_ATTEMPTS + 2)]


@pytest.mark.db
def test_an_account_spending_its_ten_leaves_another_account_free(accounts, waits):
    _account(accounts, "loud@example.com")
    _account(accounts, "quiet@example.com")
    with TestClient(app) as client:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            _login(client, "loud@example.com", password="wrong")
        assert _login(client, "loud@example.com").status_code == 429
        # Every request here comes from one peer, and the wait is stubbed, so
        # that peer's queue never drains the way the twenty-three seconds those
        # ten attempts really take would drain it. Dropped, so what the quiet
        # account meets below is the identifier ceiling alone.
        client.portal.call(_forget, "address")
        assert _login(client, "quiet@example.com").status_code == 204


@pytest.mark.db
def test_an_address_nobody_holds_is_charged_exactly_like_one_somebody_does(accounts, waits):
    _account(accounts, "known@example.com")

    def spend(client, address: str) -> list[int]:
        return [_login(client, address, password="wrong").status_code
                for _ in range(rate_limit.IDENTIFIER_LIMIT + 1)]

    with TestClient(app) as client:
        known = spend(client, "known@example.com")
        known_waits = list(waits)
        waits.clear()
        # The address budget is the one thing the two halves share, and it
        # carries into the second: cleared, so what is compared below is the
        # identifier alone.
        client.portal.call(_forget)
        unknown = spend(client, "nobody@example.com")

    assert known == [401] * rate_limit.IDENTIFIER_LIMIT + [429]
    assert unknown == known
    # To within the time each run spent in Argon2id rather than exactly: the
    # address queue is measured against the clock, so a wait shortens by
    # whatever real time passed while the attempts before it were answered.
    assert list(waits) == pytest.approx(known_waits, abs=1.0)


@pytest.mark.db
def test_the_window_is_anchored_at_the_attempt_that_opened_it(accounts, waits):
    async def go() -> list[datetime]:
        seen = []
        for _ in range(2):
            await rate_limit.charge_login("anchor@example.com", _request("198.51.100.6"))
            seen.append(await _expires_at("identifier", "anchor@example.com"))
        return seen

    first, second = accounts(go())
    # A window pushed forward by every attempt would never run out while
    # somebody kept knocking, so ten guesses by a stranger against an address
    # they merely know would keep its owner out for as long as they continued.
    assert first == second


@pytest.mark.db
def test_a_window_that_has_run_out_starts_the_count_again(accounts, waits):
    _account(accounts, "again@example.com")
    with TestClient(app) as client:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            _login(client, "again@example.com", password="wrong")
        assert _login(client, "again@example.com").status_code == 429
        client.portal.call(_expire)
        # The refusal lasts the window and not a moment longer. A ceiling
        # anybody can reach against an address they merely know would
        # otherwise be a way to keep its owner out for good.
        assert _login(client, "again@example.com").status_code == 204
        assert _login(client, "again@example.com").status_code == 204
        # The reset opens a new window instead of reopening the spent one. A row
        # reset to an expiry already in the past reads as expired to every
        # attempt after it, so the count would sit at one and bound nothing.
        assert client.portal.call(_attempts, "identifier", "again@example.com") == 2


@pytest.mark.db
def test_attempts_that_overlap_are_every_one_of_them_counted(accounts, waits):
    overlapping = 8

    async def go() -> None:
        await asyncio.gather(*[
            rate_limit.charge_login("race@example.com", _request("198.51.100.7"))
            for _ in range(overlapping)])

    accounts(go())
    assert accounts(_attempts("identifier", "race@example.com")) == overlapping
    assert accounts(_attempts("address", "198.51.100.7")) == overlapping


@pytest.mark.db
def test_the_wait_holds_no_pooled_connection(accounts, monkeypatch):
    held: list[int] = []

    async def record(seconds: float) -> None:
        held.append(db.engine.pool.checkedout())

    monkeypatch.setattr(rate_limit, "sleep", record)

    async def go() -> None:
        for _ in range(rate_limit.FREE_ATTEMPTS + 1):
            await rate_limit.charge_login("pool@example.com", _request("198.51.100.9"))

    accounts(go())
    # Fifteen connections and a wait that reaches eight seconds: a delay served
    # while the session is open would spend the pool on callers doing nothing.
    assert held == [0] * (rate_limit.FREE_ATTEMPTS + 1)


@pytest.mark.db
def test_the_maintenance_loop_forgets_a_window_that_has_run_out(accounts, waits):
    async def go() -> list[bytes]:
        await rate_limit.charge_login("stale@example.com", _request("198.51.100.8"))
        async with db.session_factory() as session:
            await session.execute(
                update(LoginAttempt)
                .where(LoginAttempt.subject == rate_limit._digest("stale@example.com"))
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await session.commit()
        await rate_limit.charge_login("fresh@example.com", _request("198.51.100.8"))
        # Through the loop that actually runs in a deployment. Calling prune()
        # here would pass just as well with nothing calling it at all.
        await gpu_samples.maintain_once()
        async with db.session_factory() as session:
            return list((await session.execute(select(LoginAttempt.subject))).scalars().all())

    left = accounts(go())
    assert rate_limit._digest("stale@example.com") not in left
    assert rate_limit._digest("fresh@example.com") in left
