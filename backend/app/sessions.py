"""Browser sessions. The row keeps the SHA-256 of the token and nothing else.

An administrator session is the most valuable thing in the install, so it takes
the short idle window and never the remember-me convenience.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update

from app import db
from sqlalchemy.ext.asyncio import AsyncSession

from app.tables import AuthToken, Session, User

ABSOLUTE = timedelta(hours=12)
REMEMBER_ABSOLUTE = timedelta(days=30)
REMEMBER_IDLE = timedelta(days=7)
ADMIN_IDLE = timedelta(minutes=30)
RECENT_AUTH = timedelta(minutes=30)
TOKEN_BYTES = 32
TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass
class Issued:
    token: str
    csrf: str
    # Seconds for the cookie, or None for one that dies with the browser. A
    # remembered row is worthless if the cookie carrying it does not outlive
    # the browser process.
    max_age: int | None = None


@dataclass
class Resolved:
    session: Session
    user: User


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def is_secure(public_url: str) -> bool:
    """One predicate for the whole cookie decision. Two that disagree on a
    malformed value would set the __Host- prefix without Secure, which every
    browser then drops."""
    return public_url.startswith("https://")


def cookie_names(public_url: str) -> tuple[str, str]:
    """__Host- requires Secure, which a browser refuses to set over plain HTTP,
    and LAN self-hosting is plain HTTP."""
    prefix = "__Host-" if is_secure(public_url) else ""
    return f"{prefix}potocolom_session", f"{prefix}potocolom_csrf"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issued(remembered: bool) -> Issued:
    return Issued(
        token=secrets.token_urlsafe(TOKEN_BYTES),
        csrf=secrets.token_urlsafe(TOKEN_BYTES),
        max_age=int(REMEMBER_ABSOLUTE.total_seconds()) if remembered else None,
    )


async def mint(user: User, remember_me: bool, authenticated: bool = False) -> Issued:
    """authenticated defaults to False because setup proves a link, not a
    person, and must not open the window that guards credential changes."""
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    if user.role == "admin":
        remembered, idle = False, ADMIN_IDLE
    elif remember_me:
        remembered, idle = True, REMEMBER_IDLE
    else:
        remembered, idle = False, None
    now = _now()
    issued = _issued(remembered)
    async with db.session_factory() as session:
        session.add(Session(
            user_id=user.id,
            token_hash=token_hash(issued.token),
            remember_me=remembered,
            absolute_expires_at=now + (REMEMBER_ABSOLUTE if remembered else ABSOLUTE),
            idle_expires_at=now + idle if idle is not None else None,
            recent_auth_at=now if authenticated else None,
        ))
        await session.commit()
    return issued


async def resolve(token: str) -> Resolved | None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    now = _now()
    async with db.session_factory() as session:
        found = (await session.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == token_hash(token))
        )).first()
        if found is None:
            return None
        row, user = found
        if row.revoked_at is not None or row.absolute_expires_at <= now:
            return None
        if row.idle_expires_at is not None and row.idle_expires_at <= now:
            return None
        if row.last_seen_at is None or now - row.last_seen_at >= TOUCH_INTERVAL:
            # Sliding on every request would make each authenticated call a
            # SELECT, an UPDATE and a COMMIT on a fifteen connection pool, to
            # move a window that is measured in minutes.
            if row.idle_expires_at is not None:
                row.idle_expires_at = now + (
                    ADMIN_IDLE if user.role == "admin" else REMEMBER_IDLE)
            row.last_seen_at = now
            await session.commit()
        return Resolved(session=row, user=user)


async def rotate_and_revoke_others(db_session: AsyncSession, resolved: "Resolved",
                                   spend_capabilities: bool = True) -> Issued:
    """Every other session ends, and this one gets a new token to carry on with.

    Revoking the others is what a credential change is usually for, and until
    #436 it never reached the copy that matters most: a stolen cookie is a
    copy of the token belonging to the browser making the change, so leaving
    that one alone left the intruder holding it. Signing the owner out instead
    reads as a failure and invites them to try again, so the token is replaced
    under them and they never notice.

    Replaced on the row, rather than by revoking it and inserting another. The
    session keeps its id, its clocks and its recent authentication, so nothing
    downstream has to learn that a rotation happened: a socket bound to this
    id at its handshake stays bound to a live session, and the window that
    guards the next credential change carries on rather than restarting.

    Inside the caller's transaction on purpose. The token this returns is
    handed to a browser, and a change that rolls back must not leave that
    browser holding the only working credential on the account.

    Lives here rather than beside any one caller because more than one kind of
    change owes this: a credential, an address, a linked identity, a second
    factor.

    Refuses with 409 when the token the caller presented is no longer the one
    on the row, which is what keeps two requests holding the same cookie from
    both being answered.
    """
    # Every transaction takes the account's session rows in one order, or two
    # credential changes racing on the same account deadlock: each revocation
    # holds the row the other one is about to rotate.
    await db_session.execute(
        select(Session.id)
        .where(Session.user_id == resolved.user.id, Session.revoked_at.is_(None))
        .order_by(Session.id)
        .with_for_update()
    )
    await db_session.execute(
        update(Session)
        .where(Session.user_id == resolved.user.id, Session.id != resolved.session.id,
               Session.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    issued = _issued(resolved.session.remember_me)
    swapped = (await db_session.execute(
        update(Session)
        .where(Session.id == resolved.session.id,
               Session.token_hash == resolved.session.token_hash,
               Session.revoked_at.is_(None))
        .values(token_hash=token_hash(issued.token))
        .returning(Session.id)
    )).first()
    if swapped is None:
        # The presented token is no longer the stored one. Matching on the id
        # alone is not enough, because the principal was resolved in an
        # earlier transaction: two requests carrying the same cookie both
        # reach here, and the second overwrote what the first had just
        # handed out. An owner's password change committed, a stolen copy of
        # the same cookie changed the address a moment later, and the owner
        # was signed out holding a dead token while the intruder kept the
        # session and got their change as well.
        #
        # Raising rather than reissuing, so the caller's transaction rolls
        # back: the loser's change must not stand, and it must not be given a
        # cookie either.
        raise HTTPException(status_code=409,
                            detail="this session changed while that was in flight")
    if spend_capabilities:
        # Reset and recovery links go with them. Changing a credential after a
        # mailbox is compromised is meant to end what that mailbox can still
        # do, and a link already sent to it is exactly that.
        await db_session.execute(
            update(AuthToken)
            .where(AuthToken.user_id == resolved.user.id,
                   AuthToken.purpose.in_(("reset", "recovery")),
                   AuthToken.consumed_at.is_(None))
            .values(consumed_at=func.now())
        )
    return issued


async def close_other_sockets(resolved: "Resolved") -> None:
    """After the change is durable, and never inside its transaction.

    A revoked row stops the next request; it does not reach a socket that
    bound its principal at the handshake, so somebody evicting a stranger
    would otherwise leave the stranger drawing.
    """
    await close_sockets(resolved.user.id, keep=resolved.session.id)


async def close_sockets(user_id: uuid.UUID, session_id: uuid.UUID | None = None,
                        keep: uuid.UUID | None = None) -> None:
    """A realtime socket binds its principal once, so revoking a session has
    to reach the socket explicitly or the canvas keeps drawing."""
    from app import realtime

    await realtime.close_revoked(user_id, session_id, keep)


async def revoke(session_id: uuid.UUID) -> None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        owner = (await session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=_now())
            .returning(Session.user_id)
        )).scalar_one_or_none()
        await session.commit()
    if owner is not None:
        await close_sockets(owner, session_id)


async def revoke_all(user_id: uuid.UUID) -> None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        await session.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await session.commit()
    await close_sockets(user_id, None)


async def is_live(session_id: uuid.UUID) -> bool:
    """Whether that account session can still act, judged without its token.

    A socket that bound its principal at the handshake has no token to
    re-present, and needs to know whether the session died in between.
    """
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    now = _now()
    async with db.session_factory() as session:
        row = (await session.execute(
            select(Session).where(Session.id == session_id)
        )).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.absolute_expires_at <= now:
        return False
    return row.idle_expires_at is None or row.idle_expires_at > now


async def live_among(session_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Which of those account sessions can still act, by the rules is_live uses.

    One query rather than one per session: the socket sweep asks about every
    socket the process holds, on every tick.
    """
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    if not session_ids:
        return set()
    now = _now()
    async with db.session_factory() as session:
        rows = (await session.execute(
            select(Session.id).where(
                Session.id.in_(session_ids),
                Session.revoked_at.is_(None),
                Session.absolute_expires_at > now,
                or_(Session.idle_expires_at.is_(None), Session.idle_expires_at > now),
            )
        )).scalars().all()
    return set(rows)


async def rotate(session_id: uuid.UUID, user: User) -> Issued:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        remembered = (await session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=_now())
            .returning(Session.remember_me)
        )).scalar_one_or_none()
        await session.commit()
    if remembered is None:
        # Nothing was revoked, so there was nothing to rotate. Minting here
        # would turn an unknown or already dead session into a live one.
        raise RuntimeError("no live session to rotate")
    return await mint(user, remember_me=bool(remembered))


def is_recent(session: Session) -> bool:
    return session.recent_auth_at is not None and session.recent_auth_at > _now() - RECENT_AUTH
