"""Durable record of privileged action, written before the action runs.

Audit fails open. An action proceeds when only its audit insert fails, because
refusing privileged work every time the audit table is unreachable turns one
incident into an outage. What the failure must never be is silent, so a lost
record leaves a structured log line, a bounded spool keeps it for the next
successful insert, and the seven-day summary shows the gap.
"""

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app import db
from app.tables import AuditEvent, User

logger = logging.getLogger("potocolom.audit")

SPOOL_LIMIT = 1000
OBJECT_ID_CAP = 100
RETENTION = timedelta(days=90)
SUMMARY_WINDOW = timedelta(days=7)
SEARCH_LIMIT = 1000
# The delivery lock is process wide and the insert takes a pooled connection,
# so an unbounded wait would queue every admin request behind one slow write.
DELIVERY_TIMEOUT = 5.0

OVERFLOW_ACTION = "audit.overflow"
FALLBACK_ACTION = "audit.fallback"
EXPORT_ACTION = "audit.export"
GAP_ACTIONS = (FALLBACK_ACTION, OVERFLOW_ACTION)


@dataclass
class Pending:
    action: str
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None
    actor_role: str | None = None
    target_user_id: uuid.UUID | None = None
    object_ids: list[str] = field(default_factory=list)
    object_count: int = 0
    truncated: bool = False
    severity: str = "info"


_spool: deque[Pending] = deque()
_dropped = 0
_fell_back = 0
# One delivery at a time. Two that read the spool concurrently would each
# insert all of it, so a recovery would duplicate every held record.
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record(
    action: str,
    *,
    actor: User | None = None,
    target_user_id: uuid.UUID | None = None,
    object_ids: "list[str] | tuple[str, ...]" = (),
    severity: str = "info",
    object_count: int | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Never raises. The caller's action is not this function's to refuse."""
    ids = [str(one) for one in object_ids]
    await _deliver(Pending(
        action=action,
        occurred_at=occurred_at or _now(),
        actor_user_id=actor.id if actor is not None else None,
        actor_role=actor.role if actor is not None else None,
        target_user_id=target_user_id,
        object_ids=ids[:OBJECT_ID_CAP],
        object_count=len(ids) if object_count is None else object_count,
        truncated=len(ids) > OBJECT_ID_CAP,
        severity=severity,
    ))


async def _deliver(event: Pending) -> None:
    global _fell_back
    try:
        await asyncio.wait_for(_locked(event), DELIVERY_TIMEOUT)
    except Exception:
        # The lock or anything else unexpected. A broken audit must not take
        # the caller's action down with it, which is the whole contract.
        _fell_back += 1
        _log_fallback(event)
        _push(event)


async def _locked(event: Pending) -> None:
    async with _lock:
        await _deliver_locked(event)


async def _deliver_locked(event: Pending) -> None:
    global _dropped, _fell_back
    sent = list(_spool)
    dropped, fell_back = _dropped, _fell_back
    try:
        await _insert([*sent, event, *_markers()])
    except Exception:
        _fell_back += 1
        _log_fallback(event)
        _push(event)
        return
    # Only what this insert carried. A record whose own delivery timed out
    # while this one was in flight was spooled behind it, and clearing the
    # whole spool would destroy that record and zero the count that would
    # have made the gap visible: silent loss, which is the one outcome this
    # module exists to prevent.
    for one in sent:
        if _spool and _spool[0] is one:
            _spool.popleft()
    _dropped -= dropped
    _fell_back -= fell_back


def _markers() -> list[Pending]:
    """The two events that make a gap visible once the audit table is back."""
    now = _now()
    markers = []
    if _fell_back:
        markers.append(Pending(action=FALLBACK_ACTION, occurred_at=now,
                               object_count=_fell_back, severity="high"))
    if _dropped:
        markers.append(Pending(action=OVERFLOW_ACTION, occurred_at=now,
                               object_count=_dropped, severity="high"))
    return markers


def _push(event: Pending) -> None:
    global _dropped
    while len(_spool) >= SPOOL_LIMIT:
        _spool.popleft()
        _dropped += 1
    _spool.append(event)


def _log_fallback(event: Pending) -> None:
    logger.warning(json.dumps({
        "audit": "fallback",
        "action": event.action,
        "occurred_at": event.occurred_at.isoformat(),
        "actor_user_id": str(event.actor_user_id) if event.actor_user_id else None,
        "actor_role": event.actor_role,
        "target_user_id": str(event.target_user_id) if event.target_user_id else None,
        "object_count": event.object_count,
        "truncated": event.truncated,
        "severity": event.severity,
    }))


async def _insert(events: list[Pending]) -> None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        session.add_all([
            AuditEvent(
                occurred_at=event.occurred_at,
                actor_user_id=event.actor_user_id,
                actor_role=event.actor_role,
                action=event.action,
                target_user_id=event.target_user_id,
                object_ids=event.object_ids,
                object_count=event.object_count,
                truncated=event.truncated,
                severity=event.severity,
            )
            for event in events
        ])
        await session.commit()


async def summary(window: timedelta = SUMMARY_WINDOW) -> dict:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(AuditEvent.action, func.count(), func.sum(AuditEvent.object_count))
            .where(AuditEvent.occurred_at >= _now() - window)
            .group_by(AuditEvent.action)
            .order_by(AuditEvent.action)
        )).all()
    actions = {action: count for action, count, _ in rows if action not in GAP_ACTIONS}
    gaps = [{"action": action, "events": int(counted or 0)}
            for action, _, counted in rows if action in GAP_ACTIONS]
    return {"actions": actions, "gaps": gaps}


async def search(
    *,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = SEARCH_LIMIT,
) -> list[dict]:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    # A cap, not a default: export builds the whole result in memory three
    # times over, so a caller-supplied limit must not decide how much.
    query = (select(AuditEvent).order_by(AuditEvent.occurred_at.desc())
             .limit(max(1, min(limit, SEARCH_LIMIT))))
    if actor_user_id is not None:
        query = query.where(AuditEvent.actor_user_id == actor_user_id)
    if target_user_id is not None:
        query = query.where(AuditEvent.target_user_id == target_user_id)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    if since is not None:
        query = query.where(AuditEvent.occurred_at >= since)
    if until is not None:
        query = query.where(AuditEvent.occurred_at <= until)
    async with db.session_factory() as session:
        rows = (await session.execute(query)).scalars().all()
    return [
        {
            "id": str(row.id),
            "occurred_at": row.occurred_at.isoformat(),
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "actor_role": row.actor_role,
            "action": row.action,
            "target_user_id": str(row.target_user_id) if row.target_user_id else None,
            "object_ids": row.object_ids,
            "object_count": row.object_count,
            "truncated": row.truncated,
            "severity": row.severity,
        }
        for row in rows
    ]


async def export(*, actor: User | None = None, **filters) -> str:
    """Reading the whole audit is itself a privileged action, so it is audited.

    The record is written after the query and before the caller receives
    anything, because the ids it carries are the point and a failure between
    the two returns nothing to anyone.
    """
    rows = await search(**filters)
    await record(EXPORT_ACTION, actor=actor, object_ids=[row["id"] for row in rows])
    # A bare array cannot say whether it is the whole audit or the newest page
    # of a much longer one, and an operator exporting an incident needs to know.
    return json.dumps({
        "events": rows,
        "truncated": len(rows) >= min(filters.get("limit", SEARCH_LIMIT), SEARCH_LIMIT),
    })


async def prune() -> None:
    if db.session_factory is None:
        return
    async with db.session_factory() as session:
        await session.execute(delete(AuditEvent).where(AuditEvent.occurred_at < _now() - RETENTION))
        await session.commit()
