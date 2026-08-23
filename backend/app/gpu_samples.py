"""Persist worker GPU heartbeats and serve history (issue #94, docs/metrics.md)."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import Date, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, db, estimates, oauth
from app.tables import (
    GpuSample,
    GpuSampleRollup,
    UsageEvent,
    UsageEventRollup,
    WorkerIdentity,
)

logger = logging.getLogger("potocolom.gpu_samples")

RAW_RETENTION = timedelta(hours=48)
ROLLUP_RETENTION = timedelta(days=30)
WORKER_RETENTION = timedelta(days=30)
USAGE_RAW_RETENTION = timedelta(days=90)
ROLLUP_BUCKET = timedelta(minutes=5)
MAINTAIN_INTERVAL = 300.0  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _vram_used_pct(used: int | None, total: int | None) -> int | None:
    if used is None or total is None or total <= 0:
        return None
    # The rollup columns are SmallInteger, and maintain_once is one
    # transaction: an overflow there stops rollups, pruning and worker cleanup
    # until the sample ages out. Drop an impossible ratio rather than clamp it,
    # matching _int_or_none and keeping the retained rollup means honest; a
    # clamped 32767 would skew a 30-day bucket that a null is filtered out of.
    return _int_or_none(round(used * 100 / total), bits=15)


def _parse_gpu(gpu: Any) -> dict[str, Any]:
    return gpu if isinstance(gpu, dict) else {}


def _int_or_none(value: Any, bits: int = 31) -> int | None:
    """Coerce to an int the target column can hold, or None.

    `bits` is the column's signed width: util_pct is SmallInteger and the VRAM
    byte counts are BigInteger, so one blanket bound is wrong in both
    directions. A value past the column raises DataError on insert; int(NaN)
    raises ValueError and int(inf) raises OverflowError. GpuSample is built
    outside record_heartbeat's try, so any of those escapes into an untracked
    task and surfaces only at GC (issue #203).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    limit = 1 << bits
    return value if -limit <= value < limit else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # float() on a big int raises OverflowError, and GpuSample is built
        # outside record_heartbeat's try, so it escapes into an untracked task.
        if isinstance(value, int) and not (-2**53 < value < 2**53):
            return None
        number = float(value)
        # NaN and infinities survive json.loads and persist in PostgreSQL.
        # Pydantic nulls them on the way out rather than 500ing, so this is
        # about not storing junk rather than about serialization (issue #203).
        return number if math.isfinite(number) else None
    return None


def _loaded_models(control: dict) -> list[str] | None:
    models = control.get("loaded_models")
    if not isinstance(models, list):
        return None
    return [str(model) for model in models]


def _worker_upsert(
    worker_id: str,
    device: str | None,
    memory_mode: str | None,
    last_seen: datetime,
):
    statement = insert(WorkerIdentity).values(
        worker_id=worker_id,
        device=device,
        memory_mode=memory_mode,
        last_seen=last_seen,
    )
    return statement.on_conflict_do_update(
        index_elements=[WorkerIdentity.worker_id],
        set_={
            "device": func.coalesce(statement.excluded.device, WorkerIdentity.device),
            "memory_mode": func.coalesce(
                statement.excluded.memory_mode, WorkerIdentity.memory_mode
            ),
            "last_seen": func.greatest(
                statement.excluded.last_seen, WorkerIdentity.last_seen
            ),
        },
    )


async def record_worker_identity(
    worker_id: str,
    device: str | None,
    memory_mode: str | None,
) -> None:
    """Upsert static worker facts without delaying the fleet registration path."""
    if db.session_factory is None:
        return
    try:
        async with db.session_factory() as session:
            await session.execute(
                _worker_upsert(worker_id, device, memory_mode, _utcnow())
            )
            await session.commit()
    except Exception as error:
        logger.warning("worker identity persistence skipped: %s", error)


def schedule_worker_identity(
    worker_id: str,
    device: str | None,
    memory_mode: str | None,
) -> None:
    asyncio.create_task(record_worker_identity(worker_id, device, memory_mode))


async def record_heartbeat(
    worker_id: str,
    control: dict,
    device: str | None = None,
    memory_mode: str | None = None,
) -> None:
    """Insert one row from a worker heartbeat; no-op when the database is down."""
    if db.session_factory is None:
        return
    gpu = _parse_gpu(control.get("gpu"))
    sampled_at = _utcnow()
    row = GpuSample(
        worker_id=worker_id,
        sampled_at=sampled_at,
        util_pct=_int_or_none(gpu.get("util_pct"), bits=15),
        vram_used_bytes=_int_or_none(gpu.get("vram_used_bytes"), bits=63),
        vram_total_bytes=_int_or_none(gpu.get("vram_total_bytes"), bits=63),
        temperature_c=_float_or_none(gpu.get("temperature_c")),
        power_w=_float_or_none(gpu.get("power_w")),
        loaded_models=_loaded_models(control),
    )
    try:
        async with db.session_factory() as session:
            session.add(row)
            await session.execute(
                _worker_upsert(worker_id, device, memory_mode, sampled_at)
            )
            await session.commit()
    except Exception as error:
        logger.warning("GPU heartbeat persistence skipped: %s", error)


def schedule_heartbeat_sample(
    worker_id: str,
    control: dict,
    device: str | None = None,
    memory_mode: str | None = None,
) -> None:
    """Fire-and-forget persistence so the fleet socket loop stays responsive."""
    if control.get("type") != "heartbeat":
        return
    asyncio.create_task(record_heartbeat(worker_id, control, device, memory_mode))


RollupMode = Literal["auto", "raw", "5m"]


def _choose_rollup(mode: RollupMode, span: timedelta) -> Literal["raw", "5m"]:
    if mode == "raw":
        return "raw"
    if mode == "5m":
        return "5m"
    if span > RAW_RETENTION:
        return "5m"
    if span > timedelta(hours=1):
        return "5m"
    return "raw"


def _floor_bucket(ts: datetime) -> datetime:
    minute = (ts.minute // 5) * 5
    return ts.replace(minute=minute, second=0, microsecond=0)


async def query_history(
    session: AsyncSession,
    from_ts: datetime,
    to_ts: datetime,
    rollup: RollupMode = "auto",
    worker_id: str | None = None,
) -> tuple[list[dict], Literal["raw", "5m"]]:
    span = to_ts - from_ts
    chosen = _choose_rollup(rollup, span)
    if chosen == "raw":
        raw_query = select(GpuSample).where(
            GpuSample.sampled_at >= from_ts,
            GpuSample.sampled_at <= to_ts,
        )
        if worker_id is not None:
            raw_query = raw_query.where(GpuSample.worker_id == worker_id)
        raw_query = raw_query.order_by(GpuSample.sampled_at)
        raw_rows = (await session.execute(raw_query)).scalars().all()
        return [_serialize_raw(row) for row in raw_rows], "raw"

    rollup_query = select(GpuSampleRollup).where(
        GpuSampleRollup.bucket_start >= from_ts,
        GpuSampleRollup.bucket_start <= to_ts,
    )
    if worker_id is not None:
        rollup_query = rollup_query.where(GpuSampleRollup.worker_id == worker_id)
    rollup_query = rollup_query.order_by(GpuSampleRollup.bucket_start)
    rollup_rows = (await session.execute(rollup_query)).scalars().all()
    if rollup_rows or rollup != "auto" or span > RAW_RETENTION:
        return [_serialize_rollup(row) for row in rollup_rows], "5m"

    raw_query = select(GpuSample).where(
        GpuSample.sampled_at >= from_ts,
        GpuSample.sampled_at <= to_ts,
    )
    if worker_id is not None:
        raw_query = raw_query.where(GpuSample.worker_id == worker_id)
    raw_query = raw_query.order_by(GpuSample.sampled_at)
    raw_rows = (await session.execute(raw_query)).scalars().all()
    return [_serialize_raw(row) for row in raw_rows], "raw"


def _serialize_raw(row: GpuSample) -> dict:
    used_pct = _vram_used_pct(row.vram_used_bytes, row.vram_total_bytes)
    return {
        "ts": row.sampled_at.isoformat(),
        "worker_id": row.worker_id,
        "util_pct": row.util_pct,
        "vram_used_pct": used_pct,
        "vram_used_bytes": row.vram_used_bytes,
        "vram_total_bytes": row.vram_total_bytes,
        "temperature_c": row.temperature_c,
        "power_w": row.power_w,
    }


def _serialize_rollup(row: GpuSampleRollup) -> dict:
    return {
        "ts": row.bucket_start.isoformat(),
        "worker_id": row.worker_id,
        "util_pct": round(row.util_mean) if row.util_mean is not None else None,
        "util_min": row.util_min,
        "util_max": row.util_max,
        "vram_used_pct": round(row.vram_used_pct_mean)
        if row.vram_used_pct_mean is not None
        else None,
        "vram_min": row.vram_used_pct_min,
        "vram_max": row.vram_used_pct_max,
        "temperature_c": row.temperature_mean,
        "power_w": row.power_mean,
        "sample_count": row.sample_count,
    }


async def maintain_once() -> None:
    """Refresh estimates, rebuild rollups, then prune raw and stale rows."""
    await estimates.refresh_observed_timings()
    if db.session_factory is None:
        return
    now = _utcnow()
    raw_cutoff = now - RAW_RETENTION
    rollup_cutoff = now - ROLLUP_RETENTION
    worker_cutoff = now - WORKER_RETENTION
    usage_raw_cutoff = _usage_raw_cutoff(now)
    async with db.session_factory() as session:
        await _rebuild_rollups(session, raw_cutoff, now)
        await _rebuild_usage_rollups(session, usage_raw_cutoff)
        await session.execute(delete(GpuSample).where(GpuSample.sampled_at < raw_cutoff))
        await session.execute(
            delete(GpuSampleRollup).where(GpuSampleRollup.bucket_start < rollup_cutoff)
        )
        await session.execute(
            delete(WorkerIdentity).where(WorkerIdentity.last_seen < worker_cutoff)
        )
        await session.execute(
            delete(UsageEvent).where(UsageEvent.created_at < usage_raw_cutoff)
        )
        await session.commit()
    # Last: an audit prune that fails must not take the retention above it
    # with it, or these tables stop being pruned at all.
    await audit.prune()
    await oauth.prune()


async def _rebuild_rollups(session: AsyncSession, from_ts: datetime, to_ts: datetime) -> None:
    rows = (
        await session.execute(
            select(GpuSample).where(
                GpuSample.sampled_at >= from_ts,
                GpuSample.sampled_at <= to_ts,
            )
        )
    ).scalars().all()
    buckets: dict[tuple[str, datetime], list[GpuSample]] = {}
    for row in rows:
        key = (row.worker_id, _floor_bucket(row.sampled_at))
        buckets.setdefault(key, []).append(row)

    for (worker_id, bucket_start), samples in buckets.items():
        util_values = [sample.util_pct for sample in samples if sample.util_pct is not None]
        vram_values: list[int] = []
        for sample in samples:
            pct = _vram_used_pct(sample.vram_used_bytes, sample.vram_total_bytes)
            if pct is not None:
                vram_values.append(pct)
        temp_values = [
            sample.temperature_c for sample in samples if sample.temperature_c is not None
        ]
        power_values = [sample.power_w for sample in samples if sample.power_w is not None]
        payload = {
            "worker_id": worker_id,
            "bucket_start": bucket_start,
            "sample_count": len(samples),
            "util_mean": (sum(util_values) / len(util_values)) if util_values else None,
            "util_min": min(util_values) if util_values else None,
            "util_max": max(util_values) if util_values else None,
            "vram_used_pct_mean": (sum(vram_values) / len(vram_values)) if vram_values else None,
            "vram_used_pct_min": min(vram_values) if vram_values else None,
            "vram_used_pct_max": max(vram_values) if vram_values else None,
            "temperature_mean": (sum(temp_values) / len(temp_values)) if temp_values else None,
            "power_mean": (sum(power_values) / len(power_values)) if power_values else None,
        }
        stmt = insert(GpuSampleRollup).values(**payload)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["worker_id", "bucket_start"],
            set_={
                "sample_count": excluded.sample_count,
                "util_mean": excluded.util_mean,
                "util_min": excluded.util_min,
                "util_max": excluded.util_max,
                "vram_used_pct_mean": excluded.vram_used_pct_mean,
                "vram_used_pct_min": excluded.vram_used_pct_min,
                "vram_used_pct_max": excluded.vram_used_pct_max,
                "temperature_mean": excluded.temperature_mean,
                "power_mean": excluded.power_mean,
            },
        )
        await session.execute(stmt)


def _usage_raw_cutoff(now: datetime) -> datetime:
    cutoff_date = (now - USAGE_RAW_RETENTION).date()
    return datetime.combine(cutoff_date, time.min, tzinfo=timezone.utc)


async def _rebuild_usage_rollups(session: AsyncSession, before_ts: datetime) -> None:
    bucket_date = cast(
        UsageEvent.created_at.op("AT TIME ZONE")("UTC"), Date
    ).label("bucket_date")
    dimensions = (
        UsageEvent.user_id,
        bucket_date,
        UsageEvent.kind,
        UsageEvent.action,
        UsageEvent.model_id,
        UsageEvent.tier,
        UsageEvent.category,
    )
    aggregate = (
        select(
            # Selected explicitly: a Python-side uuid default is evaluated once for
            # the whole INSERT ... FROM SELECT, so every row would share one id.
            func.gen_random_uuid().label("id"),
            *dimensions,
            func.count().label("event_count"),
            func.sum(UsageEvent.category_score).label("category_score_sum"),
            func.count(UsageEvent.category_score).label("category_score_count"),
            func.sum(UsageEvent.gpu_ms).label("gpu_ms_sum"),
            func.sum(UsageEvent.duration_ms).label("duration_ms_sum"),
            func.sum(UsageEvent.frames).label("frames_sum"),
        )
        .where(UsageEvent.created_at < before_ts)
        .group_by(*dimensions)
    )
    # One server-side statement: the aggregate never lands in Python, and the
    # first run after an upgrade does not become a round trip per rollup row.
    stmt = insert(UsageEventRollup).from_select(
        [
            "id",
            "user_id",
            "bucket_date",
            "kind",
            "action",
            "model_id",
            "tier",
            "category",
            "event_count",
            "category_score_sum",
            "category_score_count",
            "gpu_ms_sum",
            "duration_ms_sum",
            "frames_sum",
        ],
        aggregate,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "user_id",
            "bucket_date",
            "kind",
            "action",
            "model_id",
            text("COALESCE(tier, '')"),
            "category",
        ],
        set_={
            "event_count": excluded.event_count,
            "category_score_sum": excluded.category_score_sum,
            "category_score_count": excluded.category_score_count,
            "gpu_ms_sum": excluded.gpu_ms_sum,
            "duration_ms_sum": excluded.duration_ms_sum,
            "frames_sum": excluded.frames_sum,
        },
    )
    await session.execute(stmt)


async def maintain_loop() -> None:
    while True:
        try:
            await maintain_once()
        except Exception:
            logger.exception("gpu sample maintenance failed")
        await asyncio.sleep(MAINTAIN_INTERVAL)


async def latest_sample_at(session: AsyncSession) -> datetime | None:
    return (await session.execute(select(func.max(GpuSample.sampled_at)))).scalar_one_or_none()
