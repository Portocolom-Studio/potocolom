"""How often the sign-in path may be asked, per identifier and per caller.

Ten per identifier and thirty per address in ten minutes, with a wait that
starts after five and doubles to eight seconds (docs/blueprint.md). The wait is
half the control. A ceiling on its own says where the line is and hands anybody
who knows an address a way to keep its owner out; a wait that grows costs an
attacker the whole window and costs somebody who mistyped their password twice
nothing at all.

Every attempt is counted, right or wrong. What this exists to bound is an
attacker who already holds the password and is grinding the second factor by
starting challenge after challenge (#423), and every one of those attempts
succeeds. A counter that only charged failures would count none of them.

The count is a row rather than a Redis key or a number in this process. A
per-process counter is correct only while there is one process, and the cloud
profile runs more than one, where each replica would admit the whole allowance.
"""

import hashlib
from asyncio import sleep
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.tables import LoginAttempt

WINDOW = timedelta(minutes=10)
IDENTIFIER_LIMIT = 10
ADDRESS_LIMIT = 30
FREE_ATTEMPTS = 5
MAX_DELAY_S = 8.0

REFUSED = HTTPException(status_code=429, detail="too many sign-in attempts")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def delay_for(attempts: int) -> float:
    """Seconds to hold the attempt numbered `attempts` before answering it."""
    if attempts <= FREE_ATTEMPTS:
        return 0.0
    return min(MAX_DELAY_S, 0.5 * 2 ** (attempts - FREE_ATTEMPTS - 1))


async def _charge(session: AsyncSession, scope: str, value: str) -> int:
    """Count one attempt against a subject and answer with its running total.

    One statement, because a read followed by a write loses increments exactly
    the way the challenge budget did before #421: two attempts that overlap
    both see the same total and both store one more than it, so a flood is
    charged for a fraction of what it spent. The conflicting writer waits on
    the row here instead, so neither can miss the other.

    An expired row is reset rather than deleted, so the window starts at the
    attempt that opens it. Pruning is only about the rows nobody comes back to.
    """
    now = _now()
    fresh = LoginAttempt.expires_at <= now
    statement = insert(LoginAttempt).values(
        scope=scope, subject=_digest(value), attempts=1, expires_at=now + WINDOW)
    return (await session.execute(
        statement.on_conflict_do_update(
            index_elements=["scope", "subject"],
            set_={
                "attempts": case((fresh, 1), else_=LoginAttempt.attempts + 1),
                "expires_at": case((fresh, now + WINDOW), else_=LoginAttempt.expires_at),
            },
        ).returning(LoginAttempt.attempts)
    )).scalar_one()


async def charge_login(subject: str, http: Request) -> None:
    """Charge one sign-in attempt, then refuse it or hold it for its delay.

    Called before the password is verified, for three reasons. Argon2id is the
    expensive half of this route, and a limit that runs after it has already
    paid the cost it exists to bound. An attempt charged afterwards would be
    free whenever it failed early. And charging before anything has been looked
    up is what keeps an address nobody holds indistinguishable from one
    somebody does, which the route's ABSENT_ACCOUNT_HASH is there to protect.
    """
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    # The socket peer, which uvicorn has already rewritten from X-Forwarded-For
    # for the peers FORWARDED_ALLOW_IPS trusts. Reading that header here
    # instead would take a value any caller can set and let one choose which
    # bucket it is counted in (app/realtime.py says the same of the fleet peer).
    peer = http.client.host if http.client else None
    async with db.session_factory() as session:
        identifier = await _charge(session, "identifier", subject)
        address = 0 if peer is None else await _charge(session, "address", peer)
        await session.commit()
    if identifier > IDENTIFIER_LIMIT or address > ADDRESS_LIMIT:
        raise REFUSED
    # Outside the session on purpose: the pool is fifteen deep and this waits
    # up to eight seconds, so a delay served with a connection in hand would
    # spend the pool on callers who are doing nothing with it.
    await sleep(max(delay_for(identifier), delay_for(address)))


async def prune() -> None:
    """Drop the windows that have run out.

    A row that outlives its window is reset by the next attempt against the
    same subject, so this is not what keeps the count honest. It is what keeps
    the table from holding a digest of every address anybody ever typed here.
    """
    if db.session_factory is None:
        return
    async with db.session_factory() as session:
        await session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at < _now()))
        await session.commit()
