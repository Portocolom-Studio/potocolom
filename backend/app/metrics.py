"""Metrics history API (issue #94) and a member's own usage (issue #95)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Float, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, gpu_samples
from app.auth import require, require_accounts_mode
from app.capabilities import Capability
from app.manifests import StorableStr
from app.tables import BillingBinding, UsageEvent, User

router = APIRouter()

# One year is the widest window this route will scan: it bounds the GROUP BY
# one member can drive, and the raw rows only reach back 90 days anyway
# (docs/metrics.md), so a wider window would promise history that is gone.
USAGE_DEFAULT_WINDOW = timedelta(days=30)
USAGE_MAX_WINDOW = timedelta(days=366)


def _parse_ts(value: str, name: str) -> datetime:
    text = value.strip()
    try:
        if text.isdigit():
            # isdigit() is true for superscripts and other numeric characters
            # int() rejects, and an epoch far enough out overflows the year or
            # the float division; all three are a bad timestamp, not a 500.
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # A naive wall clock reads as UTC only if someone says so; without
            # a timezone the caller's intent is unknown, so refuse rather than
            # guess (issue #497).
            raise HTTPException(status_code=422, detail=f"naive {name} timestamp")
        # astimezone overflows at the edges of the representable range, e.g.
        # 0001-01-01T00:00:00+14:00, so it belongs inside the try too.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise HTTPException(status_code=422, detail=f"invalid {name} timestamp") from error


@router.get("/api/v1/metrics/gpu/history")
async def gpu_history(
    from_: StorableStr = Query(alias="from"),
    to: StorableStr = Query(),
    rollup: gpu_samples.RollupMode = "auto",
    worker_id: StorableStr | None = None,
    user: User = Depends(require(Capability.ADMINISTER)),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    del user  # studio scope is this install; auth keeps the route consistent.
    from_ts = _parse_ts(from_, "from")
    to_ts = _parse_ts(to, "to")
    if to_ts <= from_ts:
        raise HTTPException(status_code=422, detail="to must be after from")
    samples, chosen, truncated = await gpu_samples.query_history(
        session, from_ts, to_ts, rollup=rollup, worker_id=worker_id
    )
    return {
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "rollup": chosen,
        "samples": samples,
        "truncated": truncated,
    }


@router.get("/api/v1/usage/me")
async def usage_me(
    from_: StorableStr | None = Query(default=None, alias="from"),
    to: StorableStr | None = Query(default=None),
    user: User = Depends(require(Capability.VIEW_OWN_USAGE)),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    """One member's own usage events; a viewer creates nothing, so it is a 403."""
    to_ts = _parse_ts(to, "to") if to is not None else datetime.now(timezone.utc)
    try:
        from_ts = _parse_ts(from_, "from") if from_ is not None else to_ts - USAGE_DEFAULT_WINDOW
    except OverflowError as error:
        # A "to" near year 1 has no default window before it.
        raise HTTPException(status_code=422, detail="invalid to timestamp") from error
    if to_ts <= from_ts:
        raise HTTPException(status_code=422, detail="to must be after from")
    if to_ts - from_ts > USAGE_MAX_WINDOW:
        raise HTTPException(status_code=422, detail="window longer than 366 days")
    window = (
        UsageEvent.user_id == user.id,
        UsageEvent.created_at >= from_ts,
        UsageEvent.created_at < to_ts,
    )
    totals = (
        await session.execute(
            select(
                func.count().label("events"),
                func.coalesce(func.sum(UsageEvent.gpu_ms), 0).label("gpu_ms"),
                func.coalesce(func.sum(UsageEvent.frames), 0).label("frames"),
            ).where(*window)
        )
    ).one()
    by_category = (
        await session.execute(
            select(UsageEvent.category, func.count().label("events"))
            .where(*window)
            .group_by(UsageEvent.category)
            .order_by(func.count().desc(), UsageEvent.category)
        )
    ).all()
    by_model = (
        await session.execute(
            select(
                UsageEvent.model_id,
                func.count().label("events"),
                # avg skips the NULLs itself; percentile_cont skips them too, so a
                # job with no recorded duration never drags the median down.
                func.avg(cast(UsageEvent.gpu_ms, Float)).label("avg_gpu_ms"),
                func.percentile_cont(0.5)
                .within_group(UsageEvent.duration_ms)
                .label("p50_duration_ms"),
            )
            .where(*window)
            .group_by(UsageEvent.model_id)
            .order_by(func.count().desc(), UsageEvent.model_id)
        )
    ).all()
    by_model_category = (
        await session.execute(
            select(
                UsageEvent.model_id,
                UsageEvent.category,
                func.count().label("events"),
                func.avg(cast(UsageEvent.duration_ms, Float)).label("avg_duration_ms"),
            )
            .where(*window)
            .group_by(UsageEvent.model_id, UsageEvent.category)
            .order_by(func.count().desc(), UsageEvent.model_id, UsageEvent.category)
        )
    ).all()
    return {
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "totals": {
            "events": totals.events,
            "gpu_ms": totals.gpu_ms,
            "frames": totals.frames,
        },
        "by_category": [
            {"category": row.category, "events": row.events} for row in by_category
        ],
        "by_model": [
            {
                "model_id": row.model_id,
                "events": row.events,
                "avg_gpu_ms": row.avg_gpu_ms,
                "p50_duration_ms": row.p50_duration_ms,
            }
            for row in by_model
        ],
        "by_model_category": [
            {
                "model_id": row.model_id,
                "category": row.category,
                "events": row.events,
                "avg_duration_ms": row.avg_duration_ms,
            }
            for row in by_model_category
        ],
    }


def _usage_window(from_: str | None, to: str | None) -> tuple[datetime, datetime]:
    to_ts = _parse_ts(to, "to") if to is not None else datetime.now(timezone.utc)
    try:
        from_ts = _parse_ts(from_, "from") if from_ is not None else to_ts - USAGE_DEFAULT_WINDOW
    except OverflowError as error:
        # A "to" near year 1 has no default window before it.
        raise HTTPException(status_code=422, detail="invalid to timestamp") from error
    if to_ts <= from_ts:
        raise HTTPException(status_code=422, detail="to must be after from")
    if to_ts - from_ts > USAGE_MAX_WINDOW:
        raise HTTPException(status_code=422, detail="window longer than 366 days")
    return from_ts, to_ts


@router.get("/api/v1/usage/subject")
async def usage_subject(
    from_: StorableStr | None = Query(default=None, alias="from"),
    to: StorableStr | None = Query(default=None),
    _: None = Depends(require_accounts_mode),
    user: User = Depends(require(Capability.VIEW_SUBJECT_USAGE)),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    """Totals for the account this caller is bound to.

    The subject is the binding row joined in this query. The route takes no
    subject id, and a query parameter naming one is not read. A suspended
    caller is refused before that query. An unbound caller, and a subject
    that is not an active member or administrator, are one 404.
    """
    if user.state != "active":
        raise HTTPException(status_code=403, detail="account suspended")
    from_ts, to_ts = _usage_window(from_, to)
    eligible = and_(
        User.id == BillingBinding.subject_user_id,
        User.state == "active",
        User.role.in_(("user", "admin")),
    )
    in_window = and_(
        UsageEvent.user_id == BillingBinding.subject_user_id,
        UsageEvent.created_at >= from_ts,
        UsageEvent.created_at < to_ts,
    )
    row = (await session.execute(
        select(
            func.count(UsageEvent.id).label("events"),
            func.coalesce(func.sum(UsageEvent.gpu_ms), 0).label("gpu_ms"),
            func.coalesce(func.sum(UsageEvent.frames), 0).label("frames"),
        )
        .select_from(BillingBinding)
        .join(User, eligible)
        .outerjoin(UsageEvent, in_window)
        .where(BillingBinding.delegate_user_id == user.id)
        .group_by(BillingBinding.subject_user_id)
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return {
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "totals": {
            "events": int(row.events),
            "gpu_ms": int(row.gpu_ms),
            "frames": int(row.frames),
        },
    }
