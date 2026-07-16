"""Persist worker GPU heartbeats and serve history (issue #94, docs/metrics.md)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.tables import GpuSample, GpuSampleRollup

logger = logging.getLogger("potocolom.gpu_samples")

RAW_RETENTION = timedelta(hours=48)
ROLLUP_RETENTION = timedelta(days=30)
ROLLUP_BUCKET = timedelta(minutes=5)
MAINTAIN_INTERVAL = 300.0  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _vram_used_pct(used: int | None, total: int | None) -> int | None:
    if used is None or total is None or total <= 0:
        return None
    return round(used * 100 / total)


def _parse_gpu(gpu: Any) -> dict[str, Any]:
    return gpu if isinstance(gpu, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _loaded_models(control: dict) -> list[str] | None:
    models = control.get("loaded_models")
    if not isinstance(models, list):
        return None
    return [str(model) for model in models]


async def record_heartbeat(worker_id: str, control: dict) -> None:
    """Insert one row from a worker heartbeat; no-op when the database is down."""
    if db.session_factory is None:
        return
    gpu = _parse_gpu(control.get("gpu"))
    sampled_at = _utcnow()
    row = GpuSample(
        worker_id=worker_id,
        sampled_at=sampled_at,
        util_pct=_int_or_none(gpu.get("util_pct")),
        vram_used_bytes=_int_or_none(gpu.get("vram_used_bytes")),
        vram_total_bytes=_int_or_none(gpu.get("vram_total_bytes")),
        temperature_c=_float_or_none(gpu.get("temperature_c")),
        power_w=_float_or_none(gpu.get("power_w")),
        loaded_models=_loaded_models(control),
    )
    async with db.session_factory() as session:
        session.add(row)
        await session.commit()


def schedule_heartbeat_sample(worker_id: str, control: dict) -> None:
    """Fire-and-forget persistence so the fleet socket loop stays responsive."""
    if control.get("type") != "heartbeat":
        return
    asyncio.create_task(record_heartbeat(worker_id, control))


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
    """Roll raw samples into five-minute buckets, then prune old rows."""
    if db.session_factory is None:
        return
    now = _utcnow()
    raw_cutoff = now - RAW_RETENTION
    rollup_cutoff = now - ROLLUP_RETENTION
    async with db.session_factory() as session:
        await _rebuild_rollups(session, raw_cutoff, now)
        await session.execute(delete(GpuSample).where(GpuSample.sampled_at < raw_cutoff))
        await session.execute(
            delete(GpuSampleRollup).where(GpuSampleRollup.bucket_start < rollup_cutoff)
        )
        await session.commit()


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


async def maintain_loop() -> None:
    while True:
        await asyncio.sleep(MAINTAIN_INTERVAL)
        try:
            await maintain_once()
        except Exception:
            logger.exception("gpu sample maintenance failed")


async def latest_sample_at(session: AsyncSession) -> datetime | None:
    return (await session.execute(select(func.max(GpuSample.sampled_at)))).scalar_one_or_none()
