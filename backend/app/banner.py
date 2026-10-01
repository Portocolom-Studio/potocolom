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

import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import Response

from app import db
from app.auth import require_accounts_mode, require_role
from app.tables import StatusBanner, User

router = APIRouter(dependencies=[Depends(require_accounts_mode)])

CACHE_TTL = 5.0

_cache: tuple[float, dict | None] | None = None


def _default_message_key(kind: str) -> str:
    return f"app.banner.{kind}"


def _invalidate() -> None:
    global _cache
    _cache = None


async def current_banner() -> dict | None:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL:
        return _cache[1]
    value = await _load()
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
    custom_text: str | None = Field(default=None, max_length=280)


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
    async with db.session_factory() as session:
        async with session.begin():
            row = await session.get(StatusBanner, True)
            if row is None:
                row = StatusBanner(id=True)
                session.add(row)
            row.kind = change.kind
            row.message_key = change.message_key
            row.custom_text = change.custom_text
            row.updated_by = actor.id
            row.updated_at = datetime.now(timezone.utc)
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
