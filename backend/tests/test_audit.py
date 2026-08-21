import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import audit, db
from app.main import app
from app.tables import AuditEvent, User


@pytest.fixture
def connected(portal_runner):
    assert portal_runner(db.connect()) is True
    audit._spool.clear()
    audit._dropped = 0
    audit._fell_back = 0

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM audit_events"))
            await session.execute(text("DELETE FROM users WHERE email <> :local"),
                                  {"local": db.LOCAL_USER_EMAIL})
            await session.commit()

    portal_runner(clear())
    try:
        yield portal_runner
    finally:
        # A TestClient inside a test disposes the engine when its lifespan
        # ends, so the teardown reconnects before it can clean up.
        if db.session_factory is None:
            portal_runner(db.connect())
        portal_runner(clear())
        audit._spool.clear()
        audit._dropped = 0
        audit._fell_back = 0
        portal_runner(db.dispose())


async def _fetch_events() -> list[AuditEvent]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuditEvent).order_by(AuditEvent.occurred_at, AuditEvent.action)
        )).scalars().all())


def _stored(portal_runner) -> list[AuditEvent]:
    return portal_runner(_fetch_events())


def _actor(portal_runner, email="admin@example.com") -> User:
    async def go():
        async with db.session_factory() as session:
            row = User(id=uuid.uuid4(), email=email, role="admin")
            session.add(row)
            await session.commit()
            return row

    return portal_runner(go())


@pytest.mark.db
def test_a_privileged_action_is_recorded(connected):
    actor = _actor(connected)
    connected(audit.record("POST /api/v1/benchmark/gpu/load", actor=actor))
    stored = _stored(connected)
    assert [event.action for event in stored] == ["POST /api/v1/benchmark/gpu/load"]
    assert stored[0].actor_user_id == actor.id
    assert stored[0].actor_role == "admin"
    assert stored[0].severity == "info"


@pytest.mark.db
def test_every_admin_route_is_audited_without_naming_itself(connected):
    """The record happens inside the admin role check, so a route added later
    cannot forget to audit and no route can be audited under a stale name."""
    with TestClient(app) as client:
        assert client.get("/api/v1/telemetry/preview").status_code == 200
        # The client's lifespan owns the engine while it is live, so reads go
        # through its portal rather than the test loop.
        assert [event.action for event in client.portal.call(_fetch_events)] == [
            "GET /api/v1/telemetry/preview"
        ]


@pytest.mark.db
def test_the_attempt_is_recorded_even_when_the_action_then_fails(connected):
    """Durable before the action, so an attempt that fails is still on record."""
    with TestClient(app) as client:
        assert client.get("/api/v1/studio/gpu").status_code == 503
        assert [event.action for event in client.portal.call(_fetch_events)] == [
            "GET /api/v1/studio/gpu"
        ]


@pytest.mark.db
def test_a_privileged_action_proceeds_when_only_its_audit_fails(connected, monkeypatch):
    """Audit fails open: losing the record must not deny the action."""
    async def refuse(_events):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(audit, "_insert", refuse)
    with TestClient(app) as client:
        assert client.get("/api/v1/telemetry/preview").status_code == 200
        assert client.portal.call(_fetch_events) == []
    assert len(audit._spool) == 1


@pytest.mark.db
def test_a_spooled_event_is_flushed_by_the_next_successful_insert(connected, monkeypatch):
    actor = _actor(connected)
    calls = {"n": 0}
    real = audit._insert

    async def fail_once(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("audit table unavailable")
        return await real(events)

    monkeypatch.setattr(audit, "_insert", fail_once)
    connected(audit.record("first", actor=actor))
    assert _stored(connected) == []
    connected(audit.record("second", actor=actor))
    # The flush carries the gap marker for the insert that failed.
    assert [event.action for event in _stored(connected)] == [
        "first", "second", audit.FALLBACK_ACTION
    ]
    assert not audit._spool


@pytest.mark.db
def test_the_spool_drops_the_oldest_and_counts_it(connected, monkeypatch):
    """Bounded memory: an outage must not let the spool grow without limit."""
    async def refuse(_events):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(audit, "_insert", refuse)
    for index in range(audit.SPOOL_LIMIT + 5):
        connected(audit.record(f"action-{index}"))
    assert len(audit._spool) == audit.SPOOL_LIMIT
    assert audit._dropped == 5
    assert audit._spool[0].action == "action-5"


@pytest.mark.db
def test_overflow_and_fallback_surface_as_distinct_high_severity_events(connected, monkeypatch):
    calls = {"n": 0}
    real = audit._insert

    async def fail_then_work(events):
        calls["n"] += 1
        if calls["n"] <= audit.SPOOL_LIMIT + 2:
            raise RuntimeError("audit table unavailable")
        return await real(events)

    monkeypatch.setattr(audit, "_insert", fail_then_work)
    for index in range(audit.SPOOL_LIMIT + 2):
        connected(audit.record(f"action-{index}"))
    connected(audit.record("after-the-outage"))

    stored = {event.action: event for event in _stored(connected)}
    assert audit.OVERFLOW_ACTION in stored
    assert audit.FALLBACK_ACTION in stored
    assert stored[audit.OVERFLOW_ACTION].severity == "high"
    assert stored[audit.FALLBACK_ACTION].severity == "high"
    assert stored[audit.OVERFLOW_ACTION].object_count == 2
    assert stored[audit.FALLBACK_ACTION].object_count == audit.SPOOL_LIMIT + 2
    assert audit._dropped == 0 and audit._fell_back == 0


@pytest.mark.db
def test_a_lost_record_is_logged_as_a_structured_line(connected, monkeypatch, caplog):
    """A record that never reaches PostgreSQL must still leave a trace."""
    async def refuse(_events):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(audit, "_insert", refuse)
    with caplog.at_level("WARNING", logger="potocolom.audit"):
        connected(audit.record("POST /api/v1/benchmark/gpu/load"))
    line = json.loads(caplog.records[-1].getMessage())
    assert line["audit"] == "fallback"
    assert line["action"] == "POST /api/v1/benchmark/gpu/load"


@pytest.mark.db
def test_the_seven_day_summary_marks_a_gap_for_either_event(connected):
    actor = _actor(connected)
    connected(audit.record("GET /api/v1/studio/gpu", actor=actor))
    assert connected(audit.summary())["gaps"] == []
    connected(audit.record(audit.OVERFLOW_ACTION, severity="high", object_count=3))
    summary = connected(audit.summary())
    assert summary["gaps"] == [{"action": audit.OVERFLOW_ACTION, "events": 3}]
    assert summary["actions"]["GET /api/v1/studio/gpu"] == 1


@pytest.mark.db
def test_the_summary_window_ends_at_seven_days(connected):
    old = datetime.now(timezone.utc) - timedelta(days=8)
    connected(audit.record("old-action", occurred_at=old))
    connected(audit.record("recent-action"))
    assert set(connected(audit.summary())["actions"]) == {"recent-action"}


@pytest.mark.db
def test_object_ids_are_capped_and_the_truncation_is_visible(connected):
    ids = [str(uuid.uuid4()) for _ in range(audit.OBJECT_ID_CAP + 40)]
    connected(audit.record("bulk", object_ids=ids))
    stored = _stored(connected)[0]
    assert len(stored.object_ids) == audit.OBJECT_ID_CAP
    assert stored.object_count == audit.OBJECT_ID_CAP + 40
    assert stored.truncated is True


@pytest.mark.db
def test_a_short_object_list_is_not_flagged_as_truncated(connected):
    connected(audit.record("bulk", object_ids=["a", "b"]))
    stored = _stored(connected)[0]
    assert stored.object_count == 2 and stored.truncated is False


@pytest.mark.db
def test_exporting_the_audit_is_itself_audited(connected):
    actor = _actor(connected)
    connected(audit.record("GET /api/v1/studio/gpu", actor=actor))
    exported = json.loads(connected(audit.export(actor=actor)))
    assert [row["action"] for row in exported] == ["GET /api/v1/studio/gpu"]
    assert audit.EXPORT_ACTION in {event.action for event in _stored(connected)}


@pytest.mark.db
def test_search_filters_by_actor_action_and_time(connected):
    actor = _actor(connected)
    other = _actor(connected, "other@example.com")
    connected(audit.record("wanted", actor=actor))
    connected(audit.record("wanted", actor=other))
    connected(audit.record("unwanted", actor=actor))
    assert len(connected(audit.search(actor_user_id=actor.id))) == 2
    assert len(connected(audit.search(action="wanted"))) == 2
    assert len(connected(audit.search(actor_user_id=actor.id, action="wanted"))) == 1
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert connected(audit.search(since=future)) == []


@pytest.mark.db
def test_retention_prunes_events_past_ninety_days(connected):
    stale = datetime.now(timezone.utc) - audit.RETENTION - timedelta(days=1)
    connected(audit.record("stale", occurred_at=stale))
    connected(audit.record("fresh"))
    connected(audit.prune())
    assert [event.action for event in _stored(connected)] == ["fresh"]


@pytest.mark.db
def test_the_record_outlives_the_account_that_made_it(connected):
    """An administrator deleting themselves must not erase what they did."""
    actor = _actor(connected)
    connected(audit.record("GET /api/v1/studio/gpu", actor=actor))

    async def remove() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": actor.id})
            await session.commit()

    connected(remove())
    stored = _stored(connected)
    assert [event.action for event in stored] == ["GET /api/v1/studio/gpu"]
    assert stored[0].actor_user_id is None
    assert stored[0].actor_role == "admin"
