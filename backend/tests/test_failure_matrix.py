"""The guarantees that only show themselves when something goes wrong.

Two callers arriving at once, a store that is down, a worker a version
behind, a mode that is not the one every other test runs in. Each of these is
a promise made somewhere in the documentation, and each one is the kind of
promise that holds in the happy path and quietly does not hold here.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import db, invitations, jobs, sessions
from app.main import app
from app.tables import Invitation, User
from tests.test_totp_flow import ORIGIN, PASSWORD, _csrf, _login, _make, accounts

__all__ = ["accounts"]


@pytest.fixture
def invited(accounts):
    """The accounts fixture does not clear invitations, and a neighbouring
    test reads the only row expecting to find one."""
    yield

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM invitations"))
            await session.commit()

    if db.session_factory is None:
        accounts(db.connect())
    accounts(clear())
    accounts(db.dispose())

NEW_PASSWORD = "a-long-enough-invited-password"


async def _invitation(email: str, role: str = "user") -> str:
    """One open invitation, minted the way the route does."""
    import secrets
    from datetime import datetime, timedelta, timezone

    token = secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        async with session.begin():
            session.add(Invitation(
                email=email,
                role=role,
                token_hash=invitations._token_hash(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
            ))
    return token


async def _accounts_for(email: str) -> int:
    async with db.session_factory() as session:
        return int(await session.scalar(
            select(func.count()).select_from(User).where(User.email == email)) or 0)


@pytest.mark.db
def test_one_invitation_redeemed_twice_at_once_makes_one_account(invited):
    """The link is consumed by the same statement that finds it, so the
    loser has nothing to enrol against. Two accounts from one invitation
    would be a role handed to somebody nobody invited."""
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(_invitation, "raced@example.com")
        body = invitations.RegisterRequest(token=token, password=NEW_PASSWORD)

        async def both():
            return await asyncio.gather(
                invitations.register(body), invitations.register(body),
                return_exceptions=True,
            )

        outcomes = client.portal.call(both)
        made = client.portal.call(_accounts_for, "raced@example.com")

    refused = [one for one in outcomes if isinstance(one, HTTPException)]
    assert made == 1
    assert len(refused) == 1
    assert refused[0].status_code == 403
    # The same answer an unknown link gets: the route is not an oracle for
    # which invitations exist.
    assert refused[0].detail == invitations.INVALID_INVITATION


@pytest.mark.db
def test_two_administrators_revoking_one_invitation_at_once_agree(invited):
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.portal.call(_invitation, "revoked@example.com")
        client.portal.call(_make, "admin17@example.com", "admin")
        assert _login(client, "admin17@example.com").status_code == 204

        async def invitation_id() -> uuid.UUID:
            async with db.session_factory() as session:
                return (await session.execute(
                    select(Invitation.id)
                    .where(Invitation.email == "revoked@example.com"))).scalar_one()

        which = client.portal.call(invitation_id)
        first = client.delete(f"/api/v1/invitations/{which}", headers=_csrf(client))
        second = client.delete(f"/api/v1/invitations/{which}", headers=_csrf(client))
        assert (first.status_code, second.status_code) in ((204, 204), (204, 404))
        # And the link is dead either way.
        spent = client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                            json={"token": token, "password": NEW_PASSWORD})
        assert spent.status_code == 403


@pytest.mark.db
def test_a_login_answers_the_same_whether_or_not_the_address_exists(accounts):
    """The one place a timing or wording difference turns the login route
    into a list of who has an account here."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "known@example.com")
        wrong = _login(client, "known@example.com", password="not-the-right-password")
        nobody = _login(client, "nobody-at-all@example.com", password=PASSWORD)
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


@pytest.mark.db
def test_the_store_going_down_answers_503_and_not_a_traceback(accounts):
    """Every authenticated route, not the one that happened to be tested:
    the guard is in the principal, which all of them pass through."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "outage17@example.com")
        assert _login(client, "outage17@example.com").status_code == 204
        factory, db.session_factory = db.session_factory, None
        try:
            answers = {
                path: client.get(path).status_code
                for path in ("/api/v1/account", "/api/v1/generations", "/api/v1/users")
            }
        finally:
            db.session_factory = factory
    assert set(answers.values()) == {503}, answers


@pytest.mark.db
def test_a_storage_outage_leaves_the_job_where_the_next_pass_finds_it(monkeypatch):
    """A storage that will not answer must not produce a job marked running
    against a worker that was never given anywhere to put the result."""
    import time

    from tests.test_jobs import FLEET_HEADERS, fleet_hello

    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-storage-out")

            class Refusing:
                async def upload_target(self, key, token=None):
                    raise RuntimeError("storage is down")

            monkeypatch.setattr(jobs, "get_storage", Refusing)
            created = client.post("/api/v1/generations",
                                  json={"model_id": "sd-test",
                                        "params": {"prompt": "a boat"}})
            assert created.status_code == 202
            job_id = created.json()["job_id"]

            # Long enough for the dispatch loop to have tried and failed.
            time.sleep(0.5)
            assert client.get(f"/api/v1/generations/{job_id}").json()["state"] == "queued"
            assert uuid.UUID(job_id) not in jobs.inflight


@pytest.mark.db
def test_the_session_pool_is_bounded(accounts):
    """An unbounded pool is one request away from taking every connection a
    managed PostgreSQL will give the whole installation."""
    assert db.engine is not None
    pool = db.engine.pool
    assert pool.size() + pool._max_overflow <= 15


def test_the_cookie_names_follow_the_scheme_the_browser_sees():
    """__Host- requires Secure, which a browser refuses over plain HTTP, so
    a LAN install has to use the unprefixed names or hold no session at all."""
    assert sessions.cookie_names("https://studio.example.com") == (
        "__Host-potocolom_session", "__Host-potocolom_csrf")
    assert sessions.cookie_names("http://192.168.1.10:8080") == (
        "potocolom_session", "potocolom_csrf")
