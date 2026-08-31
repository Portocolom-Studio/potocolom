"""How often the sign-in path may be asked, per identifier and per caller.

Both subjects are counted in a ten minute window, with a wait that starts after
five attempts and doubles to eight seconds (docs/blueprint.md). Only the
identifier carries a ceiling: ten, and past it the answer is 429. The address
carries none, because one NAT, one proxy uvicorn has not been told to trust, or
the loopback publish the compose file ships arrives here as a single address,
and a ceiling there would sign a whole installation out of this route
(docs/decisions.md). A queue bounds that peer instead.

The wait is half the control. A ceiling on its own says where the line is and
hands anybody who knows an address a way to keep its owner out; a wait that
grows costs an attacker the whole window and costs somebody who mistyped their
password twice nothing at all. Against one address the wait is the whole bound,
and it is a bound only because the turns are taken one at a time: attempts that
overlap queue behind one another rather than serving the same eight seconds
together. Once the cap arrives that peer gets one attempt every eight seconds,
a few hundred an hour. A turn more than thirty seconds out is not handed over
at all and is answered 503, because a connection held for as long as a flood
cares to make the queue is an amplification rather than a limit.

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
from sqlalchemy import case, delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.tables import LoginAttempt

WINDOW = timedelta(minutes=10)
IDENTIFIER_LIMIT = 10
FREE_ATTEMPTS = 5
MAX_DELAY_S = 8.0
MAX_QUEUE_S = 30.0

REFUSED = HTTPException(status_code=429, detail="too many sign-in attempts")
BUSY = HTTPException(status_code=503, detail="sign-in is busy, try again shortly",
                     headers={"Retry-After": str(int(MAX_QUEUE_S))})


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def delay_for(attempts: int) -> float:
    """Seconds to hold the attempt numbered `attempts` before answering it."""
    if attempts <= FREE_ATTEMPTS:
        return 0.0
    return min(MAX_DELAY_S, 0.5 * 2 ** (attempts - FREE_ATTEMPTS - 1))


async def _charge(session: AsyncSession, scope: str, value: str) -> tuple[int, datetime | None]:
    """Count one attempt against a subject, and answer with its row.

    One statement, because a read followed by a write loses increments exactly
    the way the challenge budget did before #421: two attempts that overlap
    both see the same total and both store one more than it, so a flood is
    charged for a fraction of what it spent. The conflicting writer waits on
    the row here instead, so neither can miss the other, and the row stays
    locked for the rest of the transaction, which is what lets `_reserve` read
    and move the queue below without a second subject slipping between.

    An expired row is reset rather than deleted, so the window starts at the
    attempt that opens it. Pruning is only about the rows nobody comes back to.
    """
    now = _now()
    fresh = LoginAttempt.expires_at <= now
    statement = insert(LoginAttempt).values(
        scope=scope, subject=_digest(value), attempts=1, expires_at=now + WINDOW)
    row = (await session.execute(
        statement.on_conflict_do_update(
            index_elements=["scope", "subject"],
            set_={
                "attempts": case((fresh, 1), else_=LoginAttempt.attempts + 1),
                "expires_at": case((fresh, now + WINDOW), else_=LoginAttempt.expires_at),
            },
        ).returning(LoginAttempt.attempts, LoginAttempt.not_before)
    )).one()
    return row.attempts, row.not_before


async def _reserve(session: AsyncSession, address: str) -> float | None:
    """Take this peer's next turn and answer with the wait, or None if it is full.

    A sleep is latency for one task and not a rate. Twenty attempts from one
    peer that each sleep the eight second cap at the same moment are all
    answered inside one eight seconds, so a wait nobody has to queue for costs
    a flood nothing: measured, twenty of them finished in 8.206s. The turn is
    stored on the address row instead, which `_charge` has already locked for
    this transaction, so attempts that overlap read different turns and serve
    them one after another. That is what makes the documented pace true.

    Past the cap the answer is 503 and the queue is not moved for it. Extending
    it on a refusal would let a flood push a shared peer out for the rest of
    its window, which is the very outage the missing address ceiling exists to
    avoid; leaving it where it is means the queue always drains inside the cap
    and a bystander who comes back gets a turn. 503 with Retry-After also says
    the route is busy, where 429 on a sign-in reads as the account being shut.
    Simply waiting however long the queue is was the other answer, and it hands
    an attacker a connection held for as long as they care to make it.
    """
    attempts, taken = await _charge(session, "address", address)
    now = _now()
    turn = max(taken or now, now) + timedelta(seconds=delay_for(attempts))
    waiting = (turn - now).total_seconds()
    if waiting > MAX_QUEUE_S:
        return None
    await session.execute(
        update(LoginAttempt)
        .where(LoginAttempt.scope == "address", LoginAttempt.subject == _digest(address))
        .values(not_before=turn))
    return waiting


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
        identifier, _ = await _charge(session, "identifier", subject)
        queued = 0.0 if peer is None else await _reserve(session, peer)
        await session.commit()
    if identifier > IDENTIFIER_LIMIT:
        raise REFUSED
    if queued is None:
        raise BUSY
    # Only the address queues. The identifier stops at ten, so ten attempts is
    # all that can ever overlap there and the ceiling is already the bound; a
    # queue on it would hand anybody who knows an address a way to park its
    # owner behind a wait they filled, which is what the ceiling is careful not
    # to be. Outside the session on purpose either way: the pool is fifteen
    # deep, and a delay served with a connection in hand would spend the pool
    # on callers who are doing nothing with it.
    await sleep(max(delay_for(identifier), queued))


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
