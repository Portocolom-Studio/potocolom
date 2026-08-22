"""Who may change whose role, and what evidence a promotion needs."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from starlette.responses import Response

from app import audit, db, sessions
from app.auth import current_principal, require_accounts_mode, require_role
from app.tables import Session, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

ROLE_CHANGED = "user.role"
# One key for every role change, so the last-administrator count cannot be
# read by two callers at once.
ROLE_CHANGE_LOCK = 184468


class RoleChange(BaseModel):
    role: Literal["viewer", "user", "admin"]
    # A no-mail install cannot prove an address, so the administrator says on
    # the record that they know who this is.
    attested: bool = False


async def _remaining_administrators(session, excluding: uuid.UUID) -> int:
    return (await session.execute(
        select(func.count()).select_from(User)
        .where(User.role == "admin", User.state == "active", User.id != excluding)
    )).scalar_one()


@router.post("/api/v1/users/{user_id}/role", status_code=204)
async def change_role(
    user_id: uuid.UUID,
    change: RoleChange,
    actor: User = Depends(require_role("admin")),
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    if user_id == actor.id:
        # Not even to demote themselves: an administrator who can rewrite their
        # own authority is the only check standing between a mistake and an
        # installation with nobody in charge.
        raise HTTPException(status_code=403, detail="an administrator cannot change their own role")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            target = await session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not Found")
            # Two administrators demoting each other at the same moment each
            # count the other as remaining, and the install ends with none.
            # The lock is held to the end of this transaction.
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": ROLE_CHANGE_LOCK})
            if change.role == "admin":
                if not sessions.is_recent(principal.session):
                    raise HTTPException(status_code=403,
                                        detail="recent authentication required")
                if not target.mail_verified and not change.attested:
                    raise HTTPException(
                        status_code=409,
                        detail="this address is unverified; attest to promote it",
                    )
            elif (target.role == "admin" and change.role != "admin"
                  and not await _remaining_administrators(session, user_id)):
                # An install with no administrator can only be recovered
                # offline, so nothing here may produce one.
                raise HTTPException(status_code=403,
                                    detail="the last administrator cannot be demoted")
            target.role = change.role
            # In the same transaction as the change: a revocation that happens
            # after the commit can fail on its own and leave a session holding
            # the new authority in the old role's shape, remembered for thirty
            # days where an administrator may not be remembered at all.
            await session.execute(
                update(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
    # The attestation is the only evidence a no-mail install has, so the row
    # has to say whether the promotion rested on it or on a verified address.
    await audit.record(ROLE_CHANGED, actor=actor, target_user_id=user_id,
                       object_ids=[change.role, "attested"] if change.attested
                       else [change.role])
    return Response(status_code=204)
