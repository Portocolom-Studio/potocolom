import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import db, gpu_samples, telemetry
from app.main import app
from app.tables import TelemetryState, UsageEvent, User, WorkerIdentity


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
        await gpu_samples.maintain_once()

    with TestClient(app) as client:
        asyncio.run(seed())
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

    with TestClient(app):
        asyncio.run(exercise())


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

    with TestClient(app):
        asyncio.run(exercise())
