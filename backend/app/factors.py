"""The second factor: enrolling one, and the gate a primary login passes through.

TOTP is optional for every role. It gates nothing but sign-in: not setup, not
invitation acceptance, not promotion, not recovery, and it changes neither what
an account may do nor how long its session lasts.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from starlette.responses import JSONResponse, Response

from app import db, keyring, sessions, totp
from app.accounts import issue_session
from app.auth import CANNOT_SIGN_IN, current_principal, require_accounts_mode
from app.settings import Settings, get_settings
from app.tables import AuthFactor, AuthToken, RecoveryCode, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

TOTP_PURPOSE = "totp-factors"
CHALLENGE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 10
CHALLENGE_COOKIE = "potocolom_challenge"
# One answer for a wrong code, a spent challenge, an exhausted one, an expired
# one and a challenge from another browser, so none of them is an oracle.
REFUSED = HTTPException(status_code=403, detail="that code is not valid")


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _cookie_name(settings: Settings) -> str:
    return f"__Host-{CHALLENGE_COOKIE}" if sessions.is_secure(settings.public_url) \
        else CHALLENGE_COOKIE


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enrolled_factor(session, user_id: uuid.UUID) -> AuthFactor | None:
    """A factor only gates sign-in once a code has proved the app has it.

    An enrolment that was started and abandoned must never lock anyone out.
    """
    return (await session.execute(
        select(AuthFactor).where(AuthFactor.user_id == user_id,
                                 AuthFactor.confirmed_at.is_not(None))
    )).scalar_one_or_none()


async def begin_challenge(user: User, remember_me: bool) -> Response:
    """The pre-session capability a primary login hands back instead of a
    session. It carries no authority of its own and authorizes nothing."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    token = secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        session.add(AuthToken(user_id=user.id, purpose="challenge",
                              token_hash=_hash(token), expires_at=_now() + CHALLENGE_TTL))
        await session.commit()
    settings = get_settings()
    response = JSONResponse({"totp_required": True}, status_code=200)
    response.set_cookie(
        _cookie_name(settings), token, path="/", samesite="lax",
        secure=sessions.is_secure(settings.public_url), httponly=True,
        max_age=int(CHALLENGE_TTL.total_seconds()),
    )
    if remember_me:
        response.set_cookie(f"{_cookie_name(settings)}_remember", "1", path="/",
                            samesite="lax", secure=sessions.is_secure(settings.public_url),
                            httponly=True, max_age=int(CHALLENGE_TTL.total_seconds()))
    return response


class CodeRequest(BaseModel):
    code: str


@router.post("/api/v1/auth/totp", status_code=204)
async def answer_challenge(body: CodeRequest, request: Request) -> Response:
    settings = get_settings()
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    presented = request.cookies.get(_cookie_name(settings))
    if not presented:
        raise REFUSED
    async with db.session_factory() as session:
        # Counting the attempt is the first thing that happens, so a guess
        # costs one whether or not anything later succeeds.
        claimed = (await session.execute(
            update(AuthToken)
            .where(AuthToken.token_hash == _hash(presented),
                   AuthToken.purpose == "challenge",
                   AuthToken.consumed_at.is_(None),
                   AuthToken.expires_at > func.now(),
                   AuthToken.attempts < MAX_ATTEMPTS)
            .values(attempts=AuthToken.attempts + 1)
            .returning(AuthToken.id, AuthToken.user_id)
        )).first()
        await session.commit()
    if claimed is None or claimed.user_id is None:
        raise REFUSED
    async with db.session_factory() as session:
        user = await session.get(User, claimed.user_id)
        factor = await enrolled_factor(session, claimed.user_id)
    if user is None or factor is None or user.state in CANNOT_SIGN_IN:
        raise REFUSED
    if not await _accepted(body.code, user, factor):
        raise REFUSED
    async with db.session_factory() as session:
        await session.execute(
            update(AuthToken).where(AuthToken.id == claimed.id).values(consumed_at=func.now()))
        await session.commit()
    remembered = request.cookies.get(f"{_cookie_name(settings)}_remember") == "1"
    response = Response(status_code=204)
    issue_session(response, await sessions.mint(user, remember_me=remembered,
                                                authenticated=True))
    _clear_challenge(response, settings)
    return response


def _clear_challenge(response: Response, settings: Settings) -> None:
    name = _cookie_name(settings)
    secure = sessions.is_secure(settings.public_url)
    response.delete_cookie(name, path="/", samesite="lax", secure=secure, httponly=True)
    response.delete_cookie(f"{name}_remember", path="/", samesite="lax", secure=secure,
                           httponly=True)


async def _accepted(code: str, user: User, factor: AuthFactor) -> bool:
    if await _spend_recovery_code(user.id, code):
        return True
    try:
        secret = keyring.get_key_ring().decrypt(
            TOTP_PURPOSE, factor.secret_ciphertext, user.id.bytes).decode()
    except Exception:
        # A secret this installation can no longer read is a factor nobody can
        # pass, which is the fail-closed direction.
        return False
    return totp.verify(secret, code)


async def _spend_recovery_code(user_id: uuid.UUID, code: str) -> bool:
    """One use, and consumed by the same statement that finds it."""
    if db.session_factory is None:
        return False
    async with db.session_factory() as session:
        spent = (await session.execute(
            update(RecoveryCode)
            .where(RecoveryCode.user_id == user_id,
                   RecoveryCode.code_hash == _hash(code.strip().lower()),
                   RecoveryCode.consumed_at.is_(None))
            .values(consumed_at=func.now())
            .returning(RecoveryCode.id)
        )).first()
        await session.commit()
    return spent is not None


@router.post("/api/v1/account/totp")
async def start_enrolment(
    principal: sessions.Resolved = Depends(current_principal),
) -> dict:
    """Returns the only copy of the secret and the recovery codes."""
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")
    if principal.user.state != "active":
        raise HTTPException(status_code=403, detail="account suspended")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    secret = totp.new_secret()
    codes = totp.new_recovery_codes()
    ring = keyring.get_key_ring()
    user_id = principal.user.id
    async with db.session_factory() as session:
        async with session.begin():
            # Replacing an enrolment replaces its codes with it: a code minted
            # for a secret nobody holds any more is a way in nobody expects.
            await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
            await session.execute(delete(AuthFactor).where(AuthFactor.user_id == user_id))
            session.add(AuthFactor(
                user_id=user_id, kind="totp",
                secret_ciphertext=ring.encrypt(TOTP_PURPOSE, secret.encode(), user_id.bytes),
                key_version=ring.active_version,
            ))
            for code in codes:
                session.add(RecoveryCode(user_id=user_id, code_hash=_hash(code)))
    return {
        "secret": secret,
        "uri": totp.enrolment_uri(secret, account=principal.user.email, issuer="potocolom"),
        "recovery_codes": codes,
    }


@router.post("/api/v1/account/totp/confirm", status_code=204)
async def confirm_enrolment(
    body: CodeRequest,
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    """A code proves the authenticator really holds the secret.

    Without it an operator can lock themselves out with a mistyped setup.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user_id = principal.user.id
    async with db.session_factory() as session:
        factor = (await session.execute(
            select(AuthFactor).where(AuthFactor.user_id == user_id)
        )).scalar_one_or_none()
    if factor is None:
        raise REFUSED
    try:
        secret = keyring.get_key_ring().decrypt(
            TOTP_PURPOSE, factor.secret_ciphertext, user_id.bytes).decode()
    except Exception as unreadable:
        raise REFUSED from unreadable
    if not totp.verify(secret, body.code):
        raise REFUSED
    async with db.session_factory() as session:
        await session.execute(
            update(AuthFactor).where(AuthFactor.id == factor.id)
            .values(confirmed_at=func.now()))
        await session.commit()
    return Response(status_code=204)
