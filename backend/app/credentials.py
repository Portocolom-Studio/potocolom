"""Changing how an account is proved: its password, its address, its providers.

Every route here needs recent authentication, and every change ends the
account's other sessions and rotates the token of the one making it, because
the usual reason to change a credential is that somebody else holds the old
one, and a stolen session is a copy of this browser's cookie.
"""


from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from app import db, sessions
from app.account_lock import hold_the_account
from app.accounts import issue_session
from app.auth import current_principal, require_accounts_mode
from app.enable import _checked_email
from app.passwords import PasswordRejected, hash_password, verify_password
from app.tables import AuthIdentity, User

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
            await hold_the_account(session, principal.user.id)
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
                # Read here, not from the principal loaded before this
                # transaction: an address change committing in between would
                # leave the identity on an address the account no longer holds,
                # and login matches on the identity.
                address = (await session.execute(
                    select(User.email).where(User.id == principal.user.id).with_for_update()
                )).scalar_one()
                session.add(AuthIdentity(user_id=principal.user.id, provider="password",
                                         subject=address.strip().lower(),
                                         password_hash=password_hash))
                try:
                    await session.flush()
                except IntegrityError as taken:
                    # Two requests can both read no password identity. The one
                    # that loses the unique index is a conflict, not a fault of
                    # this install, and a 500 would say otherwise.
                    raise HTTPException(
                        status_code=409, detail="this account already has a password",
                    ) from taken
            else:
                await session.execute(
                    update(AuthIdentity).where(AuthIdentity.id == existing.id)
                    .values(password_hash=password_hash))
            issued = await sessions.rotate_and_revoke_others(session, principal)
    await sessions.close_other_sockets(principal)
    response = Response(status_code=204)
    issue_session(response, issued)
    return response


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
                await hold_the_account(session, principal.user.id)
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
                issued = await sessions.rotate_and_revoke_others(session, principal)
    except IntegrityError as clash:
        raise HTTPException(status_code=409, detail=ADDRESS_TAKEN) from clash
    await sessions.close_other_sockets(principal)
    response = Response(status_code=204)
    issue_session(response, issued)
    return response


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
            await hold_the_account(session, principal.user.id)
            # Taken, not just read: two unlinks at once each saw two
            # credentials, each removed a different one, and the account was
            # left with none, which is what this guard exists to prevent.
            held = (await session.execute(
                select(AuthIdentity.id, AuthIdentity.provider)
                .where(AuthIdentity.user_id == principal.user.id)
                .with_for_update()
            )).all()
            linked = next((row for row in held if row.provider == provider), None)
            if linked is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if len(held) == 1:
                raise HTTPException(status_code=409, detail="that is the only way in")
            await session.execute(delete(AuthIdentity).where(AuthIdentity.id == linked.id))
            issued = await sessions.rotate_and_revoke_others(session, principal)
    await sessions.close_other_sockets(principal)
    response = Response(status_code=204)
    issue_session(response, issued)
    return response
