"""What an account state is for: taking capability away and giving it back.

Cancelled is a job state and never an account state, and an account that
cannot sign in cannot sign in through any door.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db
from app.main import app
from app.tables import AssetShare, Session, User
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]

TERMINAL = ("disabled", "deletion_pending", "purging")


def _set_state(client, user_id, state):
    return client.post(f"/api/v1/users/{user_id}/state", headers=_csrf(client),
                       json={"state": state})


async def _state_of(user_id: uuid.UUID) -> str:
    async with db.session_factory() as session:
        return (await session.execute(
            select(User.state).where(User.id == user_id))).scalar_one()


async def _live_sessions(user_id: uuid.UUID) -> int:
    async with db.session_factory() as session:
        return len((await session.execute(
            select(Session).where(Session.user_id == user_id,
                                  Session.revoked_at.is_(None)))).scalars().all())


def _admin(client, email="stateadmin@example.com"):
    client.portal.call(_make, email, "admin")
    assert _login(client, email).status_code == 204


def _session_cookie(client) -> str:
    return next(c.value for c in client.cookies.jar
                if c.name.endswith("potocolom_session"))


def _wearing(client, token: str) -> None:
    """One client, two people. Nesting TestClients would work until the inner
    lifespan disposed the engine the outer one is still using."""
    for name in ("__Host-potocolom_session", "potocolom_session"):
        client.cookies.delete(name, path="/")
    client.cookies.set("__Host-potocolom_session", token)


@pytest.mark.db
@pytest.mark.parametrize("state", ["suspended", "disabled", "deletion_pending"])
def test_an_administrator_moves_an_account_between_states(accounts, state):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, f"subject-{state}@example.com")
        _admin(client)
        assert _set_state(client, subject.id, state).status_code == 204
        assert client.portal.call(_state_of, subject.id) == state


@pytest.mark.db
def test_setting_the_state_an_account_already_holds_changes_nothing(accounts):
    """Idempotent: a retry after a timeout must not be a second event, in the
    row or in the record of who did what."""
    from app.tables import AuditEvent

    async def recorded(user_id) -> int:
        async with db.session_factory() as session:
            return len((await session.execute(
                select(AuditEvent).where(AuditEvent.action == "account.state",
                                         AuditEvent.target_user_id == user_id)
            )).scalars().all())

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "twice@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        assert _set_state(client, subject.id, "suspended").status_code == 204
        assert client.portal.call(_state_of, subject.id) == "suspended"
        assert client.portal.call(recorded, subject.id) == 1


@pytest.mark.db
@pytest.mark.parametrize("state", ["cancelled", "active ", "", "queued", "purging"])
def test_only_the_states_an_account_can_hold_are_accepted(accounts, state):
    """Cancelled is a job state. Purging belongs to the deletion sweep, not to
    an administrator with a form."""
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "shapes@example.com")
        _admin(client)
        assert _set_state(client, subject.id, state).status_code == 422
        assert client.portal.call(_state_of, subject.id) == "active"


@pytest.mark.db
def test_a_terminal_state_is_not_a_way_back_to_active(accounts):
    """Restoration from deletion is R14's, with the data behind it. Until
    then a disabled account can come back and a deleted one cannot."""
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "oneway@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "disabled").status_code == 204
        assert _set_state(client, subject.id, "active").status_code == 204
        assert _set_state(client, subject.id, "deletion_pending").status_code == 204
        assert _set_state(client, subject.id, "active").status_code == 409
        assert client.portal.call(_state_of, subject.id) == "deletion_pending"


@pytest.mark.db
def test_an_administrator_cannot_change_their_own_state(accounts):
    """The same rule as roles: nobody removes their own authority by accident,
    and nobody restores it either."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "selfstate@example.com", "admin")
        assert _login(client, "selfstate@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
        assert _set_state(client, me["id"], "suspended").status_code == 403


@pytest.mark.db
def test_an_administrator_may_suspend_another_one(accounts):
    """Peer changes protect the last administrator, and two is not one."""
    with TestClient(app, base_url=ORIGIN) as client:
        peer = client.portal.call(_make, "peer13@example.com", "admin")
        _admin(client)
        assert _set_state(client, peer.id, "suspended").status_code == 204
        assert client.portal.call(_state_of, peer.id) == "suspended"


@pytest.mark.db
def test_leaving_active_ends_every_session_the_account_held(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "evicted@example.com")
        assert _login(client, "evicted@example.com").status_code == 204
        theirs = _session_cookie(client)
        assert client.get("/api/v1/account").status_code == 200

        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        assert client.portal.call(_live_sessions, subject.id) == 0

        _wearing(client, theirs)
        assert client.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_a_suspended_account_signs_in_and_may_change_nothing(accounts):
    """A pause, not a deletion: they may read their work and settle up."""
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "paused@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        assert _login(client, "paused@example.com").status_code == 204
        assert client.get("/api/v1/account").status_code == 200
        refused = client.post("/api/v1/generations", headers=_csrf(client),
                              json={"model_id": "sd-test", "params": {"prompt": "a cat"}})
        assert refused.status_code == 403


@pytest.mark.db
@pytest.mark.parametrize("state", TERMINAL)
def test_an_account_in_a_terminal_state_cannot_sign_in(accounts, state):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, f"gone-{state}@example.com")

        async def force() -> None:
            async with db.session_factory() as session:
                await session.execute(text("UPDATE users SET state = :state WHERE id = :id"),
                                      {"state": state, "id": subject.id})
                await session.commit()

        client.portal.call(force)
        assert _login(client, f"gone-{state}@example.com").status_code == 401


@pytest.mark.db
def test_suspending_an_account_pauses_the_links_it_shared(accounts):
    """A share is the account speaking to the public. A paused account is not
    speaking, and restoring it restores what it said."""
    from tests.test_shares import _owned_asset, _resolve, _share, _token_of

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "sharer13@example.com")
        asset = client.portal.call(_owned_asset, subject.id)
        assert _login(client, "sharer13@example.com").status_code == 204
        token = _token_of(_share(client, asset.id))
        assert _resolve(client, token).status_code == 200

        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        assert _resolve(client, token).status_code == 404

        async def shares() -> list[AssetShare]:
            async with db.session_factory() as session:
                return list((await session.execute(select(AssetShare))).scalars().all())

        # Paused, not revoked: the link comes back with the account.
        assert client.portal.call(shares)[0].revoked_at is None
        assert _set_state(client, subject.id, "active").status_code == 204
        assert _resolve(client, token).status_code == 200

        async def clear() -> None:
            async with db.session_factory() as session:
                for table in ("asset_shares", "assets", "jobs"):
                    await session.execute(text(f"DELETE FROM {table}"))
                await session.commit()

        client.portal.call(clear)


@pytest.mark.db
def test_a_state_change_is_audited_with_the_account_it_changed(accounts):
    from app.tables import AuditEvent

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "audited13@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204

        async def events() -> list[AuditEvent]:
            async with db.session_factory() as session:
                return list((await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "account.state")
                )).scalars().all())

        recorded = client.portal.call(events)
    assert len(recorded) == 1
    assert recorded[0].target_user_id == subject.id
    assert "suspended" in recorded[0].object_ids


@pytest.mark.db
def test_only_an_administrator_can_change_a_state(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "target13@example.com")
        client.portal.call(_make, "meddler13@example.com")
        assert _login(client, "meddler13@example.com").status_code == 204
        assert _set_state(client, subject.id, "suspended").status_code == 403
        assert client.portal.call(_state_of, subject.id) == "active"


@pytest.mark.db
def test_one_account_cannot_call_off_another_ones_work(accounts):
    """A job belongs to whoever created it. An administrator may stop one,
    because an administrator may take the whole account away."""
    from tests.test_shares import _owned_asset

    with TestClient(app, base_url=ORIGIN) as client:
        owner = client.portal.call(_make, "jobowner@example.com")
        asset = client.portal.call(_owned_asset, owner.id)
        job_id = asset.job_id

        client.portal.call(_make, "stranger13@example.com")
        assert _login(client, "stranger13@example.com").status_code == 204
        assert client.post(f"/api/v1/generations/{job_id}/cancel",
                           headers=_csrf(client)).status_code == 404

        _admin(client, "canceller@example.com")
        assert client.post(f"/api/v1/generations/{job_id}/cancel",
                           headers=_csrf(client)).status_code == 204

        async def clear() -> None:
            async with db.session_factory() as session:
                for table in ("asset_shares", "assets", "jobs"):
                    await session.execute(text(f"DELETE FROM {table}"))
                await session.commit()

        client.portal.call(clear)
