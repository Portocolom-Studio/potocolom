"""Who may change whose role, and what evidence a promotion needs."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from starlette.responses import Response

from app import audit, db, sessions
from app.auth import current_principal, require_accounts_mode, require_role
from app.tables import User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

ROLE_CHANGED = "user.role"


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
            # No short circuit when the role already matches: the revocation
            # below happens after this commits, so an operator retrying a call
            # that failed halfway must still reach it.
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
    # The old session carries the old role's authority and its session shape,
    # so it does not survive the change.
    await sessions.revoke_all(user_id)
    await audit.record(ROLE_CHANGED, actor=actor, target_user_id=user_id,
                       object_ids=[change.role])
    return Response(status_code=204)
