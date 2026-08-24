"""Leaving, and being let back in.

A deletion is a request with a waiting period behind it, not a switch. The
account stops at once, and for as long as the window lasts a restore puts it
back where it was. When the window runs out the purge takes the work, then the
objects behind it, and only then the row itself, in the order the foreign keys
demand.
"""

import asyncio
import json
import logging
import random
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response, StreamingResponse

from app import audit, db, jobs, sessions
from app.auth import current_principal, require_accounts_mode, require_role
from app.tables import Asset, AuthIdentity, AuthToken, Job, Session, User

logger = logging.getLogger("potocolom.deletion")

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

DELETION_REQUESTED = "account.deletion"
ACCOUNT_RESTORED = "account.restore"
# Long enough that somebody who deleted an account in anger, or by mistake,
# still has it on the day they change their mind.
RESTORE_WINDOW_DAYS = 30
PURGE_INTERVAL = 3600.0  # seconds
PURGE_BATCH = 20


async def request(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Stop the account and remember where it came from.

    Adds to the caller's transaction without committing: the state, the
    revocation and the note of what to restore become durable together or not
    at all.
    """
    changed = (await session.execute(
        update(User)
        .where(User.id == user_id, User.state != "deletion_pending", User.state != "purging")
        .values(prior_state=User.state, state="deletion_pending",
                deletion_requested_at=func.now())
        .returning(User.id)
    )).first()
    if changed is None:
        return False
    await session.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    await session.execute(
        update(AuthToken)
        .where(AuthToken.user_id == user_id, AuthToken.consumed_at.is_(None))
        .values(consumed_at=func.now())
    )
    return True


EXPORT_PAGE = 200


@router.get("/api/v1/account/export")
async def export_account(
    principal: sessions.Resolved = Depends(current_principal),
) -> StreamingResponse:
    """Everything this install holds about one account, as one JSON document.

    Streamed and paged: a library of ten thousand generations must not have to
    fit in memory at once, here or in the process that asked. No secret goes
    in it, because a file that leaves the building takes whatever is in it
    wherever it goes: a password hash is an offline cracking target and a
    session hash is a live credential.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user = principal.user
    return StreamingResponse(_export_document(user), media_type="application/json",
                             headers={"Content-Disposition":
                                      'attachment; filename="potocolom-export.json"'})


async def _export_document(user: User) -> AsyncIterator[str]:
    assert db.session_factory is not None
    stopped = HTTPException(status_code=409, detail="account no longer active")
    yield '{"account":' + json.dumps({
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "state": user.state,
        "mail_verified": user.mail_verified,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    })
    async with db.session_factory() as session:
        identities = (await session.execute(
            select(AuthIdentity.provider, AuthIdentity.subject, AuthIdentity.created_at)
            .where(AuthIdentity.user_id == user.id)
        )).all()
        yield ',"identities":' + json.dumps([
            # The subject, never the hash beside it: for a password identity
            # the subject is the address the account signs in with.
            {"provider": provider, "subject": subject,
             "created_at": created.isoformat() if created else None}
            for provider, subject, created in identities
        ])
        yield ',"generations":['
        first = True
        after: uuid.UUID | None = None
        while True:
            # Between pages, because a page is where this can stop. An account
            # that is stopped while its export runs must stop receiving it: the
            # session behind this request was revoked at the same moment.
            if (await session.execute(
                    select(User.state).where(User.id == user.id))).scalar_one_or_none() != "active":
                raise stopped
            page = list((await session.execute(
                select(Job).where(Job.user_id == user.id,
                                  *( [Job.id > after] if after is not None else [] ))
                .order_by(Job.id).limit(EXPORT_PAGE)
            )).scalars().all())
            if not page:
                break
            assets: dict[uuid.UUID, list[Asset]] = {}
            for row in (await session.execute(
                select(Asset).where(Asset.job_id.in_([job.id for job in page]))
            )).scalars().all():
                if row.job_id is not None:
                    assets.setdefault(row.job_id, []).append(row)
            for job in page:
                yield ("" if first else ",") + json.dumps(_exported_job(job, assets.get(job.id, [])))
                first = False
            after = page[-1].id
    yield "]}"


def _exported_job(job: Job, assets: list[Asset]) -> dict:
    return {
        "id": str(job.id),
        "model_id": job.model_id,
        "params": job.params,
        "state": job.state,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "gpu_ms": job.gpu_ms,
        "assets": [
            {"id": str(asset.id), "mime": asset.mime,
             "width": asset.width, "height": asset.height,
             "url": jobs.asset_url(asset.id)}
            for asset in assets
        ],
    }


@router.delete("/api/v1/account", status_code=204)
async def delete_account(
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    """An account may always leave, including the last administrator's.

    An install with nobody in charge can be recovered offline; an
    administrator held hostage by their own install cannot.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user_id = principal.user.id
    async with db.session_factory() as session:
        async with session.begin():
            asked = await request(session, user_id)
            stopping = list((await session.execute(
                select(Job.id).where(Job.user_id == user_id,
                                     Job.state.in_(("queued", "running")))
            )).scalars().all()) if asked else []
    if asked:
        await sessions.close_sockets(user_id)
        for job_id in stopping:
            await jobs.cancel(job_id, reason="account deleted")
        await audit.record(DELETION_REQUESTED, actor=principal.user, target_user_id=user_id)
    return Response(status_code=204)


@router.post("/api/v1/users/{user_id}/restore", status_code=204)
async def restore(
    user_id: uuid.UUID,
    actor: User = Depends(require_role("admin")),
) -> Response:
    """Back to where it was, once. An account that never left has nowhere to
    go, which is a conflict rather than a silent no-op: somebody asked for a
    restore that cannot mean anything."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            target = await session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if (target.state == "deletion_pending"
                    and target.deletion_requested_at is not None
                    and target.deletion_requested_at
                    < datetime.now(timezone.utc) - timedelta(days=RESTORE_WINDOW_DAYS)):
                # Past the window the account is the sweep's, and a restore
                # here would hand back something the next pass destroys.
                raise HTTPException(
                    status_code=409,
                    detail=f"that account passed its {RESTORE_WINDOW_DAYS} day restore window")
            if target.state != "deletion_pending":
                if target.deletion_requested_at is None:
                    raise HTTPException(
                        status_code=409, detail="that account is not waiting to be deleted")
                # Already restored. The same answer as the first time, because
                # a retry after a timeout is not a second event.
                return Response(status_code=204)
            target.state = target.prior_state or "active"
            target.prior_state = None
    await audit.record(ACCOUNT_RESTORED, actor=actor, target_user_id=user_id)
    return Response(status_code=204)


async def purge_due() -> None:
    """Finish the accounts whose window has run out.

    One account per transaction: a pass that died halfway would otherwise lose
    every account it had started, and the work is idempotent, so the next pass
    picks up wherever this one stopped.
    """
    if db.session_factory is None:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RESTORE_WINDOW_DAYS)
    async with db.session_factory() as session:
        due = list((await session.execute(
            select(User.id)
            .where(User.state.in_(("deletion_pending", "purging")),
                   User.deletion_requested_at < cutoff)
            .order_by(User.deletion_requested_at)
            .limit(PURGE_BATCH)
        )).scalars().all())
    for user_id in due:
        try:
            await _purge(user_id, cutoff)
        except Exception:
            # One account that cannot be finished must not stop the others,
            # and the next pass finds it again exactly where it was.
            logger.exception("could not purge account %s", user_id)


async def _purge(user_id: uuid.UUID, cutoff: datetime) -> None:
    assert db.session_factory is not None
    async with db.session_factory() as session:
        async with session.begin():
            # Claimed, not announced. The ids were read in an earlier
            # transaction, and a restore between that read and this write puts
            # the account back in service: destroying it then would delete a
            # live account somebody just saved. Nothing below runs unless this
            # statement is the one that moved the row.
            claimed = (await session.execute(
                update(User)
                .where(User.id == user_id,
                       User.state.in_(("deletion_pending", "purging")),
                       User.deletion_requested_at < cutoff)
                .values(state="purging")
                .returning(User.id)
            )).first()
        if claimed is None:
            logger.info("account %s is no longer due to be purged", user_id)
            return
        keys = list((await session.execute(
            select(Asset.storage_key).where(Asset.user_id == user_id))).scalars().all())
    # Before the rows: a key nothing names any more is an object nobody will
    # ever find, and the row is the only thing that names it.
    for key in keys:
        await _forget_object(key)
    async with db.session_factory() as session:
        async with session.begin():
            # Assets first, then jobs. An asset points at the job that made it
            # and a job at the asset it started from, so the second delete
            # depends on the first having happened.
            await session.execute(delete(Asset).where(Asset.user_id == user_id))
            await session.execute(delete(Job).where(Job.user_id == user_id))
            # Everything else this account owns is ON DELETE CASCADE, and what
            # is not is ON DELETE SET NULL: an invitation it sent outlives it
            # without naming it. Audit rows carry plain ids with no foreign
            # key, so what an administrator did survives the account.
            await session.execute(delete(User).where(User.id == user_id))
    logger.info("purged account %s and the %d objects it owned", user_id, len(keys))


async def _forget_object(storage_key: str) -> None:
    """Bounded, and never fatal. A wedged mount answers never, and an object
    that will not go is recorded for the sweep that owns retries rather than
    stopping the account from being finished."""
    try:
        await jobs._bounded_delete(storage_key)
    except Exception as error:
        logger.warning("could not remove %s for a purged account", storage_key, exc_info=True)
        await jobs.record_pending_delete(storage_key, jobs._trim_error(error))


async def purge_loop() -> None:
    """Sleeps first. A sweep at startup makes every API start do this before
    it serves anything, and nothing is due that was not due a moment ago."""
    while True:
        await asyncio.sleep(PURGE_INTERVAL * random.uniform(0.9, 1.1))
        try:
            await purge_due()
        except Exception:
            logger.exception("account purge sweep failed")
