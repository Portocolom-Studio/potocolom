"""Fleet members (issue #885, first PR): one join token per machine, spent once
for that machine's own credential. Only SHA-256 digests of the secrets are stored.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import audit, auth_mode, db, realtime, sessions
from app.main import app
from app.passwords import hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, LoginAttempt, User

ORIGIN = "http://localhost:8000"
PASSWORD = "a-long-enough-account-password"
BASE_URL = "https://worker.example:8443/"
MEMBERS = "/api/v1/admin/fleet/members"
ENROLL = "/api/v1/fleet/enroll"
ADMIN_EMAIL = "boss@example.com"

pytestmark = pytest.mark.db  # every test here needs PostgreSQL


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


async def _wipe_for_accounts() -> None:
    async with db.session_factory() as session:
        for table in ("fleet_members", "sessions", "auth_identities", "auth_tokens",
                      "invitations", "audit_events", "mail_outbox", "suppressed_addresses",
                      "installation_auth_state"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text("DELETE FROM users WHERE email <> :local"),
                              {"local": db.LOCAL_USER_EMAIL})
        await session.execute(
            text("UPDATE users SET role = 'admin', state = 'active', mail_verified = false "
                 "WHERE email = :local"), {"local": db.LOCAL_USER_EMAIL})
        await session.commit()


async def _wipe_for_none() -> None:
    async with db.session_factory() as session:
        await session.execute(text("UPDATE jobs SET source_asset_id = NULL"))
        for table in ("fleet_members", "auth_tokens", "auth_identities", "sessions",
                      "audit_events", "assets", "jobs", "installation_auth_state"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.execute(text("DELETE FROM users"))
        await session.commit()


@pytest.fixture
def accounts(portal_runner, monkeypatch):
    monkeypatch.setenv("ROOT_KEYS", "1:" + "A" * 43 + "=")
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    portal_runner(_wipe_for_accounts())
    portal_runner(db.enable_accounts_mode(db.session_factory))
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    try:
        yield portal_runner
    finally:
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(_wipe_for_accounts())
        portal_runner(db.dispose())
        get_settings.cache_clear()


@pytest.fixture
def none_mode(portal_runner, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    get_settings.cache_clear()
    assert portal_runner(db.connect(serving=False)) is True
    portal_runner(_wipe_for_none())
    portal_runner(db.dispose())
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    assert portal_runner(db.connect()) is True
    try:
        yield portal_runner
    finally:
        if db.session_factory is None:
            portal_runner(db.connect(serving=False))
        portal_runner(_wipe_for_none())
        portal_runner(db.dispose())
        get_settings.cache_clear()


async def _make(email: str, role: str = "user") -> User:
    async with db.session_factory() as session:
        user = User(id=uuid.uuid4(), email=email, role=role, state="active",
                    mail_verified=False)
        session.add(user)
        await session.flush()
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user.id, provider="password",
                                 subject=email.lower(), password_hash=hash_password(PASSWORD)))
        await session.commit()
        return user


def _account(portal_runner, email: str, role: str = "user") -> None:
    """Only before a TestClient exists: its lifespan owns the engine afterwards."""
    portal_runner(_make(email, role))


def _sign_in(client, email: str = ADMIN_EMAIL) -> dict:
    assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                       json={"email": email, "password": PASSWORD,
                             "remember_me": False}).status_code == 204
    value = next(c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf"))
    return {"Origin": ORIGIN, "X-CSRF-Token": value}


def _admin(portal_runner, client) -> dict:
    client.portal.call(_make, ADMIN_EMAIL, "admin")
    return _sign_in(client)


def _create(client, headers, label: str = "Laptop", device: str = "cuda"):
    return client.post(MEMBERS, headers=headers, json={"label": label, "device": device})


def _enroll(client, token: str, base_url: str = BASE_URL, headers: dict | None = None):
    return client.post(ENROLL, headers=headers or {},
                       json={"join_token": token, "base_url": base_url})


async def _row(member_id: str) -> dict:
    async with db.session_factory() as session:
        row = (await session.execute(
            text("SELECT * FROM fleet_members WHERE id = :id"),
            {"id": uuid.UUID(member_id)})).mappings().one()
    return dict(row)


async def _age(member_id: str) -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE fleet_members SET join_expires_at = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1),
             "id": uuid.UUID(member_id)})
        await session.commit()


async def _plant(token: str, credential: str | None = None, revoked: bool = False) -> None:
    """A row the routes would never leave behind on their own: the spend's own
    conditions are checked against it directly, one guard at a time."""
    now = datetime.now(timezone.utc)
    async with db.session_factory() as session:
        await session.execute(text(
            "INSERT INTO fleet_members (id, worker_id, label, device, join_token_hash, "
            "join_expires_at, credential_hash, base_url, enrolled_at, revoked_at) "
            "VALUES (:id, :worker_id, 'planted', 'cuda', :join, :expires, :credential, "
            ":base, :enrolled, :revoked)"),
            {"id": uuid.uuid4(), "worker_id": f"m-{uuid.uuid4().hex}",
             "join": _digest(token), "expires": now + timedelta(hours=1),
             "credential": _digest(credential) if credential else None,
             "base": BASE_URL.rstrip("/") if credential else None,
             "enrolled": now if credential else None,
             "revoked": now if revoked else None})
        await session.commit()


async def _audit_rows() -> list[dict]:
    return await audit.search()


# ----------------------------------------------------------- administration


def test_create_hands_the_join_token_over_once_and_stores_only_its_hash(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        response = _create(client, headers, label="Den desktop", device="rocm")
        assert response.status_code == 201
        body = response.json()
        assert set(body) == {"id", "label", "device", "join_token", "expires_at",
                             "worker_image"}
        assert body["label"] == "Den desktop" and body["device"] == "rocm"
        # The tag is the one this install runs (dev for a source build) plus the device.
        assert body["worker_image"] == "ghcr.io/portocolom-studio/potocolom-worker:dev-rocm"
        assert len(body["join_token"]) >= 43

        row = client.portal.call(_row, body["id"])
        assert bytes(row["join_token_hash"]) == _digest(body["join_token"])
        assert row["worker_id"] == f"m-{uuid.UUID(body['id']).hex}"
        assert row["credential_hash"] is None and row["enrolled_at"] is None
        assert row["created_by"] is not None
        assert body["join_token"] not in repr(row)
        lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc)
        assert timedelta(minutes=59) < lifetime <= timedelta(hours=1)

        listed = client.get(MEMBERS, headers=headers).json()
        assert body["join_token"] not in json.dumps(listed)


def test_create_names_the_worker_image_of_the_installed_release(accounts, monkeypatch):
    monkeypatch.setenv("POTOCOLOM_VERSION", "v9.8.7")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            headers = _admin(accounts, client)
            body = _create(client, headers, device="cuda").json()
        assert body["worker_image"] == "ghcr.io/portocolom-studio/potocolom-worker:v9.8.7-cuda"
    finally:
        monkeypatch.delenv("POTOCOLOM_VERSION")
        get_settings.cache_clear()


def test_the_list_states_follow_a_member_through_its_life(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        _create(client, headers, label="waiting")
        expired = _create(client, headers, label="expired").json()
        revoked = _create(client, headers, label="revoked").json()
        offline = _create(client, headers, label="offline").json()
        connected = _create(client, headers, label="connected").json()
        client.portal.call(_age, expired["id"])
        assert client.delete(f"{MEMBERS}/{revoked['id']}", headers=headers).status_code == 204
        client.cookies.clear()
        for member in (offline, connected):
            assert _enroll(client, member["join_token"]).status_code == 200
        headers = _sign_in(client)

        worker_of = {row["label"]: row["worker_id"]
                     for row in client.get(MEMBERS, headers=headers).json()}
        # A live socket is a registry entry, which only the listing reads here. It
        # is removed before the client shuts down, whose hook expects a real worker.
        realtime.workers[worker_of["connected"]] = object()
        try:
            listed = client.get(MEMBERS, headers=headers).json()
        finally:
            realtime.workers.pop(worker_of["connected"], None)

    states = {row["label"]: row["state"] for row in listed}
    assert states == {"waiting": "waiting", "expired": "expired", "revoked": "revoked",
                      "offline": "offline", "connected": "connected"}
    for row in listed:
        assert set(row) == {"id", "worker_id", "label", "device", "state", "created_at",
                            "enrolled_at", "last_seen_at", "join_expires_at", "paused"}


def test_delete_keeps_the_row_is_idempotent_and_404s_an_unknown_id(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        member = _create(client, headers).json()
        assert client.delete(f"{MEMBERS}/{member['id']}", headers=headers).status_code == 204
        assert client.delete(f"{MEMBERS}/{member['id']}", headers=headers).status_code == 204
        assert client.delete(f"{MEMBERS}/{uuid.uuid4()}", headers=headers).status_code == 404

        row = client.portal.call(_row, member["id"])
        assert row["revoked_at"] is not None
        assert row["join_token_hash"] is None


def test_only_an_administrator_manages_members(accounts):
    _account(accounts, "member@example.com")
    with TestClient(app) as client:
        headers = _sign_in(client, "member@example.com")
        assert _create(client, headers).status_code == 403
        assert client.get(MEMBERS, headers=headers).status_code == 403
        assert client.delete(f"{MEMBERS}/{uuid.uuid4()}", headers=headers).status_code == 403


def test_create_needs_recent_authentication(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)

        async def go_stale():
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE sessions SET recent_auth_at = :past"),
                    {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                     - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(go_stale)
        stale = _create(client, headers)
        assert stale.status_code == 403
        assert stale.json() == {"detail": "recent authentication required"}
        assert client.get(MEMBERS, headers=headers).json() == []


def test_in_none_mode_create_and_delete_refuse_a_foreign_origin_and_accept_the_allowed_one(
        none_mode):
    evil = {"Origin": "https://evil.example"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert _create(client, evil).status_code == 403
        assert client.get(MEMBERS).json() == []

        member = _create(client, {"Origin": ORIGIN})
        assert member.status_code == 201
        member_id = member.json()["id"]
        assert client.delete(f"{MEMBERS}/{member_id}", headers=evil).status_code == 403
        assert client.delete(f"{MEMBERS}/{member_id}",
                             headers={"Origin": ORIGIN}).status_code == 204
        assert [row["state"] for row in client.get(MEMBERS).json()] == ["revoked"]


def test_every_change_is_audited_and_no_event_holds_a_secret(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        created = _create(client, headers, label="Audited").json()
        revoked = _create(client, headers, label="Revoked").json()
        assert client.delete(f"{MEMBERS}/{revoked['id']}", headers=headers).status_code == 204
        # A repeated delete changes nothing, so it is not a second event.
        assert client.delete(f"{MEMBERS}/{revoked['id']}", headers=headers).status_code == 204
        client.cookies.clear()
        credential = _enroll(client, created["join_token"]).json()["credential"]
        rows = client.portal.call(_audit_rows)

    by_action = {}
    for row in rows:
        by_action.setdefault(row["action"], []).append(row["object_ids"])
    assert [created["id"]] in by_action["fleet.member_created"]
    assert by_action["fleet.member_revoked"] == [[revoked["id"]]]
    assert [created["id"], "testclient"] in by_action["fleet.member_enrolled"]
    dumped = json.dumps(rows, default=str)
    assert created["join_token"] not in dumped
    assert credential not in dumped


# ---------------------------------------------------------------- enrolment


def test_a_valid_join_token_enrolls_once_and_stores_only_the_credential_hash(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        member = _create(client, headers).json()
        client.cookies.clear()
        response = _enroll(client, member["join_token"], base_url="https://worker.example:8443/")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"worker_id", "credential"}
        assert body["worker_id"] == f"m-{uuid.UUID(member['id']).hex}"
        assert body["credential"].startswith("ptc_fleet_")
        assert len(body["credential"]) >= len("ptc_fleet_") + 43
        assert "set-cookie" not in response.headers

        row = client.portal.call(_row, member["id"])
        assert bytes(row["credential_hash"]) == _digest(body["credential"])
        assert row["join_token_hash"] is None
        assert row["base_url"] == "https://worker.example:8443"
        assert row["enrolled_at"] is not None
        assert body["credential"] not in repr(row)
        assert _enroll(client, member["join_token"]).status_code == 403


def test_every_refusal_of_a_join_token_gets_the_same_answer(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        spent = _create(client, headers).json()
        assert _enroll(client, spent["join_token"]).status_code == 200
        expired = _create(client, headers).json()
        client.portal.call(_age, expired["id"])
        revoked = _create(client, headers).json()
        assert client.delete(f"{MEMBERS}/{revoked['id']}", headers=headers).status_code == 204
        unknown = "never-issued-" + uuid.uuid4().hex

        client.cookies.clear()
        answers = [_enroll(client, token) for token in
                   (spent["join_token"], expired["join_token"], revoked["join_token"], unknown)]

    assert {response.status_code for response in answers} == {403}
    assert len({response.content for response in answers}) == 1


def test_two_parallel_enrolls_with_one_token_only_one_succeeds(accounts):
    from app import fleet_members

    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = _create(client, headers).json()["join_token"]

        async def race():
            return await asyncio.gather(
                fleet_members.enroll(token, BASE_URL),
                fleet_members.enroll(token, BASE_URL),
                return_exceptions=True,
            )

        results = client.portal.call(race)
    won = [result for result in results if isinstance(result, tuple)]
    lost = [result for result in results if isinstance(result, HTTPException)]
    assert len(won) == 1
    assert len(lost) == 1 and lost[0].status_code == 403


def test_a_row_already_enrolled_is_not_spent_again_by_a_join_hash_it_still_holds(accounts):
    with TestClient(app) as client:
        client.portal.call(_plant, "planted-join-token", "planted-credential")
        assert _enroll(client, "planted-join-token").status_code == 403


def test_a_revoked_row_is_not_spent_even_with_its_join_hash_still_set(accounts):
    with TestClient(app) as client:
        client.portal.call(_plant, "revoked-join-token", None, True)
        assert _enroll(client, "revoked-join-token").status_code == 403


@pytest.mark.parametrize("base_url", [
    "https://user:secret@worker.example",
    "https://worker.example/path",
    "https://worker.example//",
    "https://worker.example?x=1",
    "https://worker.example#fragment",
    "ftp://worker.example",
    "worker.example",
    "https://",
    "https://worker.example:99999",
    "https://" + "a" * 250 + ".example",
])
def test_a_base_url_that_is_not_a_bare_origin_is_refused_and_spends_nothing(accounts, base_url):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = _create(client, headers).json()["join_token"]
        client.cookies.clear()
        assert _enroll(client, token, base_url=base_url).status_code == 422
        assert _enroll(client, token).status_code == 200


def test_a_foreign_origin_is_refused_and_an_absent_one_is_accepted(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        first = _create(client, headers).json()["join_token"]
        second = _create(client, headers).json()["join_token"]
        client.cookies.clear()
        assert _enroll(client, first,
                       headers={"Origin": "https://evil.example"}).status_code == 403
        assert _enroll(client, first, headers={"Origin": ORIGIN}).status_code == 200
        assert _enroll(client, second).status_code == 200


def test_enrolment_is_charged_to_its_own_rate_limit_bucket(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        token = _create(client, headers).json()["join_token"]
        client.cookies.clear()
        assert _enroll(client, token).status_code == 200

        async def subjects() -> set[bytes]:
            async with db.session_factory() as session:
                rows = (await session.execute(
                    select(LoginAttempt.subject).where(LoginAttempt.scope == "address")
                )).scalars().all()
                return set(rows)

        charged = client.portal.call(subjects)
    assert _digest("fleet-enroll:testclient") in charged
    assert _digest("reset:testclient") not in charged


def test_enrolment_is_not_open_before_setup(accounts):
    assert ("POST", ENROLL) not in auth_mode.SETUP_OPEN_HTTP
    with TestClient(app) as client:
        auth_mode.load(auth_mode.UNCLAIMED)
        response = _enroll(client, "any-token")
    assert response.status_code == 503
    assert response.json() == {"detail": "setup_required"}


# ------------------------------------------------------------------- pausing


def _set_pause(client, headers, member_id: str, paused: bool):
    return client.patch(f"{MEMBERS}/{member_id}", headers=headers, json={"paused": paused})


def _listed_by_id(client, headers) -> dict:
    return {row["id"]: row for row in client.get(MEMBERS, headers=headers).json()}


def test_pause_sets_and_clears_the_flag_and_the_list_reports_it(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        member = _create(client, headers, label="Pausable").json()
        assert _listed_by_id(client, headers)[member["id"]]["paused"] is False

        assert _set_pause(client, headers, member["id"], True).status_code == 204
        assert client.portal.call(_row, member["id"])["paused"] is True
        listed = _listed_by_id(client, headers)[member["id"]]
        assert listed["paused"] is True and listed["state"] == "waiting"

        assert _set_pause(client, headers, member["id"], False).status_code == 204
        assert client.portal.call(_row, member["id"])["paused"] is False
        assert _listed_by_id(client, headers)[member["id"]]["paused"] is False


def test_pause_404s_an_unknown_id_and_409s_a_revoked_member(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        revoked = _create(client, headers).json()
        assert client.delete(f"{MEMBERS}/{revoked['id']}", headers=headers).status_code == 204

        assert _set_pause(client, headers, str(uuid.uuid4()), True).status_code == 404
        refused = _set_pause(client, headers, revoked["id"], True)
        assert refused.status_code == 409
        assert refused.json() == {"detail": "fleet member is revoked"}
        assert client.portal.call(_row, revoked["id"])["paused"] is False


def test_in_none_mode_pause_refuses_a_foreign_origin_and_accepts_the_allowed_one(none_mode):
    evil = {"Origin": "https://evil.example"}
    with TestClient(app, base_url=ORIGIN) as client:
        member = _create(client, {"Origin": ORIGIN}).json()
        assert _set_pause(client, evil, member["id"], True).status_code == 403
        assert client.portal.call(_row, member["id"])["paused"] is False
        assert _set_pause(client, {"Origin": ORIGIN}, member["id"], True).status_code == 204
        assert client.portal.call(_row, member["id"])["paused"] is True


def test_only_an_administrator_pauses_a_member(accounts):
    _account(accounts, "member@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        member = _create(client, headers).json()
        client.cookies.clear()
        user_headers = _sign_in(client, "member@example.com")
        assert _set_pause(client, user_headers, member["id"], True).status_code == 403
        assert client.portal.call(_row, member["id"])["paused"] is False


async def _events(action: str) -> list[dict]:
    return await audit.search(action=action)


def test_a_pause_writes_one_event_per_change_and_names_only_the_member(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        member = _create(client, headers).json()
        # Already unpaused: setting false again changes nothing, so it is no event.
        assert _set_pause(client, headers, member["id"], False).status_code == 204
        assert _set_pause(client, headers, member["id"], True).status_code == 204
        assert _set_pause(client, headers, member["id"], True).status_code == 204
        assert _set_pause(client, headers, member["id"], False).status_code == 204
        paused = client.portal.call(_events, "fleet.member_paused")
        resumed = client.portal.call(_events, "fleet.member_resumed")
    assert [row["object_ids"] for row in paused] == [[member["id"]]]
    assert [row["object_ids"] for row in resumed] == [[member["id"]]]
