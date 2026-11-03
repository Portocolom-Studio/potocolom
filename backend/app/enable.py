"""Deciding how the installation works, and the one-use link that claims it.

A new install starts unclaimed. Whoever spends the setup link chooses: an
administrator account (the claim) or no accounts at all. A none install can
turn accounts on later from its own administrator (the runtime enable).

The claim adopts the implicit local user rather than creating an account
beside it: the implicit user owns every job and asset made before accounts
existed, so a new row would strand all of it behind an account nobody holds.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from anyio import to_thread
from sqlalchemy import delete, func, select, text, update
from starlette.responses import JSONResponse, Response

from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, auth_mode, db, sessions
from app.accounts import issue_session
from app.auth import check_origin, require_none_mode, require_role
from app.keyring import KeyRingError
from app.manifests import StorableStr
from app.passwords import PasswordRejected, hash_password
from app.settings import get_settings
from app.tables import AuthIdentity, AuthToken, InstallationAuthState, User

SETUP_TTL = timedelta(hours=1)
SETUP_PURPOSE = "setup"
# One key for claiming this installation, shared with the offline reclaim.
SETUP_LOCK = 184469
# One answer for unknown, expired, spent and replaced, so the link is not an
# oracle for which of those it was.
INVALID_LINK = "invalid or expired setup link"
MAX_EMAIL_LENGTH = 320
OVERRIDE_REFUSAL = (
    "AUTH_MODE=none is set in the environment, so the next start would refuse accounts. "
    "Remove AUTH_MODE from deploy/compose/.env and restart the API, then turn accounts on."
)

router = APIRouter()


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class AlreadyClaimed(Exception):
    """Somebody holds an identity, so a setup link would only be refused."""


async def mint_setup_token(session: AsyncSession | None = None) -> str:
    """Returns the only copy of the link. Nothing durable holds it.

    Takes a session when the caller has one open, so a reclaim can decide and
    mint in one transaction. Either way the work happens behind SETUP_LOCK,
    which claim() holds too: a link minted for an installation somebody
    claimed a moment ago is refused when it is finally spent, which is the
    worst moment to discover it.
    """
    token = secrets.token_urlsafe(32)
    if session is not None:
        await _mint_locked(session, token)
        return token
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as owned:
        async with owned.begin():
            await _mint_locked(owned, token)
    return token


async def _mint_locked(session: AsyncSession, token: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETUP_LOCK})
    # Under the lock, not before it: the setup link adopts the implicit local
    # user, which claim() allows only while nobody holds an identity.
    claimed = (await session.execute(select(AuthIdentity.id).limit(1))).first()
    if claimed is not None:
        raise AlreadyClaimed(
            "this installation has already been claimed, so a setup link would be "
            "refused; use reclaim --restore EMAIL to make an account an administrator")
    decided = (await session.execute(
        select(InstallationAuthState.auth_mode).where(InstallationAuthState.id == 1)
    )).scalar_one_or_none()
    if decided == auth_mode.NONE:
        raise AlreadyClaimed(
            "this installation runs without accounts, so a setup link would be refused; "
            "turn accounts on first with make auth-enable")
    # Minting replaces: an operator who runs this twice must not leave an
    # older link alive for whoever saw it first.
    await session.execute(delete(AuthToken).where(
        AuthToken.purpose == SETUP_PURPOSE,
        AuthToken.consumed_at.is_(None),
    ))
    session.add(AuthToken(
        purpose=SETUP_PURPOSE,
        token_hash=_token_hash(token),
        expires_at=datetime.now(timezone.utc) + SETUP_TTL,
    ))


# It becomes the To header of mail this install sends, and smtplib takes the
# envelope recipients from that header, so a second address there is this
# install addressing a stranger from its own name.
_NOT_IN_AN_ADDRESS = set(',;<>"\\ \t\r\n')


def _checked_email(email: str) -> str:
    address = email.strip()
    local, _, domain = address.partition("@")
    if (address.count("@") != 1 or not local or not domain
            or len(address) > MAX_EMAIL_LENGTH
            or _NOT_IN_AN_ADDRESS & set(address)):
        raise HTTPException(status_code=400, detail="invalid email address")
    return address


async def _spend(session: AsyncSession, token: str) -> None:
    """Consumes the link, or raises the one answer every refusal gets.

    Raising after this rolls the consumption back with the caller's transaction.
    """
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


async def _adopt(session: AsyncSession, address: str, password: str) -> uuid.UUID:
    """Makes the implicit administrator this account, with a password identity."""
    claimed = (await session.execute(select(AuthIdentity.id).limit(1))).first()
    if claimed is not None:
        raise HTTPException(status_code=409, detail="installation already claimed")
    # Argon2id costs 19 MiB and about 13 ms. Paying it only after a valid link
    # is what keeps the setup route from being an anonymous grinder, and raising
    # here rolls the consumption back, so a rejected password leaves the
    # operator a link to retry with.
    try:
        password_hash = await to_thread.run_sync(hash_password, password)
    except PasswordRejected as rejected:
        raise HTTPException(status_code=400,
                            detail="password does not meet the policy") from rejected
    # Resolved here rather than from the process global: after the claim above
    # there is no implicit row, and a lookup outside this transaction would
    # answer differently for a caller with no link.
    user = (await session.execute(
        select(User).where(User.email == db.LOCAL_USER_EMAIL)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    adopted = user.id
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
    return adopted


async def claim(token: str, email: str, password: str) -> uuid.UUID:
    """Consumes the link, adopts the implicit account and records accounts, or raises."""
    address = _checked_email(email)
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            # One claim at a time, and one reclaim at a time beside it: the
            # offline command checks that nobody holds an identity before it
            # mints a link, and without this that check and this insert can
            # both believe they went first.
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": SETUP_LOCK})
            # The link is judged before anything else, so a caller without one
            # cannot learn whether the installation is claimed.
            await _spend(session, token)
            # A stale view that still says unclaimed must not turn a none install
            # into accounts with a link it happens to hold.
            decided = (await session.execute(
                select(InstallationAuthState.auth_mode).where(InstallationAuthState.id == 1)
            )).scalar_one_or_none()
            if decided == auth_mode.NONE:
                raise HTTPException(status_code=409, detail="installation already decided")
            adopted = await _adopt(session, address, password)
            await db.record_mode(session, auth_mode.ACCOUNTS)
    return adopted


async def decline_accounts(token: str) -> None:
    """Consumes the link on the choice to run without accounts.

    Refused once the install is decided as accounts, even when this process
    still believes it is unclaimed: a stale view must not switch a live
    accounts install back to none.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": SETUP_LOCK})
            await _spend(session, token)
            decided = (await session.execute(
                select(InstallationAuthState.auth_mode).where(InstallationAuthState.id == 1)
            )).scalar_one_or_none()
            claimed = (await session.execute(select(AuthIdentity.id).limit(1))).first()
            if claimed is not None or decided not in {None, auth_mode.UNCLAIMED, auth_mode.NONE}:
                raise HTTPException(status_code=409, detail="installation already decided")
            await db.record_mode(session, auth_mode.NONE)


async def turn_accounts_on(email: str, password: str) -> uuid.UUID:
    """The runtime enable: the implicit administrator names an account, no link needed.

    The recorded mode is read under the lock. make auth-enable may have moved the
    database to accounts from another process while this one still serves none,
    and the implicit administrator must not be adopted then without its link.
    """
    address = _checked_email(email)
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": SETUP_LOCK})
            decided = (await session.execute(
                select(InstallationAuthState.auth_mode).where(InstallationAuthState.id == 1)
            )).scalar_one_or_none()
            if decided != auth_mode.NONE:
                raise HTTPException(status_code=409, detail="installation already decided")
            adopted = await _adopt(session, address, password)
            await db.record_mode(session, auth_mode.ACCOUNTS)
    return adopted


async def require_setup_mode() -> None:
    """The setup route answers an unclaimed install, and an accounts one nobody has claimed."""
    if auth_mode.current() not in {auth_mode.UNCLAIMED, auth_mode.ACCOUNTS}:
        raise HTTPException(status_code=404, detail="Not Found")


def _missing_root_keys() -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "detail": "root_keys_missing",
        "message": "Run the install script or make setup-link to write ROOT_KEYS.",
    })


class SetupRequest(BaseModel):
    token: StorableStr
    # none records the choice and returns no session: there is nobody to sign in.
    mode: Literal["accounts", "none"] = "accounts"
    email: StorableStr = ""
    password: StorableStr = ""


@router.post("/api/v1/auth/setup", status_code=204,
             dependencies=[Depends(require_setup_mode)])
async def setup(request: SetupRequest) -> Response:
    if request.mode == "none":
        await decline_accounts(request.token)
        auth_mode.load(auth_mode.NONE)
        await audit.record("POST /api/v1/auth/setup")
        return Response(status_code=204)
    try:
        adopted = await claim(request.token, request.email, request.password)
    except KeyRingError:
        if get_settings().root_keys:
            raise
        return _missing_root_keys()
    auth_mode.load(auth_mode.ACCOUNTS)
    await audit.record("POST /api/v1/auth/setup", target_user_id=adopted)
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        owner = await session.get(User, adopted)
    if owner is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    # A clean session, and deliberately no recent-authentication grant: the
    # link proved a capability, not a person, so it must not open the window
    # that guards credential changes.
    response = Response(status_code=204)
    issue_session(response, await sessions.mint(owner, remember_me=False))
    return response


class EnableRequest(BaseModel):
    email: StorableStr
    password: StorableStr


@router.post("/api/v1/auth/enable", status_code=204,
             dependencies=[Depends(require_none_mode)])
async def enable_accounts(
    request: EnableRequest,
    http_request: Request,
    user: User = Depends(require_role("admin")),
) -> Response:
    """An administrator of a none install turns accounts on, with no restart.

    The caller is the implicit administrator, so the account they name takes
    that row over exactly as the setup claim does. The answer carries their
    session, and every socket opened without one is closed.
    """
    check_origin(http_request)
    if get_settings().auth_mode is not None:
        # The override is read again at the next start, where it would refuse the
        # accounts this switch records. Refusing here leaves nothing to undo.
        raise HTTPException(status_code=409, detail=OVERRIDE_REFUSAL)
    try:
        adopted = await turn_accounts_on(request.email, request.password)
    except KeyRingError:
        if get_settings().root_keys:
            raise
        return _missing_root_keys()
    auth_mode.load(auth_mode.ACCOUNTS)
    # Imported here: the realtime module imports this one's neighbours, and
    # only the route needs it.
    from app import realtime
    await realtime.close_none_mode_sockets()
    await audit.record("POST /api/v1/auth/enable", target_user_id=adopted)
    owner = await _owner(adopted)
    response = Response(status_code=204)
    issue_session(response, await sessions.mint(owner, remember_me=False))
    return response


async def _owner(user_id: uuid.UUID) -> User:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        owner = await session.get(User, user_id)
    if owner is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return owner


async def _ensure_claimable(factory) -> None:
    """The claim adopts the implicit user, so an install that never ran in
    none mode needs one before it can be claimed at all.

    Only while the install is unclaimed. Afterwards the row has been renamed
    to the owner, and recreating it by address would stand up a second
    administrator nobody claimed.
    """
    async with factory() as session:
        claimed = (await session.execute(select(AuthIdentity.id).limit(1))).first()
        if claimed is not None:
            return
        implicit = (await session.execute(
            select(User.id).where(User.email == db.LOCAL_USER_EMAIL)
        )).first()
        if implicit is None:
            session.add(User(email=db.LOCAL_USER_EMAIL, role="admin"))
            await session.commit()


async def _enable() -> str:
    # serving=False: this tool records the mode an installation chooses, so it
    # must not be refused by the guard that stops a serving API starting in a
    # mode nobody chose. Otherwise a second setup link could never be minted.
    if not await db.connect(serving=False):
        raise RuntimeError("could not reach PostgreSQL; is the database up?")
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    try:
        await _ensure_claimable(db.session_factory)
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
    print()
    print("The link is one use and nothing durable holds it, only its hash.")
    print("Running this again replaces it. After you claim it, sign in at")
    print("/api/v1/auth/login and invite everybody else from Admin > Invitations.")


if __name__ == "__main__":
    main()
