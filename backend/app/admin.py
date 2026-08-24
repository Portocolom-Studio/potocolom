"""What an administrator can see, and what seeing it leaves behind.

An administrator reads any one account completely and mutates none of them.
There is no view that crosses accounts: the way in is always a named user, and
naming one is recorded against that user, because the role check that guards
these routes cannot know which account a read reached.
"""

import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import audit, db, jobs
from app.auth import require_accounts_mode, require_role
from app.tables import Asset, Job, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

USER_READ = "user.read"
USER_LIST = "user.list"
ANOMALY = "admin.anomaly"
# One administrator opening account after account is what a stolen
# administrator session looks like from the outside. The number is a
# threshold, not a limit: nothing is refused, and the panel says so.
ANOMALY_TARGETS = 20
ANOMALY_WINDOW = 1800.0  # seconds

# When each administrator last opened each account. One entry per account
# rather than per read, so an administrator refreshing the same page all day
# costs one entry: the question is how many different accounts, and a list of
# every read would grow with the traffic it is watching.
_reads: dict[uuid.UUID, dict[uuid.UUID, float]] = defaultdict(dict)
_flagged: set[uuid.UUID] = set()


def _note_read(actor_id: uuid.UUID, target_id: uuid.UUID) -> int:
    """How many different accounts this administrator has opened lately.

    In process, like the rest of the self-hosted path: one process holds every
    admin session it issued. The cloud profile moves this to Redis with the
    rate limiting it belongs beside.
    """
    now = time.monotonic()
    seen = _reads[actor_id]
    seen[target_id] = now
    _forget_old(seen, now)
    return len(seen)


def _forget_old(seen: dict[uuid.UUID, float], now: float) -> None:
    for target in [t for t, at in seen.items() if at < now - ANOMALY_WINDOW]:
        del seen[target]


def anomalies() -> list[dict]:
    now = time.monotonic()
    panel = []
    for actor_id in list(_reads):
        seen = _reads[actor_id]
        _forget_old(seen, now)
        if not seen:
            # An administrator who has not looked at anything this window is
            # not state worth keeping: this is what stops the map growing by
            # one entry per administrator the install has ever had.
            del _reads[actor_id]
            _flagged.discard(actor_id)
            continue
        if len(seen) > ANOMALY_TARGETS:
            panel.append({
                "actor_user_id": str(actor_id),
                "distinct_targets": len(seen),
                "window_seconds": int(ANOMALY_WINDOW),
            })
    return sorted(panel, key=lambda row: row["distinct_targets"], reverse=True)


async def _seen(actor: User, target_id: uuid.UUID) -> None:
    await audit.record(USER_READ, actor=actor, target_user_id=target_id)
    distinct = _note_read(actor.id, target_id)
    if distinct > ANOMALY_TARGETS and actor.id not in _flagged:
        # Once per administrator per window: the point is that somebody looks,
        # and one row a second would bury the thing it is reporting.
        _flagged.add(actor.id)
        await audit.record(ANOMALY, actor=actor, object_ids=[str(distinct)],
                           severity="high")
    elif distinct <= ANOMALY_TARGETS:
        _flagged.discard(actor.id)


@router.get("/api/v1/users")
async def list_users(
    actor: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(db.get_session),
) -> list[dict]:
    """Who is on this install, and what state they are in. Not a gallery: it
    carries no work and no credential, only what user management needs.

    Recorded like every other privileged read. It reaches every account at
    once, which is more than a selected-user page reaches, not less.
    """
    rows = list((await session.execute(select(User).order_by(User.created_at))).scalars().all())
    await audit.record(USER_LIST, actor=actor, object_ids=[str(row.id) for row in rows],
                       object_count=len(rows))
    return [_account_row(row) for row in rows]


@router.get("/api/v1/users/{user_id}")
async def read_user(
    user_id: uuid.UUID,
    actor: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    counted = (await session.execute(
        select(func.count()).select_from(Job).where(Job.user_id == user_id)
    )).scalar_one()
    assets = (await session.execute(
        select(func.count()).select_from(Asset).where(Asset.user_id == user_id)
    )).scalar_one()
    await _seen(actor, user_id)
    return {**_account_row(target), "generations": counted, "assets": assets}


@router.get("/api/v1/users/{user_id}/generations")
async def read_user_generations(
    user_id: uuid.UUID,
    limit: int = 50,
    actor: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(db.get_session),
) -> list[dict]:
    """The same view the account has of its own work, read only.

    Complete, including the lineage the owner sees, because an administrator
    answering a complaint about one image needs what produced it.
    """
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    rows = (await session.execute(
        select(Job).where(Job.user_id == user_id)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(min(max(limit, 1), 200))
    )).scalars().all()
    await _seen(actor, user_id)
    return await jobs.serialize_jobs(session, list(rows))


@router.get("/api/v1/audit")
async def read_audit(
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = Query(default=audit.SEARCH_LIMIT),
    _admin: User = Depends(require_role("admin")),
) -> list[dict]:
    return await audit.search(actor_user_id=actor_user_id, target_user_id=target_user_id,
                              action=action, limit=limit)


@router.get("/api/v1/audit/summary")
async def read_summary(_admin: User = Depends(require_role("admin"))) -> dict:
    """Seven days of privileged action, and the gaps in it.

    A gap is what an audit outage left behind. It is reported here rather than
    left to be noticed, because an audit that fails open and says nothing is
    an audit nobody can rely on.
    """
    return await audit.summary()


@router.get("/api/v1/audit/anomalies")
async def read_anomalies(_admin: User = Depends(require_role("admin"))) -> list[dict]:
    return anomalies()


@router.get("/api/v1/audit/export")
async def export_audit(
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = Query(default=audit.SEARCH_LIMIT),
    actor: User = Depends(require_role("admin")),
) -> Response:
    body = await audit.export(actor=actor, actor_user_id=actor_user_id,
                              target_user_id=target_user_id, action=action, limit=limit)
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="audit.json"'})


def _account_row(row: User) -> dict:
    return {
        "id": str(row.id),
        "email": row.email,
        "role": row.role,
        "state": row.state,
        "mail_verified": row.mail_verified,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
