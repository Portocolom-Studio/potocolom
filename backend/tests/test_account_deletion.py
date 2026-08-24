"""Leaving, and being let back in.

An export is everything the install holds about one account. A deletion is a
request with a waiting period behind it, reversible until the purge runs, and
a purge leaves no user row at all.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.main import app
from app.tables import Asset, Job, User
from tests.test_account_states import _admin, _session_cookie, _set_state, _state_of, _wearing
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]


@pytest.fixture
def library(accounts):
    """The accounts fixture empties users, and a job row would hold one down."""
    yield accounts

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("asset_shares", "assets", "jobs"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()

    if db.session_factory is None:
        accounts(db.connect())
    accounts(clear())
    accounts(db.dispose())


async def _user(user_id: uuid.UUID) -> User | None:
    async with db.session_factory() as session:
        return await session.get(User, user_id)


async def _rows(table: str, user_id: uuid.UUID) -> int:
    async with db.session_factory() as session:
        return int(await session.scalar(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :id"), {"id": user_id}) or 0)


async def _owned_work(user_id: uuid.UUID) -> Asset:
    """A job and an asset, which are the two tables a purge has to order."""
    from tests.test_shares import MODEL
    from app.tables import Model

    async with db.session_factory() as session:
        if await session.get(Model, MODEL) is None:
            session.add(Model(id=MODEL, name="SD Share", capabilities=["text_to_image"],
                              parameters_schema={}, min_vram_gb=0))
            await session.flush()
        job = Job(id=uuid.uuid4(), user_id=user_id, model_id=MODEL,
                  params={"prompt": "a boat"}, state="succeeded")
        session.add(job)
        await session.flush()
        asset = Asset(id=uuid.uuid4(), user_id=user_id, job_id=job.id,
                      storage_key=f"{user_id}/boat.png", mime="image/png",
                      width=32, height=32)
        session.add(asset)
        await session.commit()
        return asset


async def _age_request(user_id: uuid.UUID, days: int) -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE users SET deletion_requested_at = :when WHERE id = :id"),
            {"when": datetime.now(timezone.utc) - timedelta(days=days), "id": user_id})
        await session.commit()


@pytest.mark.db
def test_an_account_can_take_everything_the_install_holds_about_it(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "exporter@example.com")
        client.portal.call(_owned_work, user.id)
        assert _login(client, "exporter@example.com").status_code == 204
        exported = client.get("/api/v1/account/export")
        assert exported.status_code == 200
        body = exported.json()
    assert body["account"]["email"] == "exporter@example.com"
    assert body["account"]["id"] == str(user.id)
    assert len(body["generations"]) == 1
    assert body["generations"][0]["params"] == {"prompt": "a boat"}
    assert len(body["generations"][0]["assets"]) == 1
    assert [identity["provider"] for identity in body["identities"]] == ["password"]


@pytest.mark.db
def test_an_export_carries_no_secret_of_any_kind(library):
    """It is a file that leaves the building. A password hash in it is an
    offline cracking target, and a session hash is a live credential."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "nosecrets@example.com")
        assert _login(client, "nosecrets@example.com").status_code == 204
        body = client.get("/api/v1/account/export").json()
    flat = str(body)
    for secret in ("password_hash", "token_hash", "secret_ciphertext", "code_hash",
                   "$argon2", "key_version"):
        assert secret not in flat, secret


@pytest.mark.db
def test_asking_to_be_deleted_stops_the_account_and_keeps_the_way_back(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "leaving@example.com")
        assert _login(client, "leaving@example.com").status_code == 204
        theirs = _session_cookie(client)
        assert client.delete("/api/v1/account", headers=_csrf(client)).status_code == 204

        _wearing(client, theirs)
        assert client.get("/api/v1/account").status_code == 401
        assert _login(client, "leaving@example.com").status_code == 401
        row = client.portal.call(_user, user.id)
    assert row.state == "deletion_pending"
    assert row.prior_state == "active"
    assert row.deletion_requested_at is not None


@pytest.mark.db
def test_a_suspended_account_that_leaves_comes_back_suspended(library):
    """One level of prior state, which is the level that means anything: an
    account restored out of deletion is not thereby un-suspended."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "pausedleaver@example.com")
        _admin(client)
        assert _set_state(client, user.id, "suspended").status_code == 204

        async def request_deletion() -> None:
            from app import deletion
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        assert client.post(f"/api/v1/users/{user.id}/restore",
                           headers=_csrf(client)).status_code == 204
        assert client.portal.call(_state_of, user.id) == "suspended"


@pytest.mark.db
def test_restoring_twice_says_the_same_thing(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "backagain@example.com")
        _admin(client)

        async def request_deletion() -> None:
            from app import deletion
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        assert client.post(f"/api/v1/users/{user.id}/restore",
                           headers=_csrf(client)).status_code == 204
        assert client.post(f"/api/v1/users/{user.id}/restore",
                           headers=_csrf(client)).status_code == 204
        assert client.portal.call(_state_of, user.id) == "active"


@pytest.mark.db
def test_an_account_that_never_left_cannot_be_restored(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "stillhere@example.com")
        _admin(client)
        assert client.post(f"/api/v1/users/{user.id}/restore",
                           headers=_csrf(client)).status_code == 409


@pytest.mark.db
def test_the_sweep_waits_out_the_window_before_it_purges(library):
    from app import deletion

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "waiting@example.com")
        _admin(client)

        async def request_deletion() -> None:
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        client.portal.call(deletion.purge_due)
        assert client.portal.call(_user, user.id) is not None

        client.portal.call(_age_request, user.id, deletion.RESTORE_WINDOW_DAYS + 1)
        client.portal.call(deletion.purge_due)
        assert client.portal.call(_user, user.id) is None


@pytest.mark.db
def test_a_purge_removes_the_work_before_the_row_that_owns_it(library):
    """Foreign keys decide the order: an asset points at a job and a job at
    the account, so the account cannot go first."""
    from app import deletion

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "purged@example.com")
        client.portal.call(_owned_work, user.id)
        assert _login(client, "purged@example.com").status_code == 204
        assert client.portal.call(_rows, "sessions", user.id) == 1

        async def request_deletion() -> None:
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        client.portal.call(_age_request, user.id, deletion.RESTORE_WINDOW_DAYS + 1)
        client.portal.call(deletion.purge_due)

        assert client.portal.call(_user, user.id) is None
        for table in ("jobs", "assets", "sessions", "auth_identities"):
            assert client.portal.call(_rows, table, user.id) == 0, table


@pytest.mark.db
def test_a_purge_that_runs_twice_finishes_the_same_way(library):
    from app import deletion

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "twicepurged@example.com")
        client.portal.call(_owned_work, user.id)

        async def request_deletion() -> None:
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        client.portal.call(_age_request, user.id, deletion.RESTORE_WINDOW_DAYS + 1)
        client.portal.call(deletion.purge_due)
        client.portal.call(deletion.purge_due)
        assert client.portal.call(_user, user.id) is None


@pytest.mark.db
def test_the_objects_of_a_purged_account_are_taken_with_it(library):
    from app import deletion

    removed: list[str] = []

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "objects@example.com")
        asset = client.portal.call(_owned_work, user.id)

        async def request_deletion() -> None:
            async with db.session_factory() as session:
                await deletion.request(session, user.id)
                await session.commit()

        client.portal.call(request_deletion)
        client.portal.call(_age_request, user.id, deletion.RESTORE_WINDOW_DAYS + 1)

        original = deletion._forget_object

        async def watch(key: str) -> None:
            removed.append(key)
            await original(key)

        deletion._forget_object = watch
        try:
            client.portal.call(deletion.purge_due)
        finally:
            deletion._forget_object = original
    assert asset.storage_key in removed


@pytest.mark.db
def test_the_last_administrator_may_still_delete_their_own_account(library):
    """An install with nobody in charge is recoverable offline. An
    administrator held hostage by their own install is not."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lastadmin@example.com", "admin")
        assert _login(client, "lastadmin@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
        assert client.delete("/api/v1/account", headers=_csrf(client)).status_code == 204
        assert client.portal.call(_state_of, uuid.UUID(me["id"])) == "deletion_pending"


@pytest.mark.db
def test_only_an_administrator_can_restore_somebody_else(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "restoreme@example.com")
        client.portal.call(_make, "meddler14@example.com")
        assert _login(client, "meddler14@example.com").status_code == 204
        assert client.post(f"/api/v1/users/{user.id}/restore",
                           headers=_csrf(client)).status_code == 403


@pytest.mark.db
def test_an_export_needs_a_session_of_its_own(library):
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/account/export").status_code == 401
