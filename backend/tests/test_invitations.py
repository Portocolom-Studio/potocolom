import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db
from app.main import app
from app.passwords import hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, Invitation, User

PASSWORD = "a-long-enough-account-password"
ORIGIN = "http://localhost:8000"


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    monkeypatch.setenv("ROOT_KEYS", "1:" + "A" * 43 + "=")
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
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
            for table in ("sessions", "auth_identities", "auth_tokens", "invitations",
                          "audit_events", "installation_auth_state"):
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


async def _make(email: str, role: str = "user", state: str = "active",
                mail_verified: bool = False) -> User:
    async with db.session_factory() as session:
        user = User(id=uuid.uuid4(), email=email, role=role, state=state,
                    mail_verified=mail_verified)
        session.add(user)
        await session.flush()
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                 subject=email.lower(), password_hash=hash_password(PASSWORD)))
        await session.commit()
        return user


def _account(portal_runner, email, role="user", state="active", mail_verified=False) -> User:
    """Only before a TestClient exists. Once one is live its lifespan owns the
    engine, and writing from the test loop corrupts a pooled connection."""
    return portal_runner(_make(email, role, state, mail_verified))


def _sign_in(client, email):
    assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                       json={"email": email, "password": PASSWORD,
                             "remember_me": False}).status_code == 204
    value = next(c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf"))
    return {"Origin": ORIGIN, "X-CSRF-Token": value}


def _admin(portal_runner, client, email="boss@example.com"):
    # Through the client's portal: it is already live here, so its lifespan
    # owns the engine and the test loop must not touch it.
    client.portal.call(_make, email, "admin")
    return _sign_in(client, email)


@pytest.mark.db
def test_an_invitation_is_email_bound_one_use_and_lasts_seventy_two_hours(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        created = client.post("/api/v1/invitations", headers=headers,
                              json={"email": "Guest@Example.com", "role": "user"})
        assert created.status_code == 201
        body = created.json()
        assert body["email"] == "Guest@Example.com"
        assert body["role"] == "user"
        assert body["token"]

        async def stored():
            async with db.session_factory() as session:
                return (await session.execute(select(Invitation))).scalar_one()

        row = client.portal.call(stored)
        assert row.accepted_at is None and row.revoked_at is None
        assert timedelta(hours=71) < row.expires_at - datetime.now(timezone.utc) < timedelta(
            hours=73)
        # Only the hash is durable; the link exists in the operator's clipboard.
        assert body["token"] not in str(row.token_hash)


@pytest.mark.db
def test_only_an_administrator_may_invite(accounts):
    _account(accounts, "member@example.com")
    with TestClient(app) as client:
        headers = _sign_in(client, "member@example.com")
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "guest@example.com", "role": "user"}).status_code == 403


@pytest.mark.db
def test_an_invitation_can_be_accepted_once(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "newcomer@example.com", "role": "user"}).json()["token"]
    with TestClient(app) as client:
        accepted = client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                               json={"token": token, "password": PASSWORD})
        assert accepted.status_code == 204
        # Invitation completion creates a clean session.
        me = client.get("/api/v1/account").json()
        assert me["email"] == "newcomer@example.com"
        assert me["role"] == "user"
        assert me["recent_auth"] is False
        replay = client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                             json={"token": token, "password": PASSWORD})
        assert replay.status_code == 403


@pytest.mark.db
def test_the_invited_address_is_the_account_that_gets_made(accounts):
    """Email-bound: the claimant cannot redirect the invitation elsewhere."""
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "bound@example.com", "role": "viewer"}).json()["token"]
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": token, "password": PASSWORD,
                                 "email": "attacker@example.com"}).status_code == 204
        assert client.get("/api/v1/account").json()["email"] == "bound@example.com"


@pytest.mark.db
def test_an_expired_invitation_is_refused(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "late@example.com", "role": "user"}).json()["token"]

        async def age():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE invitations SET expires_at = :past"),
                                      {"past": datetime.now(timezone.utc) - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(age)
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": token, "password": PASSWORD}).status_code == 403


@pytest.mark.db
def test_a_revoked_invitation_is_refused(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        created = client.post("/api/v1/invitations", headers=headers,
                              json={"email": "gone@example.com", "role": "user"}).json()
        assert client.delete(f"/api/v1/invitations/{created['id']}",
                             headers=headers).status_code == 204
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": created["token"],
                                 "password": PASSWORD}).status_code == 403


@pytest.mark.db
def test_revealing_again_re_mints_and_kills_the_link_it_replaces(accounts):
    """Mail that never arrived leaves a link nobody can see. Showing it again
    has to assume the first one leaked on the way."""
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        first = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "resend@example.com", "role": "user"}).json()
        second = client.post(f"/api/v1/invitations/{first['id']}/reveal", headers=headers)
        assert second.status_code == 200
        assert second.json()["token"] != first["token"]
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": first["token"],
                                 "password": PASSWORD}).status_code == 403
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": second.json()["token"],
                                 "password": PASSWORD}).status_code == 204


@pytest.mark.db
def test_one_open_invitation_per_address(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "twice@example.com", "role": "user"}).status_code == 201
        again = client.post("/api/v1/invitations", headers=headers,
                            json={"email": " TWICE@example.com ", "role": "user"})
        assert again.status_code == 409


@pytest.mark.db
def test_an_invitation_cannot_be_sent_to_an_address_that_already_has_an_account(accounts):
    _account(accounts, "already@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "Already@example.com",
                                 "role": "user"}).status_code == 409


@pytest.mark.db
@pytest.mark.parametrize("role", ["viewer", "user", "admin"])
def test_every_role_can_be_invited(accounts, role):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        created = client.post("/api/v1/invitations", headers=headers,
                              json={"email": f"{role}@example.com", "role": role})
        assert created.status_code == 201
        token = created.json()["token"]
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": token, "password": PASSWORD}).status_code == 204
        assert client.get("/api/v1/account").json()["role"] == role


@pytest.mark.db
def test_an_unknown_role_is_refused(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "root@example.com",
                                 "role": "superuser"}).status_code == 422


@pytest.mark.db
def test_a_weak_password_leaves_the_invitation_usable(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "weak@example.com", "role": "user"}).json()["token"]
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": token, "password": "short"}).status_code == 400
        assert client.post("/api/v1/auth/register", headers={"Origin": ORIGIN},
                           json={"token": token, "password": PASSWORD}).status_code == 204


@pytest.mark.db
def test_creating_an_invitation_is_audited(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        client.post("/api/v1/invitations", headers=headers,
                    json={"email": "audited@example.com", "role": "admin"})
        actions = [row["action"] for row in client.portal.call(_audit)]
    assert "POST /api/v1/invitations" in actions


async def _audit():
    from app import audit
    return await audit.search()


@pytest.mark.db
def test_inviting_an_administrator_needs_the_same_evidence_as_promoting_one(accounts):
    """Both routes end at a live administrator. Charging only one of them for
    it means the cheaper one is the one an attacker uses."""
    with TestClient(app) as client:
        headers = _admin(accounts, client)

        async def go_stale():
            from datetime import datetime, timedelta, timezone

            from app import sessions
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE sessions SET recent_auth_at = :past"),
                    {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                     - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(go_stale)
        stale = client.post("/api/v1/invitations", headers=headers,
                            json={"email": "climb@example.com", "role": "admin"})
        assert stale.status_code == 403
        # A lesser role is not the thing being guarded.
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "ordinary@example.com",
                                 "role": "user"}).status_code == 201


@pytest.mark.db
def test_an_administrator_invitation_records_who_it_was_for(accounts):
    """The generic record names the route and the caller. Without the address
    and the role, the trail cannot say an administrator was created."""
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert client.post("/api/v1/invitations", headers=headers,
                           json={"email": "Named@example.com",
                                 "role": "admin"}).status_code == 201
        rows = client.portal.call(_audit)
    created = [row for row in rows if row["action"] == "invitation.created"]
    assert len(created) == 1
    assert created[0]["object_ids"] == ["Named@example.com", "admin"]
