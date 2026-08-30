import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import db, rate_limit
from app.main import app
from app.tables import LoginAttempt
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


async def _forget() -> None:
    async with db.session_factory() as session:
        await session.execute(LoginAttempt.__table__.delete())
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
    # The identifier ceiling and the cap arrive together, and the address
    # ceiling is three times further out, so everything past ten waits the same.
    assert rate_limit.delay_for(rate_limit.IDENTIFIER_LIMIT) == rate_limit.MAX_DELAY_S
    assert rate_limit.delay_for(rate_limit.ADDRESS_LIMIT) == rate_limit.MAX_DELAY_S


@pytest.mark.db
def test_an_identifier_is_refused_once_its_ten_are_spent(accounts, waits):
    _account(accounts, "ceiling@example.com")
    with TestClient(app) as client:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            assert _login(client, "ceiling@example.com", password="wrong").status_code == 401
        assert _login(client, "ceiling@example.com", password="wrong").status_code == 429
        # The right password does not buy past it: the attempt is charged
        # before the password is looked at, or the limit would bound only the
        # guesses that were going to fail anyway.
        assert _login(client, "ceiling@example.com").status_code == 429
    assert waits == [0.0] * 5 + [0.5, 1.0, 2.0, 4.0, 8.0]


@pytest.mark.db
def test_one_address_is_refused_at_thirty_however_many_identifiers_it_uses(accounts, waits):
    with TestClient(app) as client:
        for n in range(rate_limit.ADDRESS_LIMIT):
            assert _login(client, f"spread{n}@example.com", password="x").status_code == 401
        assert _login(client, "spread-last@example.com", password="x").status_code == 429


@pytest.mark.db
def test_an_account_spending_its_ten_leaves_another_account_free(accounts, waits):
    _account(accounts, "loud@example.com")
    _account(accounts, "quiet@example.com")
    with TestClient(app) as client:
        for _ in range(rate_limit.IDENTIFIER_LIMIT):
            _login(client, "loud@example.com", password="wrong")
        assert _login(client, "loud@example.com").status_code == 429
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
    assert list(waits) == known_waits


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
def test_pruning_forgets_a_window_that_has_run_out(accounts, waits):
    async def go() -> list[bytes]:
        await rate_limit.charge_login("stale@example.com", _request("198.51.100.8"))
        async with db.session_factory() as session:
            await session.execute(
                update(LoginAttempt)
                .where(LoginAttempt.subject == rate_limit._digest("stale@example.com"))
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await session.commit()
        await rate_limit.charge_login("fresh@example.com", _request("198.51.100.8"))
        await rate_limit.prune()
        async with db.session_factory() as session:
            return list((await session.execute(select(LoginAttempt.subject))).scalars().all())

    left = accounts(go())
    assert rate_limit._digest("stale@example.com") not in left
    assert rate_limit._digest("fresh@example.com") in left
