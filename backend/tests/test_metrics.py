"""GPU metrics persistence and history API (issue #94)."""

import asyncio
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import db, gpu_samples
from app.main import app
from app.realtime import PROTOCOL_VERSION
from app.tables import (
    GpuSample,
    GpuSampleRollup,
    Job,
    UsageEvent,
    UsageEventRollup,
    User,
    WorkerIdentity,
)

FLEET_HEADERS = {"x-fleet-token": "test-fleet-token"}

MANIFEST = {
    "id": "sd-metrics",
    "name": "SD Metrics",
    "capabilities": ["text_to_image"],
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
    "min_vram_gb": 0,
}


def fleet_hello(ws, worker_id="w-metrics"):
    ws.send_json({
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": worker_id,
        "models": [MANIFEST],
        "realtime_slots": 1,
        "device": "rocm",
        "memory_mode": "model_offload",
    })
    assert ws.receive_json()["type"] == "registered"


async def _clear_gpu_metrics() -> None:
    assert db.session_factory is not None
    async with db.session_factory() as session:
        await session.execute(delete(GpuSampleRollup))
        await session.execute(delete(GpuSample))
        await session.commit()


async def _wait_for_samples(count: int = 1, timeout: float = 3.0) -> list[GpuSample]:
    assert db.session_factory is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            rows = (await session.execute(select(GpuSample))).scalars().all()
            if len(rows) >= count:
                return rows
        await asyncio.sleep(0.05)
    return []


async def _wait_for_worker(worker_id: str, timeout: float = 3.0) -> WorkerIdentity | None:
    assert db.session_factory is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            row = await session.get(WorkerIdentity, worker_id)
            if row is not None:
                return row
        await asyncio.sleep(0.05)
    return None


@pytest.mark.db
def test_registration_persists_worker_identity():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-identity")
            identity = client.portal.call(_wait_for_worker, "w-identity")
            assert identity is not None
            assert identity.device == "rocm"
            assert identity.memory_mode == "model_offload"


@pytest.mark.db
def test_maintenance_prunes_stale_worker_identities():
    now = datetime.now(timezone.utc)

    async def exercise() -> tuple[WorkerIdentity | None, WorkerIdentity | None]:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            session.add(WorkerIdentity(
                worker_id="w-retention-stale",
                last_seen=now - gpu_samples.WORKER_RETENTION - timedelta(seconds=1),
            ))
            session.add(WorkerIdentity(
                worker_id="w-retention-recent",
                last_seen=now,
            ))
            await session.commit()
        await gpu_samples.maintain_once()
        async with db.session_factory() as session:
            return (
                await session.get(WorkerIdentity, "w-retention-stale"),
                await session.get(WorkerIdentity, "w-retention-recent"),
            )

    with TestClient(app, headers=FLEET_HEADERS) as client:
        stale, recent = client.portal.call(exercise)
        assert stale is None
        assert recent is not None


@pytest.mark.db
def test_usage_event_maintenance_rolls_up_prunes_and_is_idempotent(monkeypatch):
    now = datetime(2026, 7, 28, 15, tzinfo=timezone.utc)
    cutoff = gpu_samples._usage_raw_cutoff(now)
    user_id = uuid.uuid4()
    old_ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    recent_id = uuid.uuid4()
    monkeypatch.setattr(gpu_samples, "_utcnow", lambda: now)

    async def snapshot() -> tuple[list[uuid.UUID], list[tuple]]:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            raw_ids = (
                (
                    await session.execute(
                        select(UsageEvent.id)
                        .where(UsageEvent.user_id == user_id)
                        .order_by(UsageEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            rollups = (
                (
                    await session.execute(
                        select(UsageEventRollup)
                        .where(UsageEventRollup.user_id == user_id)
                        .order_by(UsageEventRollup.category)
                    )
                )
                .scalars()
                .all()
            )
            return raw_ids, [
                (
                    row.bucket_date,
                    row.category,
                    row.event_count,
                    row.category_score_sum,
                    row.category_score_count,
                    row.gpu_ms_sum,
                    row.duration_ms_sum,
                    row.frames_sum,
                )
                for row in rollups
            ]

    async def exercise() -> tuple[list[tuple], list[tuple], tuple, tuple]:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            session.add(User(id=user_id, email=f"{user_id}@example.test"))
            await session.flush()
            old_at = cutoff - timedelta(days=2) + timedelta(hours=3)
            session.add_all(
                [
                    UsageEvent(
                        id=old_ids[0],
                        user_id=user_id,
                        kind="job",
                        action="generate",
                        model_id="sd-test",
                        tier=None,
                        category="art",
                        category_score=0.8,
                        gpu_ms=100,
                        duration_ms=None,
                        frames=1,
                        created_at=old_at,
                    ),
                    UsageEvent(
                        id=old_ids[1],
                        user_id=user_id,
                        kind="job",
                        action="generate",
                        model_id="sd-test",
                        tier=None,
                        category="art",
                        category_score=None,
                        gpu_ms=None,
                        duration_ms=2_000,
                        frames=2,
                        created_at=old_at + timedelta(hours=1),
                    ),
                    UsageEvent(
                        id=old_ids[2],
                        user_id=user_id,
                        kind="job",
                        action="generate",
                        model_id="sd-test",
                        tier=None,
                        category="design",
                        category_score=0.5,
                        gpu_ms=50,
                        duration_ms=500,
                        frames=1,
                        created_at=old_at,
                    ),
                    UsageEvent(
                        id=recent_id,
                        user_id=user_id,
                        kind="realtime",
                        action="draw",
                        model_id="sd-test",
                        tier=None,
                        category="other",
                        category_score=None,
                        gpu_ms=10,
                        duration_ms=20,
                        frames=3,
                        created_at=cutoff,
                    ),
                ]
            )
            await session.commit()

        async with db.session_factory() as session:
            await gpu_samples._rebuild_usage_rollups(session, cutoff)
            await session.commit()
        first_rebuild = (await snapshot())[1]
        async with db.session_factory() as session:
            await gpu_samples._rebuild_usage_rollups(session, cutoff)
            await session.commit()
        second_rebuild = (await snapshot())[1]

        await gpu_samples.maintain_once()
        first = await snapshot()
        await gpu_samples.maintain_once()
        second = await snapshot()
        async with db.session_factory() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        return first_rebuild, second_rebuild, first, second

    with TestClient(app, headers=FLEET_HEADERS) as client:
        first_rebuild, second_rebuild, first, second = client.portal.call(exercise)

    assert first_rebuild == second_rebuild
    assert first == second
    raw_ids, rollups = first
    assert raw_ids == [recent_id]
    assert rollups == [
        (date(2026, 4, 27), "art", 2, 0.8, 1, 100, 2_000, 3),
        (date(2026, 4, 27), "design", 1, 0.5, 1, 50, 500, 1),
    ]


@pytest.mark.db
def test_a_blank_tier_and_no_tier_roll_up_as_one_row():
    """The unique index arbitrates on COALESCE(tier, ''), so a blank tier and
    an absent one are one rollup row to it. Grouped apart they become two rows
    offered to that one row, and PostgreSQL refuses the whole statement rather
    than half of it, which takes every other account's rollup down with it and
    the pruning that shares the transaction (issue #408).
    """
    now = datetime(2026, 7, 28, 15, tzinfo=timezone.utc)
    cutoff = gpu_samples._usage_raw_cutoff(now)
    user_id = uuid.uuid4()
    old_at = cutoff - timedelta(days=2) + timedelta(hours=3)

    def event(tier: str | None) -> UsageEvent:
        return UsageEvent(
            id=uuid.uuid4(), user_id=user_id, kind="job", action="generate",
            model_id="sd-test", tier=tier, category="art", category_score=None,
            gpu_ms=10, duration_ms=None, frames=1, created_at=old_at,
        )

    async def exercise() -> list[tuple]:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            session.add(User(id=user_id, email=f"{user_id}@example.test"))
            await session.flush()
            session.add_all([event(None), event("")])
            await session.commit()
        async with db.session_factory() as session:
            await gpu_samples._rebuild_usage_rollups(session, cutoff)
            await session.commit()
        async with db.session_factory() as session:
            rows = (await session.execute(
                select(UsageEventRollup).where(UsageEventRollup.user_id == user_id)
            )).scalars().all()
            counted = [(row.tier, row.event_count) for row in rows]
        async with db.session_factory() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        return counted

    with TestClient(app, headers=FLEET_HEADERS) as client:
        rolled = client.portal.call(exercise)

    # One row holding both, rather than a statement that wrote nothing at all.
    assert rolled == [(None, 2)]


@pytest.mark.db
def test_usage_event_rollups_are_hard_deleted_with_user():
    user_id = uuid.uuid4()
    rollup_id = uuid.uuid4()

    async def exercise() -> UsageEventRollup | None:
        assert db.session_factory is not None
        async with db.session_factory() as session:
            session.add(User(id=user_id, email=f"{user_id}@example.test"))
            await session.flush()
            session.add(
                UsageEventRollup(
                    id=rollup_id,
                    user_id=user_id,
                    bucket_date=date(2026, 1, 1),
                    kind="job",
                    action="generate",
                    model_id="sd-test",
                    tier=None,
                    category="art",
                    event_count=1,
                    category_score_sum=0.8,
                    category_score_count=1,
                    gpu_ms_sum=10,
                    duration_ms_sum=20,
                    frames_sum=1,
                )
            )
            await session.commit()
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        async with db.session_factory() as session:
            return await session.get(UsageEventRollup, rollup_id)

    with TestClient(app, headers=FLEET_HEADERS) as client:
        assert client.portal.call(exercise) is None


@pytest.mark.db
def test_heartbeat_persists_gpu_sample():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker)
            worker.send_json({
                "type": "heartbeat",
                "slots_in_use": 0,
                "loaded_models": ["sd-metrics"],
                "gpu": {
                    "device": "rocm",
                    "available": True,
                    "util_pct": 42,
                    "vram_used_bytes": 4_000_000_000,
                    "vram_total_bytes": 8_000_000_000,
                    "temperature_c": 61.0,
                    "power_w": 120.0,
                },
            })
            rows = client.portal.call(_wait_for_samples)
            assert rows, "heartbeat sample was not persisted"
            row = rows[0]
            assert row.worker_id == "w-metrics"
            assert row.util_pct == 42
            # These are BigInteger; asserting only util_pct is what let an
            # int4 bound blank every real card's VRAM series unnoticed.
            assert row.vram_used_bytes == 4_000_000_000
            assert row.vram_total_bytes == 8_000_000_000
            assert row.loaded_models == ["sd-metrics"]

            async def read_worker() -> WorkerIdentity | None:
                assert db.session_factory is not None
                async with db.session_factory() as session:
                    return await session.get(WorkerIdentity, "w-metrics")

            identity = client.portal.call(read_worker)
            assert identity is not None
            assert identity.device == "rocm"
            assert identity.memory_mode == "model_offload"
            assert identity.last_seen >= row.sampled_at


@pytest.mark.db
def test_gpu_history_round_trip():
    now = datetime.now(timezone.utc)
    sample = GpuSample(
        worker_id="w-history",
        sampled_at=now - timedelta(minutes=10),
        util_pct=55,
        vram_used_bytes=3_000_000_000,
        vram_total_bytes=6_000_000_000,
        temperature_c=58.0,
        power_w=95.0,
        loaded_models=["sd-metrics"],
    )

    with TestClient(app, headers=FLEET_HEADERS) as client:
        assert db.session_factory is not None
        client.portal.call(_clear_gpu_metrics)

        async def insert():
            async with db.session_factory() as session:
                session.add(sample)
                await session.commit()

        client.portal.call(insert)

        response = client.get(
            "/api/v1/metrics/gpu/history",
            params={
                "from": int((now - timedelta(hours=1)).timestamp() * 1000),
                "to": int(now.timestamp() * 1000),
                "rollup": "raw",
                "worker_id": "w-history",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["rollup"] == "raw"
        assert len(body["samples"]) == 1
        point = body["samples"][0]
        assert point["util_pct"] == 55
        assert point["vram_used_pct"] == 50


@pytest.mark.db
def test_job_dispatch_and_finish_timestamps():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        with client.websocket_connect("/api/v1/fleet") as worker:
            fleet_hello(worker, "w-ts")

            created = client.post(
                "/api/v1/generations",
                json={"model_id": "sd-metrics", "params": {"prompt": "metrics"}},
            )
            assert created.status_code == 202
            job_id = uuid.UUID(created.json()["job_id"])

            dispatch = worker.receive_json()
            assert dispatch["type"] == "dispatch_job"

            assert db.session_factory is not None

            async def read_job():
                async with db.session_factory() as session:
                    return await session.get(Job, job_id)

            # Dispatch sends the websocket message before committing
            # dispatched_at (jobs.dispatch); wait for the row to catch up.
            job = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                job = client.portal.call(read_job)
                assert job is not None
                if job.dispatched_at is not None and job.state == "running":
                    break
                time.sleep(0.05)
            assert job is not None
            assert job.dispatched_at is not None
            assert job.state == "running"

            worker.send_json({
                "type": "job_done",
                "job_id": str(job_id),
                "dispatch_token": dispatch["dispatch_token"],
                "gpu_ms": 900,
                "width": 512,
                "height": 512,
            })

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                job = client.portal.call(read_job)
                if job.state in ("failed", "succeeded"):
                    break
                time.sleep(0.05)
            assert job.state == "failed"
            assert job.finished_at is not None


def test_non_finite_telemetry_is_dropped():
    # NaN and infinities round-trip through json.loads and PostgreSQL, but
    # Starlette refuses to serialize them: one sample would 500 every history
    # range that contains it (issue #203).
    from app.gpu_samples import _float_or_none

    assert _float_or_none(42) == 42.0
    assert _float_or_none(float("nan")) is None
    assert _float_or_none(float("inf")) is None
    assert _float_or_none(float("-inf")) is None


def test_non_finite_telemetry_is_dropped_by_both_coercions():
    # _float_or_none was guarded first; _int_or_none four lines above it was
    # not, and it feeds util_pct and the VRAM fields from the same heartbeat.
    # The GpuSample is built outside record_heartbeat's try, so a raise there
    # escapes into an untracked task (issue #203).
    from app.gpu_samples import _float_or_none, _int_or_none

    for coerce in (_float_or_none, _int_or_none):
        assert coerce(42) is not None
        assert coerce(float("nan")) is None
        assert coerce(float("inf")) is None
        assert coerce(float("-inf")) is None
    # The bound is per column, not one blanket width: util_pct is SmallInteger
    # and the VRAM counters are BigInteger, so any card above 2 GiB would be
    # silently blanked by an int4 bound.
    assert _int_or_none(8 * 1024**3, bits=63) == 8 * 1024**3
    assert _int_or_none(100_000, bits=15) is None
    assert _int_or_none(42, bits=15) == 42


def test_gpu_history_rejects_unusable_timestamps():
    # A superscript two passes isdigit() but int() rejects it; a far-future
    # epoch overflows
    # the year, and a 25-digit one overflows the float division. All three were
    # 500s from an ordinary authenticated GET (issue #232).
    with TestClient(app, headers=FLEET_HEADERS) as client:
        good = "1700000000000"
        # The last two are the ISO branch: astimezone overflows at the edges
        # of the representable range, one line below the digit branch.
        for value in ("99999999999999999", "9" * 25, "\u00b2", "not-a-date",
                      "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            response = client.get("/api/v1/metrics/gpu/history",
                                  params={"from": value, "to": good})
            assert response.status_code == 422, f"{value!r} gave {response.status_code}"
        ok = client.get("/api/v1/metrics/gpu/history",
                        params={"from": good, "to": "1700000600000"})
        assert ok.status_code == 200
        iso = client.get("/api/v1/metrics/gpu/history",
                         params={"from": "2020-01-01T00:00:00Z",
                                 "to": "2020-01-02T00:00:00Z"})
        assert iso.status_code == 200


def test_vram_percentage_cannot_overflow_the_rollup_column():
    # The rollup columns are SmallInteger, and maintain_once is one
    # transaction: an overflow there stops rollups, pruning and worker cleanup
    # until the sample ages out 48 hours later (issue #232).
    from app.gpu_samples import _vram_used_pct

    assert _vram_used_pct(4, 8) == 50
    # An impossible ratio is dropped, not clamped, in both directions. A
    # clamped 32767 would skew the rollup mean for the 30 days a bucket is
    # retained, where a null is filtered out of it.
    assert _vram_used_pct(2**62, 1) is None
    assert _vram_used_pct(-(2**62), 1) is None
    assert _vram_used_pct(None, 8) is None
    assert _vram_used_pct(4, 0) is None


def test_usage_event_floats_are_bounded_like_the_ints():
    # _numeric bounded its int branch but not its float branch, so 1e30
    # reached an int4 column and the whole usage event was dropped.
    from app.usage_events import _optional_float, _optional_int

    assert _optional_int(1500.0) == 1500
    assert _optional_int(1e30) is None
    assert _optional_float(1e30) is None
    assert _optional_float(float("nan")) is None


def test_benchmark_input_bounds_its_int4_columns():
    # Tested at the model rather than the route because the route is gated
    # behind BENCHMARK_API; the model is the validation boundary either way.
    # Every int below lands in an int4 column (issue #232).
    import pydantic

    from app.benchmark_sessions import BenchmarkInput, MeasurementInput

    base = {"created_at": "2026-01-01T00:00:00Z", "models": ["m"], "results": [],
            "prompt_count": 1, "variants_per_prompt": 1, "total_jobs": 1,
            "succeeded": 1, "failed": 0}
    assert BenchmarkInput.model_validate(base).prompt_count == 1
    for field in ("prompt_count", "variants_per_prompt", "total_jobs", "succeeded", "failed"):
        try:
            BenchmarkInput.model_validate({**base, field: 3_000_000_000})
        except pydantic.ValidationError:
            continue
        raise AssertionError(f"{field} accepted a value past int4")

    measurement = {"prompt_id": 1, "title": "t", "category": "c", "model_id": "m",
                   "variant": "v", "cell_key": "k", "state": "succeeded"}
    assert MeasurementInput.model_validate(measurement).prompt_id == 1
    for field in ("prompt_id", "model_load_ms", "gpu_ms", "width", "height"):
        try:
            MeasurementInput.model_validate({**measurement, field: 3_000_000_000})
        except pydantic.ValidationError:
            continue
        raise AssertionError(f"{field} accepted a value past int4")
    # The params dict reaches JSONB unvalidated otherwise.
    try:
        MeasurementInput.model_validate({**measurement, "params": {"x": float("inf")}})
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("a non-finite benchmark param was accepted")
