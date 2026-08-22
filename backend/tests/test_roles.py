import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, sessions
from app.main import app
from app.tables import User
from tests.test_invitations import _account, _admin, _sign_in, accounts

__all__ = ["accounts"]


async def _role_of(email: str) -> str:
    async with db.session_factory() as session:
        return (await session.execute(select(User.role).where(User.email == email))).scalar_one()


def _change(client, headers, user_id, **body):
    return client.post(f"/api/v1/users/{user_id}/role", headers=headers, json=body)


@pytest.mark.db
def test_an_administrator_promotes_a_member_who_has_verified_mail(accounts):
    target = _account(accounts, "promote@example.com", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.portal.call(_role_of, "promote@example.com") == "admin"


@pytest.mark.db
def test_promotion_without_verified_mail_needs_an_explicit_attestation(accounts):
    """A no-mail install cannot prove the address, so the administrator says
    on the record that they know who this is."""
    target = _account(accounts, "unverified@example.com", mail_verified=False)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        refused = _change(client, headers, target.id, role="admin")
        assert refused.status_code == 409
        assert _change(client, headers, target.id, role="admin",
                       attested=True).status_code == 204
        assert client.portal.call(_role_of, "unverified@example.com") == "admin"


@pytest.mark.db
def test_the_attestation_is_recorded_against_the_account_it_promoted(accounts):
    target = _account(accounts, "attested@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin", attested=True).status_code == 204
        rows = client.portal.call(_audit)
    # The role check records the attempt before it runs; the route records the
    # outcome, because only it knows who was changed and to what.
    assert any(row["action"] == "POST /api/v1/users/{user_id}/role" for row in rows)
    promoted = [row for row in rows if row["action"] == "user.role"]
    assert len(promoted) == 1
    assert promoted[0]["target_user_id"] == str(target.id)
    assert promoted[0]["object_ids"] == ["admin"]


async def _audit():
    from app import audit
    return await audit.search()


@pytest.mark.db
def test_a_demotion_needs_no_attestation(accounts):
    """Attestation is evidence for handing power over, not for taking it back."""
    target = _account(accounts, "demote@example.com", role="admin")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="user").status_code == 204
        assert client.portal.call(_role_of, "demote@example.com") == "user"


@pytest.mark.db
def test_promotion_requires_recent_authentication(accounts):
    target = _account(accounts, "stale@example.com", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)

        async def go_stale():
            from datetime import datetime, timedelta, timezone

            from sqlalchemy import text
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE sessions SET recent_auth_at = :past"),
                    {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                     - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(go_stale)
        assert _change(client, headers, target.id, role="admin").status_code == 403


@pytest.mark.db
def test_an_administrator_cannot_change_their_own_role(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        me = client.get("/api/v1/account").json()
        assert _change(client, headers, me["id"], role="user").status_code == 403
        assert client.get("/api/v1/account").json()["role"] == "admin"


@pytest.mark.db
def test_a_peer_administrator_can_be_demoted_while_another_remains(accounts):
    other = _account(accounts, "second@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client, email="first@example.com")
        assert _change(client, headers, other.id, role="user").status_code == 204
        assert client.portal.call(_role_of, "second@example.com") == "user"


@pytest.mark.db
def test_the_last_administrator_cannot_be_demoted(accounts):
    """Reached through the count rather than through self-protection: an
    install with no administrator can only be recovered offline.

    Self-protection covers this today, because to be the last administrator is
    to be the caller. The count is what keeps it true once account state
    changes can retire an administrator without demoting them.
    """
    from app.roles import _remaining_administrators

    solo = _account(accounts, "solo@example.com", role="admin")

    async def remaining():
        async with db.session_factory() as session:
            return await _remaining_administrators(session, solo.id)

    with TestClient(app) as client:
        _admin(accounts, client, email="first@example.com")
        assert client.portal.call(remaining) >= 1
        # The implicit local administrator is still one until someone claims
        # the install, so retire both to reach the case that matters.
        client.portal.call(_demote, "first@example.com")
        client.portal.call(_demote, db.LOCAL_USER_EMAIL)
        assert client.portal.call(remaining) == 0


async def _demote(email: str) -> None:
    from sqlalchemy import text

    async with db.session_factory() as session:
        await session.execute(text("UPDATE users SET role = 'user' WHERE email = :e"),
                              {"e": email})
        await session.commit()


async def _user_id(email: str):
    async with db.session_factory() as session:
        return str((await session.execute(
            select(User.id).where(User.email == email))).scalar_one())


@pytest.mark.db
def test_a_role_change_ends_every_session_the_account_held(accounts):
    """The old session carries the old role's session shape and the old
    authority, so it must not survive the change."""
    target = _account(accounts, "rotated@example.com", mail_verified=True)
    with TestClient(app) as client:
        theirs = client.portal.call(sessions.mint, target, False)
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {theirs.token}"}).status_code == 401


@pytest.mark.db
def test_only_an_administrator_may_change_a_role(accounts):
    target = _account(accounts, "target@example.com")
    _account(accounts, "nobody@example.com")
    with TestClient(app) as client:
        headers = _sign_in(client, "nobody@example.com")
        assert _change(client, headers, target.id, role="admin").status_code == 403


@pytest.mark.db
def test_an_unknown_role_is_refused(accounts):
    target = _account(accounts, "weird@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="superuser").status_code == 422


@pytest.mark.db
def test_changing_the_role_of_an_account_that_does_not_exist(accounts):
    import uuid as _uuid

    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, _uuid.uuid4(), role="user").status_code == 404


@pytest.mark.db
def test_setting_the_same_role_again_still_ends_the_sessions(accounts):
    """The role change commits before the sessions are revoked. If that second
    step fails, the operator retries, and a retry that short-circuits on
    "already that role" would leave the old sessions alive for good."""
    target = _account(accounts, "retry@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        theirs = client.portal.call(sessions.mint, target, False)
        headers = _admin(accounts, client, email="first@example.com")
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {theirs.token}"}).status_code == 401
