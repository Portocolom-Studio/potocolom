"""One billing account, bound by an administrator to one named account.

The binding is the only thing that says whose usage that account may read.
A role change that makes either side ineligible deletes it in the same
transaction. Nothing here reaches the private billing service.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import audit, db, sessions
from app.account_lock import hold_the_account
from app.admin import _seen
from app.auth import current_principal, require, require_accounts_mode
from app.capabilities import Capability
from app.tables import BillingBinding, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

SUBJECT_ROLES = frozenset({"user", "admin"})
OWN_ACCOUNT = "an administrator cannot bind their own account"


class BillingSubject(BaseModel):
    subject_user_id: uuid.UUID


def _body(row: BillingBinding) -> dict:
    return {
        "delegate_user_id": str(row.delegate_user_id),
        "subject_user_id": str(row.subject_user_id),
        "granted_by": None if row.granted_by is None else str(row.granted_by),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _hold(session: AsyncSession, *user_ids: uuid.UUID) -> None:
    for user_id in sorted(set(user_ids)):
        await hold_the_account(session, user_id)


async def drop_invalid_bindings(session: AsyncSession, user_id: uuid.UUID,
                                previous: str, new: str) -> None:
    """Drop bindings this role change makes untrue.

    The caller holds the account gate and this transaction. Any role change
    drops the outgoing binding, so one that slipped past a racing change can
    never come back with a later return to billing. Becoming a viewer or a
    billing account drops an incoming one, because neither role can be the
    named subject.
    """
    if previous == new:
        return
    await session.execute(
        delete(BillingBinding).where(BillingBinding.delegate_user_id == user_id))
    if new in ("billing", "viewer"):
        await session.execute(
            delete(BillingBinding).where(BillingBinding.subject_user_id == user_id))


def _recent(principal: sessions.Resolved) -> None:
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")


@router.get("/api/v1/users/{delegate_id}/billing-subject")
async def read_billing_subject(
    delegate_id: uuid.UUID,
    actor: User = Depends(require(Capability.ADMINISTER)),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    target = await session.get(User, delegate_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    # The same targeted-read hook as opening the account itself. The
    # capability check cannot name which account this read reached.
    await _seen(actor, delegate_id)
    row = await session.get(BillingBinding, delegate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _body(row)


@router.put("/api/v1/users/{delegate_id}/billing-subject")
async def grant_billing_subject(
    delegate_id: uuid.UUID,
    body: BillingSubject,
    actor: User = Depends(require(Capability.ADMINISTER)),
    principal: sessions.Resolved = Depends(current_principal),
) -> dict:
    _recent(principal)
    if actor.id in (delegate_id, body.subject_user_id):
        raise HTTPException(status_code=403, detail=OWN_ACCOUNT)
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    action = None
    async with db.session_factory() as session:
        async with session.begin():
            # Both accounts, in uuid order, before the role check is trusted.
            # A role change of either side waits here, and this waits for it.
            await _hold(session, delegate_id, body.subject_user_id)
            delegate = await session.get(User, delegate_id)
            subject = await session.get(User, body.subject_user_id)
            if delegate is None or subject is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if delegate.role != "billing":
                raise HTTPException(status_code=409, detail="that account is not a billing account")
            if delegate.id == subject.id or subject.role not in SUBJECT_ROLES:
                raise HTTPException(status_code=409,
                                    detail="that account cannot be a billing subject")
            taken = (await session.execute(
                select(BillingBinding.delegate_user_id).where(
                    BillingBinding.subject_user_id == subject.id,
                    BillingBinding.delegate_user_id != delegate.id,
                )
            )).scalar_one_or_none()
            if taken is not None:
                raise HTTPException(status_code=409,
                                    detail="that account already has a billing account")
            existing = await session.get(BillingBinding, delegate.id, with_for_update=True)
            now = datetime.now(timezone.utc)
            if existing is None:
                existing = BillingBinding(
                    delegate_user_id=delegate.id,
                    subject_user_id=subject.id,
                    granted_by=actor.id,
                    created_at=now,
                )
                session.add(existing)
                action = "billing.granted"
            elif existing.subject_user_id != subject.id:
                if existing.subject_user_id == actor.id:
                    # The same rule as DELETE: an administrator does not end
                    # the binding that names their own account.
                    raise HTTPException(status_code=403, detail=OWN_ACCOUNT)
                existing.subject_user_id = subject.id
                existing.granted_by = actor.id
                existing.created_at = now
                action = "billing.replaced"
            stored = _body(existing)
    if action is not None:
        await audit.record(action, actor=actor, target_user_id=delegate_id,
                           object_ids=[str(body.subject_user_id)], severity="high")
    return stored


@router.delete("/api/v1/users/{delegate_id}/billing-subject", status_code=204)
async def revoke_billing_subject(
    delegate_id: uuid.UUID,
    actor: User = Depends(require(Capability.ADMINISTER)),
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    _recent(principal)
    if actor.id == delegate_id:
        raise HTTPException(status_code=403, detail=OWN_ACCOUNT)
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            await _hold(session, delegate_id)
            row = await session.get(BillingBinding, delegate_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if actor.id == row.subject_user_id:
                raise HTTPException(status_code=403, detail=OWN_ACCOUNT)
            subject_id = row.subject_user_id
            await session.delete(row)
    await audit.record("billing.revoked", actor=actor, target_user_id=delegate_id,
                       object_ids=[str(subject_id)], severity="high")
    return Response(status_code=204)
