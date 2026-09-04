"""Getting back in after a lost password.

Two ways back, deliberately different. Someone who is not an administrator is
emailed a one-use link. An administrator never is: a credential that can be
recovered from a mailbox is only as strong as that mailbox, so their way back
is an offline command run at the machine.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app import db, mail, sessions
from app.account_lock import hold_the_account
from app.auth import require_accounts_mode
from app.passwords import PasswordRejected, hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, AuthToken, Session, User

RESET_TTL = timedelta(minutes=30)
ADMIN_RECOVERY_TTL = timedelta(minutes=10)
RESET_PURPOSES = ("reset", "recovery")
# One answer for unknown, expired and spent alike, so the link cannot be asked
# which of those it was.
INVALID_LINK = "invalid or expired reset link"
ACCEPTED = {"detail": "if that address has an account, a reset link is on its way"}

router = APIRouter(dependencies=[Depends(require_accounts_mode)])


class NoSuchAdministrator(Exception):
    pass


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _link(page: str, token: str) -> str:
    """The token is the fragment, so it is never sent to the server in a
    Referer header and never reaches a server log."""
    return f"{get_settings().public_url.rstrip('/')}/{page}#{token}"


def _mint(session: AsyncSession, user_id: uuid.UUID, purpose: str, ttl: timedelta) -> str:
    """Returns the only copy of the token. Nothing durable holds it, only its
    SHA-256, so the plaintext lives in the mail or the operator's terminal and
    nowhere else."""
    token = secrets.token_urlsafe(32)
    session.add(AuthToken(user_id=user_id, purpose=purpose, token_hash=_token_hash(token),
                          expires_at=datetime.now(timezone.utc) + ttl))
    return token


async def _active_account(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(
        select(User).where(
            func.lower(func.btrim(User.email)) == email.strip().lower(),
            User.state == "active",
        )
    )).scalar_one_or_none()


async def mint_reset(email: str) -> str:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            user = await _active_account(session, email)
            if user is None:
                raise LookupError(f"no active account holds {email}")
            await hold_the_account(session, user.id)
            return _mint(session, user.id, "reset", RESET_TTL)


async def mint_admin_recovery(email: str) -> str:
    """The offline way back, minted at the machine and printed to a terminal."""
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            user = await _active_account(session, email)
            if user is None or user.role != "admin":
                raise NoSuchAdministrator(email)
            await hold_the_account(session, user.id)
            token = _mint(session, user.id, "recovery", ADMIN_RECOVERY_TTL)
    return _link("recover", token)


class ResetRequest(BaseModel):
    email: str


@router.post("/api/v1/auth/reset", status_code=202)
async def ask(request: ResetRequest) -> dict:
    """The same answer whoever asked, because a different one for an address
    nobody holds turns this route into a way to enumerate accounts.

    An administrator is answered alike and sent nothing at all. The token and
    its delivery share one transaction, so they become durable together or
    neither does.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            user = await _active_account(session, request.email)
            if user is not None and user.role != "admin":
                token = _mint(session, user.id, "reset", RESET_TTL)
                await mail.queue(session, user.email, "reset", {"link": _link("reset", token)})
    return ACCEPTED


class CompleteRequest(BaseModel):
    token: str
    password: str


@router.post("/api/v1/auth/reset/complete", status_code=204)
async def complete(request: CompleteRequest) -> Response:
    """Consumes the link, replaces the password, and ends every session the
    account held, because whoever forced the reset may be the one holding a
    stolen session.

    Argon2id is paid only behind a valid link, and raising after the
    consumption rolls it back, so a rejected password leaves the link usable.
    Nothing comes back: no session cookie and no recent-authentication grant,
    because the link proved a capability and not a person, so a reset returns
    the account holder to the login screen.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            found = (await session.execute(
                select(AuthToken.user_id).where(
                    AuthToken.token_hash == _token_hash(request.token),
                    AuthToken.purpose.in_(RESET_PURPOSES),
                    AuthToken.consumed_at.is_(None),
                    AuthToken.expires_at > func.now(),
                )
            )).first()
            if found is None or found.user_id is None:
                raise HTTPException(status_code=403, detail=INVALID_LINK)
            await hold_the_account(session, found.user_id)
            spent = (await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token_hash == _token_hash(request.token),
                    AuthToken.purpose.in_(RESET_PURPOSES),
                    AuthToken.consumed_at.is_(None),
                    AuthToken.expires_at > func.now(),
                )
                .values(consumed_at=func.now())
                .returning(AuthToken.user_id)
            )).first()
            if spent is None or spent.user_id is None:
                raise HTTPException(status_code=403, detail=INVALID_LINK)
            try:
                password_hash = await to_thread.run_sync(hash_password, request.password)
            except PasswordRejected as rejected:
                raise HTTPException(status_code=400,
                                    detail="password does not meet the policy") from rejected
            user_id: uuid.UUID = spent.user_id
            replaced = (await session.execute(
                update(AuthIdentity)
                .where(AuthIdentity.user_id == user_id, AuthIdentity.provider == "password")
                .values(password_hash=password_hash)
                .returning(AuthIdentity.id)
            )).first()
            if replaced is None:
                address = (await session.execute(
                    select(User.email).where(User.id == user_id)
                )).scalar_one()
                session.add(AuthIdentity(user_id=user_id, provider="password",
                                         subject=address.strip().lower(),
                                         password_hash=password_hash))
            # In the same transaction as the new password. Done afterwards, a
            # failure here leaves the token spent, the password changed and
            # every stolen session still live, which is the case this route
            # exists to end.
            await session.execute(
                update(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
    # After the commit, and unconditionally: a revoked row stops the next
    # request, but a socket bound its principal at the handshake and keeps
    # drawing on the account this reset exists to take back.
    await sessions.close_sockets(user_id)
    return Response(status_code=204)


async def _recover(email: str) -> str:
    if not await db.connect(serving=False):
        raise RuntimeError("could not reach PostgreSQL; is the database up?")
    try:
        return await mint_admin_recovery(email)
    finally:
        await db.dispose()


def main() -> None:
    import asyncio
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.recovery <administrator email>")
    address = sys.argv[1]
    try:
        link = asyncio.run(_recover(address))
    except NoSuchAdministrator:
        raise SystemExit(f"no active administrator holds {address}") from None
    print()
    print("An administrator is never emailed a way back in. A credential that")
    print("can be recovered from a mailbox is only as strong as that mailbox,")
    print("so this link is the way back, and it was printed here only.")
    print()
    print(f"  {link}")
    print()
    print("It is good for ten minutes and for one use. Open it and set a new")
    print("password. Every session that account held ends with the reset, and")
    print("the new password has to be signed in with.")
    print()
    print("Hand it over whole: the part after the # is the capability, and it")
    print("never leaves the browser. Nothing durable holds it, only its hash.")


if __name__ == "__main__":
    main()
