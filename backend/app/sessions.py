"""Browser sessions. The row keeps the SHA-256 of the token and nothing else.

An administrator session is the most valuable thing in the install, so it takes
the short idle window and never the remember-me convenience.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app import db
from app.tables import Session, User

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
    issued = Issued(
        token=secrets.token_urlsafe(TOKEN_BYTES),
        csrf=secrets.token_urlsafe(TOKEN_BYTES),
        max_age=int(REMEMBER_ABSOLUTE.total_seconds()) if remembered else None,
    )
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
