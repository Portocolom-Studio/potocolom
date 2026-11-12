import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from conftest import run_on_test_loop
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import db, realtime, telemetry
from app.main import app
from app.manifests import Manifest
from app.tables import Job, Model, TelemetryState, UsageEvent, User, WorkerIdentity


def test_usage_event_schedulers_hold_their_tasks_until_done(monkeypatch):
    # asyncio.create_task keeps only a weak reference, so a task the scheduler
    # does not hold can be garbage-collected mid-write (issue #497).
    from app import usage_events

    gate = asyncio.Event()

    async def blocked(*args, **kwargs):
        await gate.wait()

    monkeypatch.setattr(usage_events, "record_job", blocked)
    monkeypatch.setattr(usage_events, "record_realtime", blocked)

    async def exercise() -> None:
        usage_events.schedule_job(uuid.uuid4(), {"category": "art"})
        usage_events.schedule_realtime(uuid.uuid4(), "sd-test", {"gpu_ms": 1})
        assert len(usage_events._background_tasks) == 2
        gate.set()
        deadline = time.monotonic() + 5
        while usage_events._background_tasks:
            if time.monotonic() >= deadline:
                raise AssertionError("a scheduled task was never released")
            await asyncio.sleep(0.01)

    run_on_test_loop(exercise())


@pytest.mark.db
def test_telemetry_preview_is_exact_daily_aggregate():
    async def seed() -> None:
        assert db.session_factory is not None
        assert db.local_user_id is not None
        reported_day = datetime.now(timezone.utc).date() - timedelta(days=1)
        start = datetime.combine(reported_day, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        async with db.session_factory() as session:
            # This test asserts the exact reported period, so isolate that shared state.
            await session.execute(delete(UsageEvent).where(
                UsageEvent.created_at >= start,
                UsageEvent.created_at < end,
            ))
            await session.execute(delete(WorkerIdentity))
            session.add(UsageEvent(
                user_id=db.local_user_id,
                kind="realtime",
                action="draw",
                model_id="sd-test",
                tier=None,
                category="other",
                category_score=None,
                gpu_ms=100,
                duration_ms=120_000,
                frames=3,
                created_at=start + timedelta(hours=12),
            ))
            session.add(WorkerIdentity(
                worker_id="w-preview",
                device="rocm",
                memory_mode="model_offload",
                last_seen=start + timedelta(hours=12),
            ))
            session.add(WorkerIdentity(
                worker_id="w-stale",
                device="cuda",
                memory_mode="full",
                last_seen=start - timedelta(seconds=1),
            ))
            await session.commit()

    with TestClient(app) as client:
        client.portal.call(seed)
        response = client.get("/api/v1/telemetry/preview")
        assert response.status_code == 200
        payload = response.json()
        assert payload["active_users"] == 1
        assert payload["events"] == {"realtime": 1}
        assert payload["by_action"] == {"draw": 1}
        assert payload["by_category"] == {"other": 1}
        assert payload["by_tier"] == {}
        assert payload["realtime_minutes"] == 2
        assert payload["workers"] == [{"device": "rocm", "memory_mode": "model_offload"}]


@pytest.mark.db
def test_failed_telemetry_send_is_marked_and_dropped(monkeypatch):
    def fail(_url: str, _payload: dict) -> None:
        raise OSError("offline")

    monkeypatch.setattr(telemetry, "_post", fail)
    monkeypatch.setattr(
        telemetry, "get_settings", lambda: SimpleNamespace(telemetry=True))

    async def exercise() -> None:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            state = await session.get(TelemetryState, 1)
            assert state is not None
            state.last_report_day = None
            await session.commit()
        await telemetry.send_once()
        async with db.session_factory() as session:
            state = await session.get(TelemetryState, 1)
            assert state is not None
            expected = datetime.now(timezone.utc).date() - timedelta(days=1)
            assert state.last_report_day == expected

    with TestClient(app) as client:
        client.portal.call(exercise)


@pytest.mark.db
def test_usage_events_are_hard_deleted_with_user():
    async def exercise() -> None:
        assert db.session_factory is not None
        user_id = uuid.uuid4()
        event_id = uuid.uuid4()
        async with db.session_factory() as session:
            session.add(User(id=user_id, email=f"{user_id}@example.test"))
            # Flush the owner first: there is no ORM relationship between these
            # tables, so the unit of work has no dependency to sort inserts by.
            await session.flush()
            session.add(UsageEvent(
                id=event_id,
                user_id=user_id,
                kind="job",
                action="generate",
                model_id="sd-test",
                tier=None,
                category="other",
                category_score=None,
                gpu_ms=1,
                duration_ms=2,
                frames=1,
            ))
            await session.commit()
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        async with db.session_factory() as session:
            assert await session.get(UsageEvent, event_id) is None

    with TestClient(app) as client:
        client.portal.call(exercise)


async def _record_job_usage_tier(model_id: str) -> str | None:
    # Seeds one job for model_id, runs the completion writer, and returns the
    # tier stored on the usage row it wrote.
    from app import usage_events

    assert db.session_factory is not None
    async with db.session_factory() as session:
        user = User(email=f"{uuid.uuid4()}@example.test")
        session.add(Model(id=model_id, name=model_id, capabilities=["text_to_image"],
                          parameters_schema={}, min_vram_gb=0))
        session.add(user)
        # No ORM relationship links these tables, so flush the owners before the job.
        await session.flush()
        job = Job(id=uuid.uuid4(), user_id=user.id, model_id=model_id, params={})
        session.add(job)
        await session.commit()
        job_id = job.id
    await usage_events.record_job(job_id, {"category": "art"})
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(UsageEvent).where(UsageEvent.model_id == model_id)
        )).scalars().all()
    # record_job logs and swallows write errors, so a missing row must fail here.
    assert len(rows) == 1, "record_job wrote no usage event"
    return rows[0].tier


@pytest.mark.db
@pytest.mark.parametrize("tier", ["draft", "premium"])
def test_job_usage_event_stores_the_manifest_tier(tier):
    # The usage table is grouped by tier, so a job's row must carry the tier of
    # the manifest that ran it, not a placeholder (issue #862).
    model_id = f"tier-{tier}-{uuid.uuid4().hex[:8]}"
    worker = realtime.Worker(
        id=f"w-{model_id}", ws=None, realtime_slots=0,
        manifests=[Manifest(id=model_id, name=model_id,
                            capabilities=["text_to_image"], tier=tier)],
    )
    with TestClient(app) as client:
        realtime.workers[worker.id] = worker
        try:
            stored = client.portal.call(_record_job_usage_tier, model_id)
        finally:
            realtime.workers.pop(worker.id, None)
    assert stored == tier


@pytest.mark.db
def test_job_usage_event_stores_null_tier_for_a_model_without_a_manifest():
    # A model whose worker is offline has no manifest in the registry, so there
    # is no tier to store.
    model_id = f"tier-unknown-{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        stored = client.portal.call(_record_job_usage_tier, model_id)
    assert stored is None
