"""Completion-side persistence for privacy-bounded product usage events."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from typing import Any

from app import db
from app.tables import Job, Model, UsageEvent

logger = logging.getLogger("potocolom.usage_events")

LABELS = ("art", "photo_edit", "design", "character", "nsfw", "other")


def _category(control: dict) -> tuple[str, float | None]:
    category = control.get("category")
    if category not in LABELS:
        category = "other"
    return category, _optional_float(control.get("category_score"))


async def record_job(job_id: uuid.UUID, control: dict) -> None:
    if db.session_factory is None:
        return
    try:
        async with db.session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            model = await session.get(Model, job.model_id)
            capabilities = set(model.capabilities) if model is not None else set()
            action = (
                "generate" if job.source_asset_id is None else
                "enhance" if "upscale" in capabilities else
                "edit"
            )
            category, score = _category(control)
            session.add(UsageEvent(
                user_id=job.user_id,
                kind="job",
                action=action,
                model_id=job.model_id,
                # Model tier routing is not shipped yet, so there is no honest value.
                tier=None,
                category=category,
                category_score=score,
                gpu_ms=job.gpu_ms,
                duration_ms=_optional_int(control.get("duration_ms")),
                frames=1,
            ))
            await session.commit()
    except Exception:
        logger.exception("usage event write failed for job %s", job_id)


async def record_realtime(
    user_id: uuid.UUID, model_id: str, control: dict,
) -> None:
    if db.session_factory is None:
        return
    try:
        category, score = _category(control)
        async with db.session_factory() as session:
            session.add(UsageEvent(
                user_id=user_id,
                kind="realtime",
                action="draw",
                model_id=model_id,
                tier=None,
                category=category,
                category_score=score,
                gpu_ms=_optional_int(control.get("gpu_ms")),
                duration_ms=_optional_int(control.get("duration_ms")),
                frames=_optional_int(control.get("frames")),
            ))
            await session.commit()
    except Exception:
        logger.exception("usage event write failed for realtime session")


def _numeric(value: Any) -> bool:
    """bool is a subclass of int, and these values arrive from the worker.

    Non-finite floats are not numeric for our purposes: they survive json.loads,
    persist in PostgreSQL, and then break serialization and SUM rollups
    downstream (issue #203).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        # isfinite() would raise OverflowError on a big int, and this is a
        # predicate: callers do not expect it to raise.
        return -2**31 <= value < 2**31
    # The float branch feeds int4 columns too, so finite is not enough.
    return math.isfinite(value) and -2**31 <= value < 2**31


def _optional_int(value: Any) -> int | None:
    return int(value) if _numeric(value) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if _numeric(value) else None


def schedule_job(job_id: uuid.UUID, control: dict) -> None:
    asyncio.create_task(record_job(job_id, dict(control)))


def schedule_realtime(user_id: uuid.UUID, model_id: str, control: dict) -> None:
    asyncio.create_task(record_realtime(user_id, model_id, dict(control)))
