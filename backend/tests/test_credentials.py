import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, sessions, totp
from app.main import app
from app.passwords import verify_password
from app.tables import AuthIdentity, User
from tests.test_totp_flow import ORIGIN, PASSWORD, _csrf, _enrol, _login, _make, _now, accounts

__all__ = ["accounts"]

NEW = "a-brand-new-long-enough-password"


async def _identities(user_id: uuid.UUID) -> list[AuthIdentity]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthIdentity).where(AuthIdentity.user_id == user_id)
        )).scalars().all())


async def _user(email: str) -> User:
    async with db.session_factory() as session:
        return (await session.execute(
            select(User).where(User.email == email))).scalar_one()


async def _link(user_id: uuid.UUID, provider: str, subject: str) -> None:
    async with db.session_factory() as session:
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user_id, provider=provider,
                                 subject=subject))
        await session.commit()


def _change_password(client, current, new):
    return client.post("/api/v1/account/password", headers=_csrf(client),
                       json={"current_password": current, "password": new})


@pytest.mark.db
def test_changing_a_password_needs_the_current_one(accounts):
    """Recent authentication says this browser was somebody's. The current
    password says it is still theirs at this keyboard."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "changer@example.com")
        assert _login(client, "changer@example.com").status_code == 204
        assert _change_password(client, "the-wrong-password", NEW).status_code == 403
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        stored = client.portal.call(_identities,
                                    client.portal.call(_user, "changer@example.com").id)
    password = next(row for row in stored if row.provider == "password")
    assert verify_password(password.password_hash, NEW) is True
    assert verify_password(password.password_hash, PASSWORD) is False


@pytest.mark.db
def test_changing_a_password_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "stalechange@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert _change_password(client, PASSWORD, NEW).status_code == 403


@pytest.mark.db
def test_a_weak_new_password_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "weakchange@example.com")
        assert _login(client, "weakchange@example.com").status_code == 204
        assert _change_password(client, PASSWORD, "short").status_code == 400
        assert _change_password(client, PASSWORD, "passwordpassword").status_code == 400


@pytest.mark.db
def test_changing_a_password_ends_every_other_session(accounts):
    """The reason to change a password is usually that somebody else has it."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "kickother@example.com")
        elsewhere = client.portal.call(sessions.mint, user, False)
        assert _login(client, "kickother@example.com").status_code == 204
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {elsewhere.token}"}
                          ).status_code == 401
        # The browser that made the change keeps working.
        assert client.get("/api/v1/account").status_code == 200


@pytest.mark.db
def test_an_account_with_no_password_can_add_one(accounts):
    """An account that only ever signed in with a provider still needs a way
    in when the provider is unavailable."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "haspassword@example.com")
        assert _login(client, "haspassword@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        added = client.post("/api/v1/account/password", headers=_csrf(client),
                            json={"password": NEW})
        assert added.status_code == 204
        rows = client.portal.call(_identities, user.id)
    assert [row.provider for row in rows if row.provider == "password"] == ["password"]


@pytest.mark.db
def test_adding_a_password_still_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "addstale@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert client.post("/api/v1/account/password", headers=_csrf(client),
                           json={"password": NEW}).status_code == 403


def _change_email(client, address):
    return client.post("/api/v1/account/email", headers=_csrf(client),
                       json={"email": address})


@pytest.mark.db
def test_changing_the_primary_address_drops_the_assurance_with_it(accounts):
    """A provider proved the old address. It says nothing about the new one."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "assured@example.com")
        assert _login(client, "assured@example.com").status_code == 204

        async def assure():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE users SET mail_verified = true "
                                           "WHERE id = :id"), {"id": user.id})
                await session.commit()

        client.portal.call(assure)
        assert client.get("/api/v1/account").json()["mail_verified"] is True
        assert _change_email(client, "moved@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
    assert me["email"] == "moved@example.com"
    assert me["mail_verified"] is False


@pytest.mark.db
def test_the_password_identity_follows_the_address(accounts):
    """Login matches on the identity subject, so leaving it behind would sign
    somebody in under an address they no longer hold."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "renamed@example.com")
        assert _login(client, "renamed@example.com").status_code == 204
        assert _change_email(client, "NewName@example.com").status_code == 204
        rows = client.portal.call(_identities, user.id)
    password = next(row for row in rows if row.provider == "password")
    assert password.subject == "newname@example.com"
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "newname@example.com").status_code == 204
        assert _login(fresh, "renamed@example.com").status_code == 401


@pytest.mark.db
def test_an_address_somebody_else_holds_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "taken@example.com")
        client.portal.call(_make, "mover@example.com")
        assert _login(client, "mover@example.com").status_code == 204
        assert _change_email(client, "Taken@example.com").status_code == 409
        assert client.get("/api/v1/account").json()["email"] == "mover@example.com"


@pytest.mark.db
def test_changing_the_address_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "staleemail@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert _change_email(client, "other@example.com").status_code == 403


@pytest.mark.db
def test_unlinking_a_provider_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "unlinker@example.com")
        client.portal.call(_link, user.id, "google", "g-unlink")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert client.delete("/api/v1/account/identities/google",
                             headers=_csrf(client)).status_code == 403


@pytest.mark.db
def test_unlinking_a_provider_removes_it(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "dropgoogle@example.com")
        client.portal.call(_link, user.id, "google", "g-drop")
        assert _login(client, "dropgoogle@example.com").status_code == 204
        assert client.delete("/api/v1/account/identities/google",
                             headers=_csrf(client)).status_code == 204
        rows = client.portal.call(_identities, user.id)
    assert {row.provider for row in rows} == {"password"}


@pytest.mark.db
def test_the_last_way_in_cannot_be_unlinked(accounts):
    """An account with no credential at all can only be recovered offline."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "onlygoogle@example.com")
        client.portal.call(_link, user.id, "google", "g-only")
        assert _login(client, "onlygoogle@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        refused = client.delete("/api/v1/account/identities/google", headers=_csrf(client))
        assert refused.status_code == 409
        rows = client.portal.call(_identities, user.id)
    assert {row.provider for row in rows} == {"google"}


@pytest.mark.db
def test_unlinking_ends_every_other_session(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "unlinkkick@example.com")
        client.portal.call(_link, user.id, "github", "h-kick")
        elsewhere = client.portal.call(sessions.mint, user, False)
        assert _login(client, "unlinkkick@example.com").status_code == 204
        assert client.delete("/api/v1/account/identities/github",
                             headers=_csrf(client)).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {elsewhere.token}"}
                          ).status_code == 401


@pytest.mark.db
def test_a_credential_change_by_an_enrolled_account_still_passes_the_factor(accounts):
    """Recent authentication is what these routes require, and the only way to
    have it is to have passed the gate."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "gatedchange@example.com")
        assert _login(client, "gatedchange@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "gatedchange@example.com").status_code == 200
        # Mid-challenge there is no session at all, so nothing can be changed.
        assert _change_password(fresh, PASSWORD, NEW).status_code == 401
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": totp.code_at(secret, int(_now()))}).status_code == 204
        assert _change_password(fresh, PASSWORD, NEW).status_code == 204
