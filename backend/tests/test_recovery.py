from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, recovery, sessions
from app.main import app
from app.passwords import verify_password
from app.settings import get_settings
from app.tables import AuthIdentity, AuthToken, MailOutbox
from tests.test_totp_flow import ORIGIN, PASSWORD, _login, _make, accounts

__all__ = ["accounts"]

NEW_PASSWORD = "a-brand-new-long-enough-password"


async def _tokens(purpose: str) -> list[AuthToken]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthToken).where(AuthToken.purpose == purpose)
        )).scalars().all())


async def _outbox() -> list[MailOutbox]:
    async with db.session_factory() as session:
        return list((await session.execute(select(MailOutbox))).scalars().all())


async def _hash_of(email: str) -> str:
    async with db.session_factory() as session:
        return (await session.execute(
            select(AuthIdentity.password_hash).where(AuthIdentity.subject == email)
        )).scalar_one()


def _ask(client, email):
    return client.post("/api/v1/auth/reset", headers={"Origin": ORIGIN},
                       json={"email": email})


@pytest.mark.db
def test_asking_answers_the_same_whoever_asked(accounts):
    """A different answer for an address nobody holds turns this route into a
    way to enumerate accounts."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "real@example.com")
        known = _ask(client, "real@example.com")
        unknown = _ask(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


@pytest.mark.db
def test_a_reset_is_queued_only_for_an_address_somebody_holds(accounts, monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("MAIL_FROM", "potocolom@example.com")
    get_settings.cache_clear()
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "real2@example.com")
        assert _ask(client, "nobody@example.com").status_code == 202
        assert client.portal.call(_outbox) == []
        assert _ask(client, "real2@example.com").status_code == 202
        queued = client.portal.call(_outbox)
    assert len(queued) == 1
    assert queued[0].template == "reset"
    assert queued[0].to_email == "real2@example.com"


@pytest.mark.db
def test_the_reset_link_lasts_thirty_minutes_and_is_good_once(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "resetme@example.com")
        before = datetime.now(timezone.utc)
        assert _ask(client, "resetme@example.com").status_code == 202
        token = client.portal.call(recovery.mint_reset, "resetme@example.com")
        rows = client.portal.call(_tokens, "reset")
        window = rows[-1].expires_at - before
        assert timedelta(minutes=29) < window < timedelta(minutes=31)
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD}).status_code == 204
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD}).status_code == 403


@pytest.mark.db
def test_a_completed_reset_changes_the_password_and_grants_nothing(accounts):
    """Reset returns to login: it does not hand back a session, and it does not
    open the recent-authentication window that guards credential changes."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "changed@example.com")
        was = client.portal.call(_hash_of, "changed@example.com")
        token = client.portal.call(recovery.mint_reset, "changed@example.com")
        done = client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD})
        assert done.status_code == 204
        assert done.headers.get_list("set-cookie") == []
        assert client.get("/api/v1/account").status_code == 401
        now = client.portal.call(_hash_of, "changed@example.com")
    assert now != was
    assert verify_password(now, NEW_PASSWORD) is True
    assert verify_password(now, PASSWORD) is False


@pytest.mark.db
def test_a_reset_ends_every_session_that_account_held(accounts):
    """Whoever forced the reset may be the one holding a stolen session."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "kicked@example.com")
        stolen = client.portal.call(sessions.mint, user, False)
        token = client.portal.call(recovery.mint_reset, "kicked@example.com")
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD}).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {stolen.token}"}).status_code == 401


@pytest.mark.db
def test_a_weak_password_leaves_the_reset_link_usable(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "weakreset@example.com")
        token = client.portal.call(recovery.mint_reset, "weakreset@example.com")
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": "short"}).status_code == 400
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD}).status_code == 204


@pytest.mark.db
def test_an_expired_reset_link_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "late@example.com")
        token = client.portal.call(recovery.mint_reset, "late@example.com")

        async def age():
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE auth_tokens SET expires_at = :past WHERE purpose = 'reset'"),
                    {"past": datetime.now(timezone.utc) - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(age)
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD}).status_code == 403


@pytest.mark.db
def test_an_administrator_gets_no_emailed_reset(accounts, monkeypatch):
    """An administrator credential must never be recoverable from a mailbox.
    Their way back is offline, at the machine."""
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("MAIL_FROM", "potocolom@example.com")
    get_settings.cache_clear()
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "boss2@example.com", "admin")
        assert _ask(client, "boss2@example.com").status_code == 202
        assert client.portal.call(_outbox) == []
        assert client.portal.call(_tokens, "reset") == []


@pytest.mark.db
def test_offline_recovery_prints_a_ten_minute_link_for_an_administrator(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "boss3@example.com", "admin")
        before = datetime.now(timezone.utc)
        link = client.portal.call(recovery.mint_admin_recovery, "boss3@example.com")
        rows = client.portal.call(_tokens, "recovery")
    assert link.startswith(f"{ORIGIN}/recover#")
    assert len(rows) == 1
    window = rows[0].expires_at - before
    assert timedelta(minutes=9) < window < timedelta(minutes=11)
    # Only the hash is durable.
    assert link.split("#")[1].encode() not in rows[0].token_hash


@pytest.mark.db
def test_offline_recovery_refuses_an_address_that_is_not_an_administrator(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "member@example.com")
        with pytest.raises(recovery.NoSuchAdministrator):
            client.portal.call(recovery.mint_admin_recovery, "member@example.com")
        with pytest.raises(recovery.NoSuchAdministrator):
            client.portal.call(recovery.mint_admin_recovery, "ghost@example.com")


@pytest.mark.db
def test_an_admin_recovery_link_sets_a_password_and_returns_to_login(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "boss4@example.com", "admin")
        link = client.portal.call(recovery.mint_admin_recovery, "boss4@example.com")
        token = link.split("#")[1]
        done = client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW_PASSWORD})
        assert done.status_code == 204
        assert done.headers.get_list("set-cookie") == []
        assert client.get("/api/v1/account").status_code == 401
        assert _login(client, "boss4@example.com", password=NEW_PASSWORD).status_code == 204
