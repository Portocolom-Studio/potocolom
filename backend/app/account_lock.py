"""The per-account advisory gate every overlapping auth transaction takes first.

There is no single table lock order across collapse and the credential routes
(issue #444). The same key `_budget_lock` used to take, taken before any row
lock, serialises those transactions so they can wait but cannot cycle.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


def budget_lock(user_id: uuid.UUID) -> int:
    """A per-account advisory key, the way enable.py takes SETUP_LOCK."""
    return int.from_bytes(user_id.bytes[:8], "big", signed=True)


async def hold_the_account(
    session: AsyncSession,
    user_id: uuid.UUID,
    busy_detail: str = "too many changes to this account at once",
    *,
    wait: bool = False,
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

    HTTP routes give up rather than queue: a waiter holds its pooled
    connection, and the pool is fifteen deep, so an unbounded wait would let
    a flood aimed at one account starve every other request. Nothing has been
    written when this runs, so the caller can safely be told to come back.

    Collapse waits. A 3s abort of collapse is the half-finished destruction
    this gate exists to stop, and the command is one process with no pool to
    starve.
    """
    if wait:
        await session.execute(text("SET LOCAL lock_timeout = '0'"))
    else:
        await session.execute(text("SET LOCAL lock_timeout = '3s'"))
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": budget_lock(user_id)},
        )
    except DBAPIError as busy:
        if wait:
            raise
        # The wide class, deliberately: asyncpg raises LockNotAvailableError,
        # which has no DBAPI class of its own, so SQLAlchemy wraps it as a
        # plain DBAPIError and the narrower OperationalError never arrives.
        # This transaction has written nothing, so telling the caller to come
        # back is safe. An attempt a previous transaction already counted
        # stays counted, which is what a refused guess costs anyway.
        raise HTTPException(status_code=503, detail=busy_detail) from busy
    if not wait:
        # Only this wait was meant to be bounded, and SET LOCAL lasts for the
        # whole transaction. Left on, a row lock taken further down times out
        # into a DBAPIError no handler here expects, and the caller gets a 500
        # in place of the wait it used to do. Nothing after this line queues
        # behind an unrelated account, because the key above is already held.
        await session.execute(text("SET LOCAL lock_timeout = '0'"))
