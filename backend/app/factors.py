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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.responses import JSONResponse, RedirectResponse, Response

from app import db, keyring, sessions, totp
from app.auth import CANNOT_SIGN_IN, current_principal, require_accounts_mode
from app.settings import Settings, get_settings
from app.tables import AuthFactor, AuthToken, RecoveryCode, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

TOTP_PURPOSE = "totp-factors"
# The unique index that makes an account hold one factor, named so the race
# it refuses can be told apart from any other integrity failure.
ONE_FACTOR_PER_ACCOUNT = "auth_factors_one_per_kind"
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


async def _hold_the_account(session: AsyncSession, user_id: uuid.UUID,
                            busy_detail: str = "too many changes to this account at once",
                            ) -> None:
    """The account to itself for the rest of the caller's transaction.

    A first enrolment has no factor row, so the SELECT ... FOR UPDATE the
    other routes exclude each other with locks nothing against it, and
    operator.clear_factor is free to interleave: its delete of the factor
    finding none, and its delete of the recovery codes taking the ones an
    enrolment committed in between, leaving a factor nobody has a way back
    past. This key holds whether or not a row exists.

    Taken first, before any row lock, the way begin_challenge takes it. A lock
    every holder acquires before anything else can serialise them but can
    never be the second edge of a cycle, which is what makes adding it to
    routes already arranged against deadlock safe.

    Giving up rather than queueing: a waiter holds its pooled connection, and
    the pool is fifteen deep, so an unbounded wait would let a flood aimed at
    one account starve every other request. Nothing has been written when this
    runs, so the caller can safely be told to come back.
    """
    await session.execute(text("SET LOCAL lock_timeout = '3s'"))
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                              {"key": _budget_lock(user_id)})
    except DBAPIError as busy:
        # The wide class, deliberately: asyncpg raises LockNotAvailableError,
        # which has no DBAPI class of its own, so SQLAlchemy wraps it as a
        # plain DBAPIError and the narrower OperationalError never arrives.
        # This transaction has written nothing, so telling the caller to come
        # back is safe. An attempt a previous transaction already counted
        # stays counted, which is what a refused guess costs anyway.
        raise HTTPException(status_code=503, detail=busy_detail) from busy
    # Only this wait was meant to be bounded, and SET LOCAL lasts for the whole
    # transaction. Left on, a row lock taken further down times out into a
    # DBAPIError no handler here expects, and the caller gets a 500 in place of
    # the wait it used to do. Nothing after this line queues behind an
    # unrelated account, because the key above is already held.
    await session.execute(text("SET LOCAL lock_timeout = '0'"))


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
            # Refused rather than queued, so a flood of logins for one account
            # cannot starve the pool: the caller is told to come back rather
            # than handed a challenge that skipped the carry.
            await _hold_the_account(session, user.id, "too many sign-ins at once")
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
    proof = await _verify(body.code, user, factor)
    if proof is None:
        raise REFUSED
    async with db.session_factory() as session:
        async with session.begin():
            # The session is contingent on winning this. The attempt was
            # counted and committed before the code was checked, so a second
            # request carrying the same challenge reaches here too, and only
            # the one that actually spends the token is let in (issue #421).
            won = (await session.execute(
                update(AuthToken)
                .where(AuthToken.id == claimed.id, AuthToken.consumed_at.is_(None))
                .values(consumed_at=func.now())
                .returning(AuthToken.id)
            )).first()
            if won is None:
                raise REFUSED
            # auth_factors before recovery_codes, the one order every route
            # that touches both has to take. Spending a recovery proof writes
            # recovery_codes and the budget reset below writes auth_factors,
            # so without this the pair runs backwards from confirm_enrolment
            # and a recovery-code login racing a replacement deadlocks: two
            # transactions each holding what the other wants, resolved by
            # PostgreSQL killing one, which the caller sees as a 500 on a
            # sign-in (issue #438).
            #
            # After the token claim, not before it: operator.collapse deletes
            # auth_tokens before auth_factors, so locking the factor first
            # would trade the cycle this closes for one against a collapse run
            # while the API is still up.
            await session.execute(
                select(AuthFactor.id).where(AuthFactor.id == factor.id).with_for_update())
            # The proof is spent here rather than when it was checked, so the
            # request that loses the race rolls back and gives the code or the
            # step back with it (#424).
            if not await _spend(session, factor, proof):
                raise REFUSED
            # Answering a challenge proves the authenticator is in the right
            # hands, which is what the replacement budget waits for. An
            # attacker holding only a session cannot reach this line.
            await session.execute(
                update(AuthFactor).where(AuthFactor.id == factor.id,
                                         AuthFactor.replace_attempts > 0)
                .values(replace_attempts=0))
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


@dataclass(frozen=True)
class Proof:
    """What a presented code turned out to be, and what spending it will cost.

    Checking and spending are separate because the caller decides whether the
    thing the proof authorises actually happens. Spent at the moment it is
    checked, a code is gone whether or not the request that presented it
    succeeded, and a one-use credential consumed for nothing is a way into the
    account that its holder no longer has and cannot know about (#424, #430).
    """

    recovery_code_id: uuid.UUID | None = None
    step: int | None = None


async def _recovery_proof(user: User, code: str) -> Proof | None:
    """A recovery code, told apart from anything else without spending a try.

    Its own step because the replacement budget bounds a six-digit space, and
    a recovery code is not one: twenty symbols from an alphabet of thirty-two,
    which no online guessing reaches. Charging the budget before knowing which
    kind of code this is let anyone holding a session fill it with rubbish and
    leave the owner holding the one credential these routes exist to accept
    and no way to present it (issue #419).

    A plain SELECT in a session of its own, so it takes no lock and cannot be
    half of the auth_factors-before-recovery_codes cycle (issue #438).
    """
    if db.session_factory is None:
        return None
    async with db.session_factory() as session:
        found = (await session.execute(
            select(RecoveryCode.id).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == _hash(code.strip().lower()),
                RecoveryCode.consumed_at.is_(None))
        )).first()
    return None if found is None else Proof(recovery_code_id=found.id)


def _totp_proof(code: str, user: User, factor: AuthFactor) -> Proof | None:
    """What the authenticator's own code is worth, without consuming it."""
    try:
        secret = keyring.get_key_ring().decrypt(
            TOTP_PURPOSE, factor.secret_ciphertext, user.id.bytes).decode()
    except Exception:
        # A secret this installation can no longer read is a factor nobody can
        # pass, which is the fail-closed direction.
        return None
    step = totp.matched_step(secret, code)
    return None if step is None else Proof(step=step)


async def _verify(code: str, user: User, factor: AuthFactor) -> Proof | None:
    """What this code is, without consuming it. None if it is nothing."""
    recovered = await _recovery_proof(user, code)
    return recovered if recovered is not None else _totp_proof(code, user, factor)


async def _spend(session: AsyncSession, factor: AuthFactor, proof: Proof) -> bool:
    """Consume the proof inside the caller's transaction.

    Conditional on it still being unspent, so of two requests holding the same
    proof only one is told it spent it, and the caller that loses gives the
    credential back by rolling the transaction it was part of.
    """
    if proof.recovery_code_id is not None:
        spent = (await session.execute(
            update(RecoveryCode)
            .where(RecoveryCode.id == proof.recovery_code_id,
                   RecoveryCode.consumed_at.is_(None))
            .values(consumed_at=func.now())
            .returning(RecoveryCode.id)
        )).first()
        return spent is not None
    claimed = (await session.execute(
        update(AuthFactor)
        .where(AuthFactor.id == factor.id,
               (AuthFactor.last_step.is_(None)) | (AuthFactor.last_step < proof.step))
        .values(last_step=proof.step)
        .returning(AuthFactor.id)
    )).first()
    return claimed is not None






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


def _violated(error: IntegrityError) -> str | None:
    """The constraint a driver refused on, dug out of the wrapping.

    Three layers: SQLAlchemy's IntegrityError wraps the adapter's, which
    carries only a message, and the asyncpg error holding `constraint_name`
    hangs off it as `__cause__`. Walking `__cause__` rather than reading one
    level is what makes this work against the real driver instead of against
    an exception assembled by hand.
    """
    cause: BaseException | None = getattr(error, "orig", None)
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        try:
            name = getattr(cause, "constraint_name", None)
            if name:
                return str(name)
        except Exception:
            # A driver that raises while being asked its own attribute is not
            # one to take an answer from, and this runs inside an exception
            # handler where a second exception would replace the first.
            return None
        cause = cause.__cause__
    return None


async def _spend_replacement_attempt(factor_id: uuid.UUID) -> bool:
    """Counts the try before the code is looked at, so a wrong one costs.

    Without this the ask is free to repeat: whoever asked for the new secret
    can answer that half correctly every time, and the enrolment they hold is
    good for thirty minutes, so the old half is a six-digit space to grind at
    leisure. The login challenge's budget does not reach here, because this
    caller already has a session (issue #427).
    """
    assert db.session_factory is not None
    async with db.session_factory() as session:
        spent = (await session.execute(
            update(AuthFactor)
            .where(AuthFactor.id == factor_id,
                   AuthFactor.replace_attempts < MAX_ATTEMPTS)
            .values(replace_attempts=AuthFactor.replace_attempts + 1)
            .returning(AuthFactor.id)
        )).first()
        await session.commit()
    return spent is not None


def _carrying(issued: sessions.Issued) -> Response:
    """Nothing to report, with the rotated session cookie riding on it."""
    # Imported here rather than at the top, the way begin_challenge does it:
    # app.accounts imports this module, so the other direction can only be a
    # local import.
    from app.accounts import issue_session

    response = Response(status_code=204)
    issue_session(response, issued)
    return response


@router.delete("/api/v1/account/totp", status_code=204)
async def remove_factor(
    body: CodeRequest,
    principal: sessions.Resolved = Depends(current_principal),
) -> Response:
    """Turning the factor off costs a code, the same as replacing it.

    A session by itself must not be enough, or the factor protects an account
    only until somebody steals a cookie. A recovery code counts, which is what
    lets whoever lost the authenticator but kept a code turn it off themselves
    rather than lose the account (issue #419).

    An account holding neither has nothing to present, and no route can safely
    help it: one that removed a factor without asking for one is exactly the
    route worth stealing a session for. That way back is the command at the
    machine, `make auth-clear-factor`.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    if principal.user.state != "active":
        # current_principal admits a suspended account, which reads and
        # changes nothing. Only current_user carries that check.
        raise HTTPException(status_code=403, detail="account suspended")
    if not sessions.is_recent(principal.session):
        raise HTTPException(status_code=403, detail="recent authentication required")
    user_id = principal.user.id
    async with db.session_factory() as session:
        factor = await enrolled_factor(session, user_id)
    if factor is None:
        raise REFUSED
    # The recovery code is looked for before anything is charged, because the
    # budget below is not what guards it: a stolen session that spent all ten
    # on rubbish would otherwise leave somebody holding a working code and no
    # route that would look at it.
    proof = await _recovery_proof(principal.user, body.code)
    if proof is None:
        # The same budget replacing one spends, and spent before the digits
        # are looked at. Two doors onto the same six digits with a counter on
        # only one of them is no counter at all.
        if not await _spend_replacement_attempt(factor.id):
            raise REFUSED
        proof = _totp_proof(body.code, principal.user, factor)
    if proof is None:
        raise REFUSED

    async with db.session_factory() as session:
        async with session.begin():
            await _hold_the_account(session, user_id)
            # The factor row first, the way a replacement takes it, so the two
            # routes cannot deadlock against each other by touching
            # auth_factors and recovery_codes in opposite orders.
            await session.execute(
                select(AuthFactor.id).where(AuthFactor.id == factor.id).with_for_update())
            if not await _spend(session, factor, proof):
                raise REFUSED
            gone = (await session.execute(
                delete(AuthFactor)
                .where(AuthFactor.user_id == user_id, AuthFactor.id == factor.id)
                .returning(AuthFactor.id)
            )).first()
            if gone is None:
                raise REFUSED
            # The codes go with it. A code minted against a secret nobody
            # holds any more is a way in nobody expects.
            await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
            # The account is less protected after this than before, which
            # makes ending the other sessions matter more here, not less.
            issued = await sessions.rotate_and_revoke_others(
                session, principal, spend_capabilities=False)
    await sessions.close_other_sockets(principal)
    return _carrying(issued)


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
    if principal.user.state != "active":
        # current_principal admits a suspended account, which may read and
        # change nothing. Enrolment started while active must not complete
        # after that.
        raise HTTPException(status_code=403, detail="account suspended")
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
    proof: Proof | None = None
    async with db.session_factory() as session:
        replacing = await enrolled_factor(session, user_id)
    if replacing is not None:
        if body.current_code is None:
            raise REFUSED
        # Ahead of the charge, for the reason remove_factor gives: the budget
        # bounds six digits, and a recovery code is not six digits.
        proof = await _recovery_proof(principal.user, body.current_code)
        if proof is None:
            if not await _spend_replacement_attempt(replacing.id):
                # The budget is gone. Signing in with the factor clears it,
                # which is the one thing an attacker holding only a session
                # cannot do.
                raise REFUSED
            proof = _totp_proof(body.current_code, principal.user, replacing)
        if proof is None:
            raise REFUSED

    # After the factor read, and that ordering is the whole of the guard.
    # Removing a factor deletes it and revokes the other sessions in one
    # transaction, so this request reads no factor only once that transaction
    # has committed, and a reading of the session taken afterwards sees the
    # revocation with it. Asked before the read it would prove nothing: a
    # stolen session holds a sealed enrolment for thirty minutes and cannot
    # confirm it while the factor stands, and the owner taking that factor off
    # is exactly what turns the request into a first enrolment, which asks for
    # no proof at all.
    if not await sessions.is_live(principal.session.id):
        raise REFUSED

    ring = keyring.get_key_ring()
    async with db.session_factory() as session:
        async with session.begin():
            await _hold_the_account(session, user_id)
            if replacing is not None:
                # The factor row first, always, whichever kind of proof this
                # is, and answer_challenge takes it first too: auth_factors
                # before recovery_codes is the order every route that touches
                # both must take (issue #438). Spending a recovery code touches recovery_codes and then
                # auth_factors, and spending a step touches auth_factors and
                # then the code sweep touches recovery_codes: two replacements
                # of different kinds would take the two tables in opposite
                # orders and deadlock, and PostgreSQL resolves that by killing
                # one of them, which reaches the caller as a 500.
                await session.execute(
                    select(AuthFactor.id).where(AuthFactor.id == replacing.id)
                    .with_for_update())
                # Inside the transaction, so a replacement that turns out to
                # be impossible gives the code or the step back instead of
                # keeping them for a change that did not happen (#430).
                #
                # Before the codes are cleared, not after: spending a recovery
                # code means marking the row consumed, and the delete below
                # would have taken the row out from under it.
                if proof is None or not await _spend(session, replacing, proof):
                    raise REFUSED
                # Bound to the factor that was proved, and the request only
                # continues if it is the one that removed it. An account-wide
                # delete would let the last writer replace a factor it never
                # proved, which is the hole this route is closing.
                gone = (await session.execute(
                    delete(AuthFactor)
                    .where(AuthFactor.user_id == user_id, AuthFactor.id == replacing.id)
                    .returning(AuthFactor.id)
                )).first()
                if gone is None:
                    raise REFUSED
            # A first enrolment deletes nothing at all. Deleting by account
            # here is what let two of them race: both read no factor, the
            # first installed one, and the second removed it and installed
            # its own having proved nothing. With no delete, the unique
            # constraint on (user_id, kind) refuses the second.
            session.add(AuthFactor(
                user_id=user_id, kind="totp",
                secret_ciphertext=ring.encrypt(TOTP_PURPOSE, secret.encode(), user_id.bytes),
                key_version=ring.active_version,
                confirmed_at=func.now(),
                # The confirming code is spent, like every other one: it is
                # live for another ninety seconds otherwise.
                last_step=step,
            ))
            try:
                await session.flush()
            except IntegrityError as raced:
                # A first enrolment deletes nothing, so the unique constraint
                # on (user_id, kind) is what refuses a second one that read no
                # factor at the same moment. Letting the violation out would
                # answer a case the design expects with a 500.
                #
                # Only that constraint. Labelling every integrity error a race
                # would give a foreign key or a recovery-code collision the
                # same friendly answer and keep a real fault out of the 500s
                # somebody is watching.
                if _violated(raced) != ONE_FACTOR_PER_ACCOUNT:
                    raise
                raise HTTPException(
                    status_code=409,
                    detail="a second factor was enrolled already") from raced
            # Replacing an enrolment replaces its codes with it: a code minted
            # for a secret nobody holds any more is a way in nobody expects.
            #
            # After the factor insert, never before it, and this is the whole
            # of the first enrolment's share of the one lock order. An insert
            # whose unique key collides with a row some other transaction has
            # deleted and not yet committed waits for that transaction, so a
            # first enrolment that ran the delete first would be holding
            # recovery_codes while waiting on auth_factors, which is the cycle
            # every other route here is arranged to avoid (issue #438).
            await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
            for code in codes:
                session.add(RecoveryCode(user_id=user_id, code_hash=_hash(code)))
            # After the guarded flush, not before it. Any statement issued
            # while the new factor is still pending autoflushes it, and that
            # flush happens outside the try, so the constraint violation this
            # route expects would escape as a 500 instead of a 409.
            # Turning a second factor on, or moving it to a new authenticator,
            # is what somebody does when they think their account is at risk.
            # A factor gates the next sign-in and says nothing about a session
            # already open, so the eviction has to be explicit (issue #433).
            #
            # Reset and recovery links are left alone: they prove control of a
            # mailbox, which is not what changed here, and spending them would
            # strip a way back in from somebody who just secured their account.
            issued = await sessions.rotate_and_revoke_others(
                session, principal, spend_capabilities=False)
    # After the transaction, never inside it: a revoked row stops the next
    # request and does not reach a socket that already bound its principal.
    await sessions.close_other_sockets(principal)
    return _carrying(issued)
