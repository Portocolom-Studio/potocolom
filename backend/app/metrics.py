"""Metrics history API (issue #94)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, gpu_samples
from app.auth import current_user
from app.tables import User

router = APIRouter()


def _parse_ts(value: str, name: str) -> datetime:
    text = value.strip()
    if text.isdigit():
        ms = int(text)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"invalid {name} timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@router.get("/api/v1/metrics/gpu/history")
async def gpu_history(
    from_: str = Query(alias="from"),
    to: str = Query(),
    rollup: gpu_samples.RollupMode = "auto",
    worker_id: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    del user  # studio scope is this install; auth keeps the route consistent.
    from_ts = _parse_ts(from_, "from")
    to_ts = _parse_ts(to, "to")
    if to_ts <= from_ts:
        raise HTTPException(status_code=422, detail="to must be after from")
    samples, chosen = await gpu_samples.query_history(
        session, from_ts, to_ts, rollup=rollup, worker_id=worker_id
    )
    return {
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "rollup": chosen,
        "samples": samples,
    }
