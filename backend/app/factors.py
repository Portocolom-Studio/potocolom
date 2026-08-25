"""The second factor: enrolling one, and the gate a primary login passes through.

TOTP is optional for every role. It gates nothing but sign-in: not setup, not
invitation acceptance, not promotion, not recovery, and it changes neither what
an account may do nor how long its session lasts.
"""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from starlette.responses import JSONResponse, RedirectResponse, Response

from app import db, keyring, sessions, totp
from app.auth import CANNOT_SIGN_IN, current_principal, require_accounts_mode
from app.settings import Settings, get_settings
from app.tables import AuthFactor, AuthToken, RecoveryCode, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

TOTP_PURPOSE = "totp-factors"
# Its own purpose, so an enrolment nobody confirmed can never be presented as
# a stored factor, nor a stored factor as an enrolment.
ENROLMENT_PURPOSE = "totp-enrolment"
ENROLMENT_TTL = timedelta(minutes=30)
CHALLENGE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 10
CHALLENGE_COOKIE = "potocolom_challenge"
# One answer for a wrong code, a spent challenge, an exhausted one, an expired
# one and a challenge from another browser, so none of them is an oracle.
REFUSED = HTTPException(status_code=403, detail="that code is not valid")


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _sealed_enrolment(user_id: uuid.UUID, secret: str, codes: list[str]) -> str:
    """The pending enrolment, held by the browser setting it up rather than by
    this database, so an abandoned one leaves nothing behind to clean up or to
    weaken the factor the account already has.

    Encrypted under this installation's key ring and bound to the account, so
    it is no more use to anyone else than the secret printed beside it.
    """
    sealed = keyring.get_key_ring().encrypt(
        ENROLMENT_PURPOSE,
        json.dumps({"secret": secret, "codes": codes,
                    "expires": int(_now().timestamp() + ENROLMENT_TTL.total_seconds())}).encode(),
        user_id.bytes,
    )
    return base64.urlsafe_b64encode(sealed).decode()


def _opened_enrolment(user_id: uuid.UUID, sealed: str) -> tuple[str, list[str]]:
    try:
        opened = json.loads(keyring.get_key_ring().decrypt(
            ENROLMENT_PURPOSE, base64.urlsafe_b64decode(sealed), user_id.bytes))
        secret, codes, expires = opened["secret"], opened["codes"], opened["expires"]
    except Exception as unreadable:
        raise REFUSED from unreadable
    if expires <= _now().timestamp():
        raise REFUSED
    return secret, codes


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


def _budget_lock(user_id: uuid.UUID) -> int:
    """A per-account advisory key, the way enable.py takes SETUP_LOCK."""
    return int.from_bytes(user_id.bytes[:8], "big", signed=True)


async def begin_challenge(user: User, remember_me: bool,
                          redirect_to: str | None = None) -> Response:
    """The pre-session capability a primary login hands back instead of a
    session. It carries no authority of its own and authorizes nothing."""
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    token = secrets.token_urlsafe(32)
    async with db.session_factory() as session:
        async with session.begin():
            # Serialised per account, because the carry-forward below is a read
            # and then a write. Two logins that overlap both found nothing
            # outstanding and both seeded a fresh budget, so ten guesses became
            # ten guesses per login and a six-digit code had unlimited tries
            # against it (issue #421). A collision with another key here costs
            # two accounts a moment of waiting and nothing else.
            # A waiter holds its pooled connection, and the pool is fifteen
            # deep, so an unbounded wait would let a flood of logins for one
            # account starve every other request. Giving up is safe here:
            # nothing has been written yet, and the caller is told to come
            # back rather than handed a challenge that skipped the carry.
            await session.execute(text("SET LOCAL lock_timeout = '3s'"))
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                      {"key": _budget_lock(user.id)})
            except OperationalError as busy:
                raise HTTPException(status_code=503,
                                    detail="too many sign-ins at once") from busy
            # The budget belongs to the account, not to one challenge. Minted
            # per challenge it counts nothing, because starting another is
            # free to anyone who already has the password.
            spent = (await session.execute(
                update(AuthToken)
                .where(AuthToken.user_id == user.id, AuthToken.purpose == "challenge",
                       AuthToken.consumed_at.is_(None), AuthToken.expires_at > func.now())
                .values(consumed_at=func.now())
                .returning(AuthToken.attempts)
            )).scalars().all()
            session.add(AuthToken(user_id=user.id, purpose="challenge",
                                  token_hash=_hash(token), attempts=sum(spent),
                                  expires_at=_now() + CHALLENGE_TTL))
    settings = get_settings()
    response: Response = (RedirectResponse(redirect_to, status_code=307)
                          if redirect_to else JSONResponse({"totp_required": True},
                                                           status_code=200))
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


class ConfirmRequest(BaseModel):
    enrolment: str
    code: str
    # Only when there is a factor to replace, and then it is required: a code
    # from the authenticator being retired, or one of its recovery codes.
    current_code: str | None = None


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
        # The session is contingent on winning this. The attempt was counted
        # and committed before the code was checked, so a second request
        # carrying the same challenge reaches here too, and only the one that
        # actually spends the token is let in (issue #421).
        spent = (await session.execute(
            update(AuthToken)
            .where(AuthToken.id == claimed.id, AuthToken.consumed_at.is_(None))
            .values(consumed_at=func.now())
            .returning(AuthToken.id)
        )).first()
        await session.commit()
    if spent is None:
        raise REFUSED
    remembered = request.cookies.get(f"{_cookie_name(settings)}_remember") == "1"
    response = Response(status_code=204)
    from app.accounts import issue_session

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
    step = totp.matched_step(secret, code)
    if step is None:
        return False
    return await _claim_step(factor.id, step)


async def _claim_step(factor_id: uuid.UUID, step: int) -> bool:
    """A code is good once, which RFC 6238 requires and the drift window makes
    necessary: without this one stays live for ninety seconds, long enough for
    whoever phished it to spend it."""
    if db.session_factory is None:
        return False
    async with db.session_factory() as session:
        claimed = (await session.execute(
            update(AuthFactor)
            .where(AuthFactor.id == factor_id,
                   (AuthFactor.last_step.is_(None)) | (AuthFactor.last_step < step))
            .values(last_step=step)
            .returning(AuthFactor.id)
        )).first()
        await session.commit()
    return claimed is not None


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
    """Returns the only copy of the secret and the recovery codes.

    Nothing is written here. An enrolment that is started and abandoned must
    leave the account exactly as it was, and an account replacing its
    authenticator keeps the working one until the new one answers. Writing
    first made this endpoint the cheapest way to turn the second factor off:
    one request from a stolen session, no code, and no notice to anybody.
    """
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")
    if principal.user.state != "active":
        raise HTTPException(status_code=403, detail="account suspended")
    secret = totp.new_secret()
    codes = totp.new_recovery_codes()
    return {
        "secret": secret,
        "uri": totp.enrolment_uri(secret, account=principal.user.email, issuer="potocolom"),
        "recovery_codes": codes,
        "enrolment": _sealed_enrolment(principal.user.id, secret, codes),
    }


@router.post("/api/v1/account/totp/confirm", status_code=204)
async def confirm_enrolment(
    body: ConfirmRequest,
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    """A code proves the authenticator really holds the secret.

    Without it an operator can lock themselves out with a mistyped setup, and
    the whole enrolment lands here: the factor and its codes replace what the
    account had in one transaction, or the account keeps what it had.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    user_id = principal.user.id
    secret, codes = _opened_enrolment(user_id, body.enrolment)
    step = totp.matched_step(secret, body.code)
    if step is None:
        raise REFUSED

    # Replacing a factor costs the factor being replaced. Proving the new
    # secret only proves the caller controls the new authenticator, which a
    # stolen session does by definition: it asked for the secret. Without
    # this, two requests from a session with recent authentication replaced
    # somebody's second factor with one the caller held, and the account
    # found out when its own authenticator stopped working (issue #427).
    async with db.session_factory() as session:
        replacing = await enrolled_factor(session, user_id)
    if replacing is not None:
        if body.current_code is None:
            raise REFUSED
        if not await _accepted(body.current_code, principal.user, replacing):
            raise REFUSED

    ring = keyring.get_key_ring()
    async with db.session_factory() as session:
        async with session.begin():
            # Replacing an enrolment replaces its codes with it: a code minted
            # for a secret nobody holds any more is a way in nobody expects.
            await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
            retired = delete(AuthFactor).where(AuthFactor.user_id == user_id)
            if replacing is None:
                await session.execute(retired)
            else:
                # Bound to the factor that was proved, and the request is only
                # allowed to continue if it is the one that removed it. Two
                # replacements racing would otherwise each delete a factor that
                # was already gone and each insert their own, leaving the
                # account with two and no way to say which one gates a login.
                gone = (await session.execute(
                    retired.where(AuthFactor.id == replacing.id).returning(AuthFactor.id)
                )).first()
                if gone is None:
                    raise REFUSED
            session.add(AuthFactor(
                user_id=user_id, kind="totp",
                secret_ciphertext=ring.encrypt(TOTP_PURPOSE, secret.encode(), user_id.bytes),
                key_version=ring.active_version,
                confirmed_at=func.now(),
                # The confirming code is spent, like every other one: it is
                # live for another ninety seconds otherwise.
                last_step=step,
            ))
            for code in codes:
                session.add(RecoveryCode(user_id=user_id, code_hash=_hash(code)))
    return Response(status_code=204)
