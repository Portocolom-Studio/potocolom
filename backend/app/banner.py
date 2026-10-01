"""The one banner every SPA shows, set by an administrator, read by everyone.

Sessions resolve against PostgreSQL on every request with no cache to
invalidate (sessions.py); the banner does the same, but GET /api/v1/config is
on the hot path for every page load, so it keeps a short in-process TTL cache
rather than a round trip per request. Set and clear invalidate it on the
replica that served them; the rest catch up within the TTL, the same
self-hosted, one-process, no-Redis story admin.py's read-anomaly counter
tells (docs/blueprint.md defers cross-replica coordination to the cloud
profile).
"""

import logging
import time
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.dialects.postgresql import insert
from starlette.responses import Response

from app import db
from app.auth import require_accounts_mode, require_role
from app.tables import StatusBanner, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

CACHE_TTL = 5.0
CUSTOM_TEXT_MAX = 280

logger = logging.getLogger("potocolom.banner")

_cache: tuple[float, dict | None] | None = None
_generation = 0


def _default_message_key(kind: str) -> str:
    return f"app.banner.{kind}"


def _invalidate() -> None:
    global _cache, _generation
    _cache = None
    _generation += 1


async def current_banner() -> dict | None:
    """GET /api/v1/config drives sign-in, so a banner that cannot be read is
    no banner rather than a failed config: the outage is logged and cached for
    the TTL so every page load does not hit the failing database again."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL:
        return _cache[1]
    # A write that lands while this read is in flight bumps the generation;
    # storing the older read would undo that write's invalidation.
    generation = _generation
    try:
        value = await _load()
    except Exception as error:
        logger.warning("status banner unavailable (%s); serving none", error)
        value = None
    if generation == _generation:
        _cache = (now, value)
    return value


async def _load() -> dict | None:
    if db.session_factory is None:
        return None
    async with db.session_factory() as session:
        row = await session.get(StatusBanner, True)
    if row is None:
        return None
    return {"kind": row.kind, "message_key": row.message_key, "custom_text": row.custom_text}


class BannerSet(BaseModel):
    kind: Literal["high_demand", "degraded", "maintenance"]
    message_key: str | None = None
    custom_text: str | None = None

    @field_validator("custom_text")
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
            raise ValueError("custom_text must be plain text on one line")
        if len(value) > CUSTOM_TEXT_MAX:
            raise ValueError(f"custom_text must be at most {CUSTOM_TEXT_MAX} characters")
        return value


@router.put("/api/v1/admin/banner", status_code=204)
async def set_banner(
    change: BannerSet,
    actor: User = Depends(require_role("admin")),
) -> Response:
    """Set or replace the banner outright: there is one row, so there is
    nothing to merge with what was there before."""
    if change.message_key is not None and change.message_key != _default_message_key(change.kind):
        raise HTTPException(status_code=422, detail="message_key is not valid for this kind")
    if change.message_key is None and not change.custom_text:
        raise HTTPException(status_code=422, detail="a banner needs a message_key or custom_text")
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    values = {
        "kind": change.kind,
        "message_key": change.message_key,
        "custom_text": change.custom_text,
        "updated_by": actor.id,
        "updated_at": datetime.now(timezone.utc),
    }
    # One statement, so two first-time PUTs, or a PUT racing a DELETE, cannot
    # trip over a row the other one inserted or removed.
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(StatusBanner).values(id=True, **values)
                .on_conflict_do_update(index_elements=[StatusBanner.id], set_=values)
            )
    _invalidate()
    return Response(status_code=204)


@router.delete("/api/v1/admin/banner", status_code=204)
async def clear_banner(actor: User = Depends(require_role("admin"))) -> Response:
    if db.session_factory is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with db.session_factory() as session:
        async with session.begin():
            row = await session.get(StatusBanner, True)
            if row is not None:
                await session.delete(row)
    _invalidate()
    return Response(status_code=204)
