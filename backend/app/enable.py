"""Turning accounts on, and the one-use link that claims the installation.

The claim adopts the implicit local user rather than creating an account
beside it: the implicit user owns every job and asset made before accounts
existed, so a new row would strand all of it behind an account nobody holds.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from starlette.responses import Response

from app import audit, db
from app.passwords import PasswordRejected, hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, AuthToken, User

SETUP_TTL = timedelta(hours=1)
SETUP_PURPOSE = "setup"
# One answer for unknown, expired, spent and replaced, so the link is not an
# oracle for which of those it was.
INVALID_LINK = "invalid or expired setup link"
MAX_EMAIL_LENGTH = 320

router = APIRouter()


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def mint_setup_token() -> str:
    """Returns the only copy of the link. Nothing durable holds it."""
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    token = secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        async with session.begin():
            # Minting replaces: an operator who runs this twice must not leave
            # an older link alive for whoever saw it first.
            await session.execute(delete(AuthToken).where(
                AuthToken.purpose == SETUP_PURPOSE,
                AuthToken.consumed_at.is_(None),
            ))
            session.add(AuthToken(
                purpose=SETUP_PURPOSE,
                token_hash=_token_hash(token),
                expires_at=datetime.now(timezone.utc) + SETUP_TTL,
            ))
    return token


def _checked_email(email: str) -> str:
    address = email.strip()
    if "@" not in address or not 3 <= len(address) <= MAX_EMAIL_LENGTH:
        raise HTTPException(status_code=400, detail="invalid email address")
    return address


async def claim(token: str, email: str, password: str) -> uuid.UUID:
    """Consumes the link and adopts the implicit account, or raises.

    The password is checked before the link is spent, so a rejected password
    leaves the operator a link to retry with.
    """
    address = _checked_email(email)
    try:
        password_hash = hash_password(password)
    except PasswordRejected as rejected:
        raise HTTPException(status_code=400,
                            detail="password does not meet the policy") from rejected
    if db.session_factory is None or db.local_user_id is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            # The link is judged before anything else, so a caller without one
            # cannot learn whether the installation is claimed. Raising after
            # this rolls the consumption back with it.
            spent = (await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token_hash == _token_hash(token),
                    AuthToken.purpose == SETUP_PURPOSE,
                    AuthToken.consumed_at.is_(None),
                    AuthToken.expires_at > func.now(),
                )
                .values(consumed_at=func.now())
                .returning(AuthToken.id)
            )).first()
            if spent is None:
                raise HTTPException(status_code=403, detail=INVALID_LINK)
            claimed = (await session.execute(select(AuthIdentity.id).limit(1))).first()
            if claimed is not None:
                raise HTTPException(status_code=409, detail="installation already claimed")
            user = await session.get(User, db.local_user_id)
            if user is None:
                raise HTTPException(status_code=503, detail="database unavailable")
            user.email = address
            user.role = "admin"
            user.state = "active"
            # Nobody has proved this address yet; setup proves the link.
            user.mail_verified = False
            session.add(AuthIdentity(
                user_id=user.id,
                provider="password",
                subject=address.lower(),
                password_hash=password_hash,
            ))
    return db.local_user_id


class SetupRequest(BaseModel):
    token: str
    email: str
    password: str


@router.post("/api/v1/auth/setup", status_code=204)
async def setup(request: SetupRequest) -> Response:
    adopted = await claim(request.token, request.email, request.password)
    await audit.record("POST /api/v1/auth/setup", target_user_id=adopted)
    return Response(status_code=204)


async def _enable() -> str:
    if not await db.connect():
        raise RuntimeError("could not reach PostgreSQL; is the database up?")
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    try:
        await db.enable_accounts_mode(db.session_factory)
        return await mint_setup_token()
    finally:
        await db.dispose()


def main() -> None:
    import asyncio

    token = asyncio.run(_enable())
    base = get_settings().public_url.rstrip("/")
    print()
    print("Accounts are enabled for this installation. This cannot be undone")
    print("without an offline destructive reset.")
    print()
    print("Restart the API with AUTH_MODE=accounts, then claim the")
    print("administrator account within one hour:")
    print()
    print(f"  curl -X POST {base}/api/v1/auth/setup \\")
    print("    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"token\": \"{token}\",")
    print("         \"email\": \"you@example.com\",")
    print("         \"password\": \"a password of fifteen characters or more\"}'")
    print()
    print("A password typed on a command line reaches your shell history.")
    print("The browser page for this arrives with sign-in.")
    print()
    print("The link is one use and nothing durable holds it, only its hash.")
    print("Running this again replaces it. Until sign-in ships, accounts mode")
    print("authenticates nobody except through this call.")


if __name__ == "__main__":
    main()
