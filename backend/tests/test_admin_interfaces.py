"""What an administrator can see, and what seeing it leaves behind.

An administrator reads any one account completely and mutates none of them.
There is no view that crosses accounts: the way in is always a named user, and
naming one is itself recorded.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db
from app.main import app
from app.tables import AuditEvent
from tests.test_account_states import _admin, _set_state
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]


@pytest.fixture
def library(accounts):
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


async def _events(action: str | None = None) -> list[AuditEvent]:
    async with db.session_factory() as session:
        query = select(AuditEvent).order_by(AuditEvent.occurred_at)
        if action is not None:
            query = query.where(AuditEvent.action == action)
        return list((await session.execute(query)).scalars().all())


def _wait_for_audit(client, action, count=1, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = client.portal.call(_events, action)
        if len(found) >= count:
            return found
        time.sleep(0.05)
    raise AssertionError(f"no {action} audit event arrived")


@pytest.mark.db
def test_an_administrator_reads_one_account_completely(library):
    from tests.test_account_deletion import _owned_work

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "watched@example.com")
        client.portal.call(_owned_work, subject.id)
        _admin(client)

        page = client.get(f"/api/v1/users/{subject.id}")
        assert page.status_code == 200
        body = page.json()
        assert body["email"] == "watched@example.com"
        assert body["state"] == "active"
        assert body["role"] == "user"

        work = client.get(f"/api/v1/users/{subject.id}/generations")
        assert work.status_code == 200
        assert len(work.json()) == 1
        assert work.json()[0]["params"] == {"prompt": "a boat"}


@pytest.mark.db
def test_reading_another_account_records_who_was_read(library):
    """A privileged read carries a target, which the role check cannot know,
    so the route records it itself."""
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "recorded@example.com")
        _admin(client)
        assert client.get(f"/api/v1/users/{subject.id}").status_code == 200
        recorded = _wait_for_audit(client, "user.read")
    assert recorded[0].target_user_id == subject.id


@pytest.mark.db
def test_an_administrator_cannot_change_another_accounts_work(library):
    """Administrators read and administer. Nothing here lets one star, cancel
    or delete somebody else's generation through an admin route."""
    from tests.test_account_deletion import _owned_work

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "untouched@example.com")
        asset = client.portal.call(_owned_work, subject.id)
        _admin(client)
        assert client.post(f"/api/v1/generations/{asset.job_id}/star",
                           headers=_csrf(client)).status_code == 404


@pytest.mark.db
def test_there_is_no_way_to_list_everybodys_work_at_once(library):
    """No global gallery and no cross-user search: the way in is a named
    account, one at a time, and each look is recorded."""
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert client.get("/api/v1/generations?user_id=all").json() == []
        assert client.get("/api/v1/users/generations").status_code in (404, 422)


@pytest.mark.db
def test_the_seven_day_summary_counts_what_administrators_did(library):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "counted@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        _wait_for_audit(client, "account.state")

        summary = client.get("/api/v1/audit/summary")
        assert summary.status_code == 200
        body = summary.json()
    assert body["actions"].get("account.state") == 1
    assert body["gaps"] == []


@pytest.mark.db
def test_the_audit_can_be_searched_by_who_and_by_what(library):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "searched@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        _wait_for_audit(client, "account.state")

        by_action = client.get("/api/v1/audit", params={"action": "account.state"})
        assert by_action.status_code == 200
        assert [row["action"] for row in by_action.json()] == ["account.state"]
        assert by_action.json()[0]["target_user_id"] == str(subject.id)

        by_target = client.get("/api/v1/audit", params={"target_user_id": str(subject.id)})
        assert {row["action"] for row in by_target.json()} >= {"account.state"}

        nobody = client.get("/api/v1/audit", params={"target_user_id": str(uuid.uuid4())})
        assert nobody.json() == []


@pytest.mark.db
def test_exporting_the_audit_is_itself_audited(library):
    """Reading the whole record is a privileged act, and the record says who
    did it and which events they took."""
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "exported@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        _wait_for_audit(client, "account.state")

        exported = client.get("/api/v1/audit/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/json")
        body = exported.json()
        assert "events" in body and "truncated" in body
        recorded = _wait_for_audit(client, "audit.export")
    assert recorded[0].object_count == len(body["events"])


@pytest.mark.db
def test_an_export_carries_at_most_a_hundred_object_ids_and_says_so(library):
    """The cap is what keeps one action from writing an unbounded row, and
    the flag is what stops a reader believing the short list is everything."""
    from app import audit

    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)

        async def record_many() -> None:
            await audit.record("test.many", object_ids=[str(uuid.uuid4()) for _ in range(150)],
                               object_count=150)

        client.portal.call(record_many)
        recorded = _wait_for_audit(client, "test.many")
    assert len(recorded[0].object_ids) == audit.OBJECT_ID_CAP
    assert recorded[0].object_count == 150
    assert recorded[0].truncated is True


@pytest.mark.db
def test_reading_many_different_accounts_quickly_raises_a_flag(library):
    """One administrator opening account after account is what a stolen
    administrator session looks like, and the panel exists to show it."""
    from app import admin

    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        subjects = [client.portal.call(_make, f"crowd{n}@example.com")
                    for n in range(admin.ANOMALY_TARGETS + 1)]
        for subject in subjects:
            assert client.get(f"/api/v1/users/{subject.id}").status_code == 200
        flagged = _wait_for_audit(client, "admin.anomaly")

        panel = client.get("/api/v1/audit/anomalies")
        assert panel.status_code == 200
        assert panel.json()[0]["distinct_targets"] >= admin.ANOMALY_TARGETS
    assert flagged[0].severity == "high"


@pytest.mark.db
def test_the_audit_belongs_to_administrators_alone(library):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "nosy@example.com")
        assert _login(client, "nosy@example.com").status_code == 204
        for path in ("/api/v1/audit", "/api/v1/audit/summary", "/api/v1/audit/export",
                     "/api/v1/audit/anomalies", f"/api/v1/users/{subject.id}",
                     f"/api/v1/users/{subject.id}/generations"):
            assert client.get(path).status_code == 403, path


@pytest.mark.db
def test_an_account_that_does_not_exist_reads_as_one_that_does_not(library):
    with TestClient(app, base_url=ORIGIN) as client:
        _admin(client)
        assert client.get(f"/api/v1/users/{uuid.uuid4()}").status_code == 404


@pytest.mark.db
def test_the_administrator_list_shows_who_is_here_and_what_state_they_are_in(library):
    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "listed@example.com")
        _admin(client)
        assert _set_state(client, subject.id, "suspended").status_code == 204
        listed = client.get("/api/v1/users")
        assert listed.status_code == 200
        rows = {row["email"]: row for row in listed.json()}
    assert rows["listed@example.com"]["state"] == "suspended"
    assert "password_hash" not in str(rows)
