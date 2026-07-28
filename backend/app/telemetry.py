"""Anonymous self-hosted daily aggregate and its exact preview."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.auth import current_user
from app.settings import get_settings
from app.tables import TelemetryState, UsageEvent, User, WorkerIdentity

logger = logging.getLogger("potocolom.telemetry")
router = APIRouter()
DESTINATION = "https://telemetry.potocolom.com/v1/report"


def _version() -> str:
    try:
        return version("potocolom-backend")
    except PackageNotFoundError:
        return "0.0.1"


async def _state(session: AsyncSession) -> TelemetryState:
    state = await session.get(TelemetryState, 1)
    if state is None:
        state = TelemetryState(id=1)
        session.add(state)
        await session.flush()
    return state


async def _counts_by(session: AsyncSession, column, start: datetime, end: datetime) -> dict[str, int]:
    """One GROUP BY per dimension, so a busy day never lands in memory."""
    rows = await session.execute(
        select(column, func.count())
        .where(UsageEvent.created_at >= start, UsageEvent.created_at < end,
               column.is_not(None))
        .group_by(column)
    )
    return {str(value): int(total) for value, total in rows.all()}


async def payload_for_day(session: AsyncSession, day: date) -> dict:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    in_day = (UsageEvent.created_at >= start, UsageEvent.created_at < end)
    active_users = await session.scalar(
        select(func.count(func.distinct(UsageEvent.user_id))).where(*in_day)
    )
    realtime_ms = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.duration_ms), 0))
        .where(*in_day, UsageEvent.kind == "realtime")
    )
    by_kind = await _counts_by(session, UsageEvent.kind, start, end)
    by_action = await _counts_by(session, UsageEvent.action, start, end)
    by_category = await _counts_by(session, UsageEvent.category, start, end)
    by_tier = await _counts_by(session, UsageEvent.tier, start, end)
    workers = (
        await session.execute(
            select(WorkerIdentity)
            .where(WorkerIdentity.last_seen >= start)
            .order_by(WorkerIdentity.worker_id)
        )
    ).scalars().all()
    state = await _state(session)
    await session.commit()
    return {
        "install_id": str(state.install_id),
        "version": _version(),
        "day": day.isoformat(),
        "active_users": int(active_users or 0),
        "events": by_kind,
        "by_action": by_action,
        "by_category": by_category,
        "by_tier": by_tier,
        "realtime_minutes": round(int(realtime_ms or 0) / 60_000),
        "workers": [
            {"device": worker.device, "memory_mode": worker.memory_mode}
            for worker in workers
            if worker.device is not None and worker.memory_mode is not None
        ],
    }


@router.get("/api/v1/telemetry/preview")
async def telemetry_preview(
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    return await payload_for_day(session, datetime.now(timezone.utc).date() - timedelta(days=1))


def _post(url: str, payload: dict) -> None:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"telemetry destination returned {response.status}")


async def send_once() -> None:
    settings = get_settings()
    if not settings.telemetry or db.session_factory is None:
        return
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    async with db.session_factory() as session:
        state = await _state(session)
        if state.last_report_day == day:
            return
        payload = await payload_for_day(session, day)
        # Mark before the network call: failures are deliberately dropped, never queued.
        state = await _state(session)
        state.last_report_day = day
        await session.commit()
    logger.info(
        "telemetry daily summary day=%s active_users=%s events=%s",
        day, payload["active_users"], sum(payload["events"].values()),
    )
    try:
        await asyncio.to_thread(_post, DESTINATION, payload)
    except Exception as error:
        logger.warning("telemetry send dropped: %s", error)


async def telemetry_loop() -> None:
    while True:
        try:
            await send_once()
        except Exception:
            logger.exception("telemetry aggregation failed")
        await asyncio.sleep(3600)
