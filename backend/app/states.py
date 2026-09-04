"""What an account state is for: taking capability away, and giving it back.

A state is the one place that decides whether an account may sign in, change
anything, hold a GPU slot, or speak to the public through a share. Nothing here
is a job state: `cancelled` belongs to work, never to a person.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from starlette.responses import Response

from app import audit, db, jobs, sessions
from app.account_lock import hold_the_account
from app.auth import require_accounts_mode, require_role
from app.tables import AuthToken, Job, Session, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

STATE_CHANGED = "account.state"
# The same key the role change takes: both read how many administrators are
# left, and the count has to mean something for the length of the change.
STATE_CHANGE_LOCK = 184468

# Where an account may go from where it is. Purging is absent on purpose: the
# deletion sweep moves an account there, never an administrator with a form.
TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"suspended", "disabled", "deletion_pending"}),
    "suspended": frozenset({"active", "disabled", "deletion_pending"}),
    "disabled": frozenset({"active", "deletion_pending"}),
    # Restoring a deletion restores the data with it, which is R14's to do.
    "deletion_pending": frozenset(),
    "purging": frozenset(),
}


class StateChange(BaseModel):
    state: Literal["active", "suspended", "disabled", "deletion_pending"]


@router.post("/api/v1/users/{user_id}/state", status_code=204)
async def change_state(
    user_id: uuid.UUID,
    change: StateChange,
    actor: User = Depends(require_role("admin")),
) -> Response:
    """Compare and set, and idempotent: the state an account already holds is
    not a second event, and a transition that was never legal is a conflict
    rather than a silent overwrite."""
    if user_id == actor.id:
        # The same rule as roles. An administrator who can suspend themselves
        # is one mistake away from an installation with nobody in charge.
        raise HTTPException(status_code=403,
                            detail="an administrator cannot change their own state")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            await hold_the_account(session, user_id)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": STATE_CHANGE_LOCK})
            target = await session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if target.state == change.state:
                return Response(status_code=204)
            if change.state not in TRANSITIONS[target.state]:
                raise HTTPException(
                    status_code=409,
                    detail=f"an account cannot go from {target.state} to {change.state}")
            if (change.state != "active" and target.role == "admin"
                    and not await _remaining_administrators(session, user_id)):
                raise HTTPException(status_code=403,
                                    detail="the last administrator cannot be suspended")
            target.state = change.state
            if change.state != "active":
                # The mailed capabilities before the sessions, because that is
                # the order operator.collapse deletes them in and the other way
                # round deadlocks against a collapse running while the API
                # serves.
                await session.execute(
                    update(AuthToken)
                    .where(AuthToken.user_id == user_id,
                           AuthToken.purpose.in_(("reset", "recovery")),
                           AuthToken.consumed_at.is_(None))
                    .values(consumed_at=func.now())
                )
                # In the same transaction as the state: a revocation that
                # happens after the commit can fail on its own and leave a
                # session holding capability the state was changed to remove.
                await session.execute(
                    update(Session)
                    .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                    .values(revoked_at=func.now())
                )
                stopping = list((await session.execute(
                    select(Job.id).where(Job.user_id == user_id,
                                         Job.state.in_(("queued", "running")))
                )).scalars().all())
            else:
                stopping = []
    if change.state != "active":
        # After the commit, and outside it. A socket binds its principal at
        # the handshake, and a worker cancellation is a message to somebody
        # else: neither belongs inside a transaction that can still roll back.
        await sessions.close_sockets(user_id)
        for job_id in stopping:
            await jobs.cancel(job_id, reason="account suspended")
    await audit.record(STATE_CHANGED, actor=actor, target_user_id=user_id,
                       object_ids=[change.state])
    return Response(status_code=204)


async def _remaining_administrators(session, excluding: uuid.UUID) -> int:
    return (await session.execute(
        select(func.count()).select_from(User)
        .where(User.role == "admin", User.state == "active", User.id != excluding)
    )).scalar_one()
