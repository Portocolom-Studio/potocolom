"""Changing how an account is proved: its password, its address, its providers.

Every route here needs recent authentication, and every change ends the
account's other sessions, because the usual reason to change a credential is
that somebody else holds the old one.
"""

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import db, sessions
from app.auth import current_principal, require_accounts_mode
from app.enable import _checked_email
from app.passwords import PasswordRejected, hash_password, verify_password
from app.tables import AuthIdentity, Session, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

UNLINKABLE = ("google", "github")
ADDRESS_TAKEN = "that address is already in use"


async def recent_principal(
    principal: sessions.Resolved = Depends(current_principal),
) -> sessions.Resolved:
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")
    if principal.user.state != "active":
        raise HTTPException(status_code=403, detail="account suspended")
    return principal


async def _revoke_others(session: AsyncSession, principal: sessions.Resolved) -> None:
    """Everything but the browser making the change, which would otherwise sign
    somebody out of the session they are changing the credential from."""
    await session.execute(
        update(Session)
        .where(Session.user_id == principal.user.id, Session.id != principal.session.id,
               Session.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )


class PasswordChange(BaseModel):
    password: str
    current_password: str | None = None


@router.post("/api/v1/account/password", status_code=204)
async def change_password(
    body: PasswordChange,
    principal: sessions.Resolved = Depends(recent_principal),
) -> Response:
    """Recent authentication says this browser was somebody's. The current
    password says it is still theirs at this keyboard.

    An account with no password identity is adding one, and has none to give.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            existing = (await session.execute(
                select(AuthIdentity.id, AuthIdentity.password_hash)
                .where(AuthIdentity.user_id == principal.user.id,
                       AuthIdentity.provider == "password")
            )).first()
            if existing is not None and (
                body.current_password is None
                or not await to_thread.run_sync(verify_password, existing.password_hash or "",
                                                body.current_password)
            ):
                raise HTTPException(status_code=403, detail="the current password is required")
            try:
                password_hash = await to_thread.run_sync(hash_password, body.password)
            except PasswordRejected as rejected:
                raise HTTPException(status_code=400,
                                    detail="password does not meet the policy") from rejected
            if existing is None:
                session.add(AuthIdentity(user_id=principal.user.id, provider="password",
                                         subject=principal.user.email.strip().lower(),
                                         password_hash=password_hash))
            else:
                await session.execute(
                    update(AuthIdentity).where(AuthIdentity.id == existing.id)
                    .values(password_hash=password_hash))
            await _revoke_others(session, principal)
    return Response(status_code=204)


class AddressChange(BaseModel):
    email: str


@router.post("/api/v1/account/email", status_code=204)
async def change_email(
    body: AddressChange,
    principal: sessions.Resolved = Depends(recent_principal),
) -> Response:
    """The assurance goes with the old address, and the password identity comes
    with the new one: login matches on the subject, so leaving it behind would
    sign somebody in under an address they no longer hold."""
    address = _checked_email(body.email)
    normalized = address.lower()
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    try:
        async with db.session_factory() as session:
            async with session.begin():
                held = (await session.execute(
                    select(User.id).where(func.lower(func.btrim(User.email)) == normalized,
                                          User.id != principal.user.id)
                )).first()
                if held is not None:
                    raise HTTPException(status_code=409, detail=ADDRESS_TAKEN)
                await session.execute(
                    update(User).where(User.id == principal.user.id)
                    .values(email=address, mail_verified=False))
                await session.execute(
                    update(AuthIdentity)
                    .where(AuthIdentity.user_id == principal.user.id,
                           AuthIdentity.provider == "password")
                    .values(subject=normalized))
                await _revoke_others(session, principal)
    except IntegrityError as clash:
        raise HTTPException(status_code=409, detail=ADDRESS_TAKEN) from clash
    return Response(status_code=204)


@router.delete("/api/v1/account/identities/{provider}", status_code=204)
async def unlink_identity(
    provider: str,
    principal: sessions.Resolved = Depends(recent_principal),
) -> Response:
    """The last way in cannot go: an account with no credential at all can only
    be recovered offline."""
    if provider not in UNLINKABLE:
        raise HTTPException(status_code=404, detail="Not Found")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            held = (await session.execute(
                select(AuthIdentity.id, AuthIdentity.provider)
                .where(AuthIdentity.user_id == principal.user.id)
            )).all()
            linked = next((row for row in held if row.provider == provider), None)
            if linked is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if len(held) == 1:
                raise HTTPException(status_code=409, detail="that is the only way in")
            await session.execute(delete(AuthIdentity).where(AuthIdentity.id == linked.id))
            await _revoke_others(session, principal)
    return Response(status_code=204)
