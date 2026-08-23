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


async def _live_tokens(user_id: uuid.UUID) -> list:
    from app.tables import AuthToken
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthToken).where(AuthToken.user_id == user_id,
                                    AuthToken.consumed_at.is_(None))
        )).scalars().all())


@pytest.mark.db
def test_changing_a_credential_kills_a_reset_link_already_in_flight(accounts):
    """The reason to change an address after a mailbox is compromised is to
    end what that mailbox can still do. A link already sent is exactly that."""
    from app import recovery

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "inflight@example.com")
        token = client.portal.call(recovery.mint_reset, "inflight@example.com")
        assert _login(client, "inflight@example.com").status_code == 204
        assert _change_email(client, "safe@example.com").status_code == 204
        assert client.portal.call(_live_tokens, user.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_changing_a_password_kills_a_reset_link_too(accounts):
    from app import recovery

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "inflight2@example.com")
        token = client.portal.call(recovery.mint_reset, "inflight2@example.com")
        assert _login(client, "inflight2@example.com").status_code == 204
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        assert client.portal.call(_live_tokens, user.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_a_promotion_kills_a_reset_link_minted_before_it(accounts):
    """A reset is refused for an administrator. A link minted while the account
    was not one, and spent after it became one, is an administrator password
    from mailbox control alone."""
    from app import recovery
    from tests.test_totp_flow import _make as make

    with TestClient(app, base_url=ORIGIN) as client:
        target = client.portal.call(make, "climber@example.com")
        token = client.portal.call(recovery.mint_reset, "climber@example.com")
        boss = client.portal.call(make, "chief@example.com", "admin")
        admin_token = client.portal.call(sessions.mint, boss, False, True).token
        promoted = client.post(f"/api/v1/users/{target.id}/role",
                               headers={"Authorization": f"Bearer {admin_token}"},
                               json={"role": "admin", "attested": True})
        assert promoted.status_code == 204
        assert client.portal.call(_live_tokens, target.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_two_unlinks_at_once_cannot_empty_an_account(accounts):
    """Each sees two credentials and removes a different one, and the account
    is left with none, which is the state the guard exists to prevent."""
    import asyncio

    from app import credentials

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "racer@example.com")
        client.portal.call(_link, user.id, "google", "g-race")
        client.portal.call(_link, user.id, "github", "h-race")
        assert _login(client, "racer@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        principal = client.portal.call(sessions.resolve,
                                       next(c.value for c in client.cookies.jar
                                            if c.name.endswith("potocolom_session")))

        async def both():
            return await asyncio.gather(
                credentials.unlink_identity("google", principal),
                credentials.unlink_identity("github", principal),
                return_exceptions=True,
            )

        client.portal.call(both)
        left = client.portal.call(_identities, user.id)
    assert len(left) >= 1


@pytest.mark.db
def test_an_address_cannot_be_a_list_of_addresses(accounts):
    """It becomes the To header of mail this install sends, so a second
    recipient there is the install addressing a stranger."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lister@example.com")
        assert _login(client, "lister@example.com").status_code == 204
        for bad in ("me@evil.com, victim@corp.com", "me@evil.com;victim@corp.com",
                    "a@b.co <victim@corp.com>", "two@@example.com", "sp ace@example.com"):
            assert _change_email(client, bad).status_code == 400, bad
