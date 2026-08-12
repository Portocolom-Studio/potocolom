"""Job dispatch and generation history (docs/blueprint.md, the generation
request path).

Self-hosted shape: an in-process queue and a dispatch loop that is the
degenerate, always-leader scheduler. The cloud profile swaps InProcessQueues
for Redis sorted sets and elects a leader; the API surface and the worker
protocol do not change. PostgreSQL rows are the source of truth throughout;
the queue is rebuilt from them on startup.
"""

import asyncio
import heapq
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app import db, realtime, registry
from app.auth import current_user, require_role
from app.manifests import validate_params
from app.settings import get_settings
from app.storage import get_storage
from app.tables import Asset, Job, User

logger = logging.getLogger("potocolom.jobs")

router = APIRouter()

JOB_QUEUE = "queue:jobs"
TIER_DEFAULT = 1  # 0 resuming, 1 paid, 2 trial; one tier until billing exists
DISPATCH_INTERVAL = 0.1  # the scheduler step cadence (docs/blueprint.md)
JOB_DISPATCH_DEPTH = 2  # queued jobs per worker; overlap encode/upload with denoise

TERMINAL_STATES = ("succeeded", "failed")
THUMBNAIL_MAX_EDGE = 384  # thumbnail rendition size (issue #56)
DOWNLOAD_SLUG_MAX_LENGTH = 48
DOWNLOAD_SLUG_MAX_WORDS = 6
# One hundred generations exceeds a practical edit chain but bounds corrupt cycles.
LINEAGE_SUBTREE_MAX_DEPTH = 100
# Six hundred matches the canvas tile ceiling and bounds unusually broad trees.
LINEAGE_SUBTREE_MAX_NODES = 600


class Queues(Protocol):
    async def push(self, queue: str, id: str, tier: int) -> None: ...

    async def pop(self, queue: str) -> str | None: ...


class InProcessQueues:
    """A heap in the single API process; RedisQueues replaces it in the cloud."""

    def __init__(self) -> None:
        self._heaps: dict[str, list[tuple[int, int, str]]] = {}
        self._seq = count()

    async def push(self, queue: str, id: str, tier: int) -> None:
        heapq.heappush(self._heaps.setdefault(queue, []), (tier, next(self._seq), id))

    async def pop(self, queue: str) -> str | None:
        heap = self._heaps.get(queue)
        if not heap:
            return None
        return heapq.heappop(heap)[2]


queues: Queues = InProcessQueues()


@dataclass
class InFlight:
    worker: realtime.Worker
    storage_key: str
    thumb_storage_key: str
    user_id: uuid.UUID
    attempt: int = 1


inflight: dict[uuid.UUID, InFlight] = {}
lost_jobs: list[uuid.UUID] = []  # drained by the dispatch loop


def storage_key_in_flight(key: str) -> bool:
    for job_id, entry in inflight.items():
        expected = storage_keys_for_attempt(entry.user_id, job_id, entry.attempt)
        if key in expected:
            return True
    return False


def storage_keys_for_attempt(user_id: uuid.UUID, job_id: uuid.UUID, attempt: int) -> tuple[str, str]:
    prefix = f"{user_id}/{job_id}-attempt-{attempt}"
    return f"{prefix}.png", f"{prefix}-thumb.webp"

# Latest reported denoising fraction per running job. Transient by design:
# the job row is the source of truth for state, progress is display only.
live_progress: dict[uuid.UUID, float] = {}
# Monotonic timestamp of the last dispatch or job_progress for each inflight job.
last_progress_at: dict[uuid.UUID, float] = {}

# SSE subscribers per job; events are transient, the job row is the truth.
subscribers: dict[uuid.UUID, list[asyncio.Queue]] = {}


def _worker_float(value: object, default: float = 0.0) -> float:
    """Coerce a worker-supplied number to a float, or the default.

    float() raises TypeError on a list or dict and OverflowError on a big int,
    both of which json.loads produces from ordinary JSON.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, int) and not (-2**53 < value < 2**53):
        return default
    return float(value)


def _worker_int(value: object, default: int = 0) -> int:
    """Coerce a worker-supplied number, treating anything unusable as absent.

    A bare int() raises three different ways on values json.loads accepts:
    ValueError on NaN, OverflowError on Infinity, TypeError on null. Only the
    first is in the fleet handler's except tuple, so the other two escaped the
    WebSocket endpoint, and the first stranded the job in `running` because
    on_worker_message had already de-tracked it (issue #203).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    # isfinite() converts an int to float first, so it raises OverflowError on a
    # big one. json.loads produces arbitrary-precision ints from ordinary JSON,
    # which makes that more reachable than the NaN this guard was written for.
    if isinstance(value, float) and not math.isfinite(value):
        return default
    number = int(value)
    # gpu_ms and the asset dimensions are int4; a unit mixup in a worker is
    # enough to overflow one, and the commit below is not inside a try.
    return number if -2**31 <= number < 2**31 else default


def publish(job_id: uuid.UUID, event: dict) -> None:
    event = {"job_id": str(job_id), **event}
    for queue in subscribers.get(job_id, []):
        queue.put_nowait(event)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    params: dict = Field(default_factory=dict)
    source_asset_id: uuid.UUID | None = None


def generation_download_name(
    job: Job,
    asset: Asset,
    position: int | None = None,
) -> str:
    return _generation_download_name(
        job.model_id,
        job.params,
        job.created_at,
        asset.storage_key,
        asset.mime,
        position,
    )


def _generation_download_name(
    model_id: str,
    params: dict,
    created_at: datetime,
    storage_key: str,
    mime: str,
    position: int | None = None,
) -> str:
    prompt = params.get("prompt")
    source = prompt if isinstance(prompt, str) and prompt.strip() else model_id
    ascii_source = (
        unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii").lower()
    )
    words = re.findall(r"[a-z0-9]+", ascii_source)[:DOWNLOAD_SLUG_MAX_WORDS]
    slug = "-".join(words)[:DOWNLOAD_SLUG_MAX_LENGTH].strip("-")
    if not slug:
        slug = "generation"
    timestamp = created_at.strftime("%Y%m%d-%H%M%S")
    filename = storage_key.rsplit("/", 1)[-1]
    _, separator, extension = filename.rpartition(".")
    if not separator or not extension:
        extension = mime.rpartition("/")[2]
        if re.fullmatch(r"[a-z0-9]+", extension.lower()) is None:
            extension = "bin"
    extension = extension.lower()
    position_suffix = f"-{position}" if position is not None else ""
    return f"potocolom-{timestamp}-{slug}{position_suffix}.{extension}"


@router.post("/api/v1/generations", status_code=202)
async def create_generation(
    request: GenerationRequest,
    user: User = Depends(require_role("member")),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    manifest = registry.for_jobs().get(request.model_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="unknown model")
    if error := validate_params(manifest, request.params):
        raise HTTPException(status_code=422, detail=f"params: {error}")
    source_asset_id = request.source_asset_id
    caps = set(manifest.capabilities)
    if "upscale" in caps and source_asset_id is None:
        raise HTTPException(status_code=422, detail="upscale requires source_asset_id")
    # A prompt-only request still implies a capability, and capability
    # narrowing makes the gap reachable: without this, a studio-narrowed
    # model (issue #268) queues jobs its worker refuses.
    if source_asset_id is None and "text_to_image" not in caps:
        raise HTTPException(status_code=422, detail="model does not support text_to_image")
    if source_asset_id is not None:
        source = await session.get(Asset, source_asset_id)
        if source is None or source.user_id != user.id:
            raise HTTPException(status_code=404, detail="unknown source asset")
        if source.storage_key.endswith("-thumb.webp"):
            raise HTTPException(status_code=422, detail="source asset cannot be a thumbnail")
        if "image_to_image" not in caps and "upscale" not in caps:
            raise HTTPException(
                status_code=422,
                detail="model does not support image_to_image or upscale",
            )
    # The model row may be missing if the worker registered while the database
    # was down; upserting here keeps the foreign key satisfied either way.
    # The row records what the model can do, not what the studio chooses to
    # offer: persist the unnarrowed manifest, since usage_events and
    # job-history classification read that row. The worker can disconnect
    # between the for_jobs() read above and this second registry read; when
    # it does, available() no longer carries the model and the fallback is
    # the narrowed copy. Writing the narrowed list is deliberate, not
    # silent: the job cannot be dispatched in that state anyway, so the row
    # exists only to satisfy the foreign key.
    persisted = registry.available().get(request.model_id)
    if persisted is None:
        logger.warning(
            "model %s left the registry while its job was being created; "
            "writing the models row from the narrowed copy",
            request.model_id,
        )
        persisted = manifest
    await registry.persist_manifests([persisted])
    job = Job(user_id=user.id, model_id=request.model_id, params=request.params,
              source_asset_id=source_asset_id)
    session.add(job)
    await session.commit()
    await queues.push(JOB_QUEUE, str(job.id), TIER_DEFAULT)
    return {"job_id": str(job.id)}


async def serialize_jobs(session: AsyncSession, jobs: list[Job]) -> list[dict]:
    assets: dict[uuid.UUID, list[Asset]] = {}
    thumbs_by_parent: dict[uuid.UUID, Asset] = {}
    jobs_with_derivatives: set[uuid.UUID] = set()
    if jobs:
        job_ids = [job.id for job in jobs]
        owner_job = aliased(Job)
        rows = await session.execute(
            select(Asset)
            .join(owner_job, owner_job.id == Asset.job_id)
            .where(Asset.job_id.in_(job_ids), Asset.user_id == owner_job.user_id)
        )
        for asset in rows.scalars():
            if asset.job_id is not None:
                assets.setdefault(asset.job_id, []).append(asset)
            if asset.parent_asset_id is not None and asset.storage_key.endswith("-thumb.webp"):
                thumbs_by_parent[asset.parent_asset_id] = asset
        child_job = aliased(Job)
        derivative_rows = await session.execute(
            select(Asset.job_id)
            .join(owner_job, owner_job.id == Asset.job_id)
            .where(
                Asset.job_id.in_(job_ids),
                Asset.user_id == owner_job.user_id,
                Asset.storage_key.not_like("%-thumb.webp"),
                select(child_job.id)
                .where(
                    child_job.source_asset_id == Asset.id,
                    child_job.user_id == owner_job.user_id,
                )
                .exists(),
            )
            .distinct()
        )
        jobs_with_derivatives = {
            job_id for job_id in derivative_rows.scalars() if job_id is not None
        }
    storage = get_storage()
    now = datetime.now(timezone.utc)
    # Keep expired masters for expired_favorite, but number only the assets the
    # client can see.
    all_masters = {
        job.id: [
            asset
            for asset in assets.get(job.id, [])
            if not asset.storage_key.endswith("-thumb.webp")
        ]
        for job in jobs
    }
    visible_masters = {
        job.id: [
            asset
            for asset in all_masters[job.id]
            if asset.expires_at is None or asset.expires_at > now
        ]
        for job in jobs
    }
    return [
        {
            "id": str(job.id),
            "model_id": job.model_id,
            "source_asset_id": str(job.source_asset_id) if job.source_asset_id else None,
            "has_derivatives": job.id in jobs_with_derivatives,
            "params": job.params,
            "state": job.state,
            "attempt": job.attempt,
            "progress": live_progress.get(job.id) if job.state == "running" else None,
            "gpu_ms": job.gpu_ms,
            "input_fetch_ms": job.input_fetch_ms,
            "load_ms": job.load_ms,
            "postprocess_ms": job.postprocess_ms,
            "failure_reason": job.failure_reason,
            "starred_at": job.starred_at.isoformat() if job.starred_at else None,
            "created_at": job.created_at.isoformat(),
            "dispatched_at": job.dispatched_at.isoformat() if job.dispatched_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "assets": [
                {
                    "id": str(asset.id),
                    "url": await storage.url(asset.storage_key),
                    "download_url": await storage.url(
                        asset.storage_key,
                        download_name=generation_download_name(
                            job,
                            asset,
                            position if len(visible_masters[job.id]) > 1 else None,
                        ),
                    ),
                    "thumbnail_url": await storage.url(thumb.storage_key)
                    if (thumb := thumbs_by_parent.get(asset.id)) is not None else None,
                    "mime": asset.mime,
                    "width": asset.width,
                    "height": asset.height,
                }
                for position, asset in enumerate(visible_masters[job.id], start=1)
            ],
            "expired_favorite": bool(
                job.starred_at
                and all_masters[job.id]
                and not any(
                    asset.expires_at is None or asset.expires_at > now
                    for asset in all_masters[job.id]
                )
            ),
        }
        for job in jobs
    ]


@router.get("/api/v1/generations")
async def list_generations(
    limit: int = 50,
    cursor: uuid.UUID | None = None,
    state: Literal["queued", "running", "succeeded", "failed"] | None = Query(default=None),
    starred: bool | None = Query(default=None),
    roots_only: bool | None = Query(default=None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> list[dict]:
    query = select(Job).where(Job.user_id == user.id)
    if state is not None:
        query = query.where(Job.state == state)
    if starred is not None:
        query = query.where(Job.starred_at.is_not(None) if starred else Job.starred_at.is_(None))
    if roots_only is not None:
        query = query.where(
            Job.source_asset_id.is_(None)
            if roots_only else Job.source_asset_id.is_not(None)
        )
    if cursor is not None:
        anchor = await session.get(Job, cursor)
        if anchor is None or anchor.user_id != user.id:
            raise HTTPException(status_code=404, detail="unknown cursor")
        if state is not None and anchor.state != state:
            raise HTTPException(status_code=404, detail="unknown cursor")
        if roots_only is not None and (anchor.source_asset_id is None) != roots_only:
            raise HTTPException(status_code=404, detail="unknown cursor")
        if starred is not None and (anchor.starred_at is not None) != starred:
            raise HTTPException(status_code=404, detail="unknown cursor")
        if starred is True:
            query = query.where(
                or_(
                    Job.starred_at < anchor.starred_at,
                    and_(Job.starred_at == anchor.starred_at, Job.id < anchor.id),
                )
            )
        else:
            query = query.where(
                or_(
                    Job.created_at < anchor.created_at,
                    and_(Job.created_at == anchor.created_at, Job.id < anchor.id),
                )
            )
    order = (
        (Job.starred_at.desc(), Job.id.desc())
        if starred is True else
        (Job.created_at.desc(), Job.id.desc())
    )
    rows = await session.execute(
        query.order_by(*order).limit(min(max(limit, 1), 200))
    )
    return await serialize_jobs(session, list(rows.scalars()))


async def owned_job(session: AsyncSession, job_id: uuid.UUID, user: User) -> Job:
    job = await session.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="no such generation")
    return job


async def serialize_lineage_entry(row: RowMapping, now: datetime) -> dict:
    missing = row["expires_at"] is not None and row["expires_at"] <= now
    thumbnail_missing = (
        row["thumbnail_expires_at"] is not None
        and row["thumbnail_expires_at"] <= now
    )
    thumbnail_url = None
    if not missing and not thumbnail_missing and row["thumbnail_storage_key"] is not None:
        thumbnail_url = await get_storage().url(row["thumbnail_storage_key"])
    capabilities = set(row["capabilities"] or [])
    action = (
        "upload" if row["job_id"] is None else
        "generate" if row["source_asset_id"] is None else
        "upscale" if "upscale" in capabilities else
        "image_to_image"
    )
    return {
        "job_id": str(row["job_id"]) if row["job_id"] is not None else None,
        "asset_id": str(row["asset_id"]),
        "action": action,
        "model_id": row["model_id"],
        "created_at": row["created_at"].isoformat(),
        "state": row["state"],
        "thumbnail_url": thumbnail_url,
        "missing": missing,
    }


@router.get("/api/v1/generations/{job_id}/subtree")
async def generation_subtree(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    params = {
        "job_id": job_id,
        "user_id": user.id,
        "max_depth": LINEAGE_SUBTREE_MAX_DEPTH,
        "max_nodes": LINEAGE_SUBTREE_MAX_NODES,
    }
    result = await session.execute(text("""
        WITH RECURSIVE walk AS (
            SELECT
                ARRAY[root.id]::uuid[] AS pending_job_ids,
                ARRAY[0]::integer[] AS pending_depths,
                ARRAY[]::uuid[] AS result_job_ids,
                ARRAY[]::integer[] AS result_depths,
                FALSE AS truncated,
                0::integer AS hidden_count_lower_bound
            FROM jobs AS root
            WHERE root.id = :job_id
                AND root.user_id = :user_id
                AND EXISTS (
                    SELECT 1
                    FROM assets AS root_asset
                    WHERE root_asset.job_id = root.id
                        AND root_asset.user_id = :user_id
                        AND root_asset.storage_key NOT LIKE '%-thumb.webp'
                )

            UNION ALL

            SELECT
                current.rest_job_ids || accepted.job_ids,
                current.rest_depths || accepted.depths,
                walk.result_job_ids || ARRAY[current.job_id],
                walk.result_depths || ARRAY[current.depth],
                walk.truncated OR cardinality(candidates.job_ids) > slots.capacity,
                walk.hidden_count_lower_bound + CASE
                    WHEN cardinality(candidates.job_ids) > slots.capacity THEN 1
                    ELSE 0
                END
            FROM walk
            CROSS JOIN LATERAL (
                SELECT
                    walk.pending_job_ids[1] AS job_id,
                    walk.pending_depths[1] AS depth,
                    walk.pending_job_ids[2:cardinality(walk.pending_job_ids)] AS rest_job_ids,
                    walk.pending_depths[2:cardinality(walk.pending_depths)] AS rest_depths
            ) AS current
            CROSS JOIN LATERAL (
                SELECT CASE
                    WHEN current.depth < :max_depth THEN greatest(
                        0,
                        :max_nodes
                            - cardinality(walk.result_job_ids)
                            - cardinality(walk.pending_job_ids)
                    )
                    ELSE 0
                END AS capacity
            ) AS slots
            CROSS JOIN LATERAL (
                SELECT COALESCE(
                    array_agg(candidate.id ORDER BY candidate.created_at, candidate.id),
                    ARRAY[]::uuid[]
                ) AS job_ids
                FROM (
                    SELECT child.id, child.created_at
                    FROM assets AS output
                    JOIN jobs AS child
                        ON child.source_asset_id = output.id
                        AND child.user_id = :user_id
                    WHERE output.job_id = current.job_id
                        AND output.user_id = :user_id
                        AND output.storage_key NOT LIKE '%-thumb.webp'
                        AND NOT (
                            child.id = ANY(
                                walk.result_job_ids || walk.pending_job_ids
                            )
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM assets AS child_asset
                            WHERE child_asset.job_id = child.id
                                AND child_asset.user_id = :user_id
                                AND child_asset.storage_key NOT LIKE '%-thumb.webp'
                        )
                    ORDER BY child.created_at, child.id
                    LIMIT slots.capacity + 1
                ) AS candidate
            ) AS candidates
            CROSS JOIN LATERAL (
                SELECT
                    candidates.job_ids[1:slots.capacity] AS job_ids,
                    array_fill(
                        current.depth + 1,
                        ARRAY[least(cardinality(candidates.job_ids), slots.capacity)]
                    ) AS depths
            ) AS accepted
            WHERE cardinality(walk.pending_job_ids) > 0
        ), final_state AS (
            SELECT *
            FROM walk
            WHERE cardinality(pending_job_ids) = 0
            ORDER BY cardinality(result_job_ids) DESC
            LIMIT 1
        ), ordered_jobs AS (
            SELECT expanded.job_id, expanded.depth, expanded.position
            FROM final_state
            CROSS JOIN LATERAL unnest(
                final_state.result_job_ids,
                final_state.result_depths
            ) WITH ORDINALITY AS expanded(job_id, depth, position)
        )
        SELECT
            job.id AS job_id,
            master.id AS asset_id,
            job.model_id,
            job.source_asset_id,
            source.job_id AS parent_job_id,
            job.params,
            job.state,
            job.attempt,
            job.gpu_ms,
            job.input_fetch_ms,
            job.load_ms,
            job.postprocess_ms,
            job.failure_reason,
            job.starred_at,
            job.created_at,
            job.dispatched_at,
            job.finished_at,
            model.capabilities,
            master.storage_key,
            master.mime,
            master.width,
            master.height,
            master.expires_at,
            ARRAY(
                SELECT output.id
                FROM assets AS output
                WHERE output.job_id = job.id
                    AND output.user_id = :user_id
                    AND output.storage_key NOT LIKE '%-thumb.webp'
                ORDER BY output.id
            ) AS output_asset_ids,
            thumbnail.storage_key AS thumbnail_storage_key,
            thumbnail.expires_at AS thumbnail_expires_at,
            EXISTS (
                SELECT 1
                FROM assets AS derivative_source
                JOIN jobs AS derivative
                    ON derivative.source_asset_id = derivative_source.id
                    AND derivative.user_id = :user_id
                WHERE derivative_source.job_id = job.id
                    AND derivative_source.user_id = :user_id
                    AND derivative_source.storage_key NOT LIKE '%-thumb.webp'
            ) AS has_derivatives,
            final_state.truncated,
            final_state.hidden_count_lower_bound
        FROM ordered_jobs
        JOIN final_state ON TRUE
        JOIN jobs AS job
            ON job.id = ordered_jobs.job_id
            AND job.user_id = :user_id
        LEFT JOIN models AS model ON model.id = job.model_id
        LEFT JOIN assets AS source
            ON source.id = job.source_asset_id
            AND source.user_id = :user_id
        JOIN LATERAL (
            SELECT asset.*
            FROM assets AS asset
            WHERE asset.job_id = job.id
                AND asset.user_id = :user_id
                AND asset.storage_key NOT LIKE '%-thumb.webp'
            ORDER BY asset.id
            LIMIT 1
        ) AS master ON TRUE
        LEFT JOIN LATERAL (
            SELECT thumb.storage_key, thumb.expires_at
            FROM assets AS thumb
            WHERE thumb.parent_asset_id = master.id
                AND thumb.user_id = :user_id
                AND thumb.storage_key LIKE '%-thumb.webp'
            ORDER BY thumb.id
            LIMIT 1
        ) AS thumbnail ON TRUE
        ORDER BY ordered_jobs.position
    """), params)
    rows = list(result.mappings())
    if not rows:
        raise HTTPException(status_code=404, detail="no such generation")

    now = datetime.now(timezone.utc)
    storage = get_storage()
    nodes = []
    for row in rows:
        expired = row["expires_at"] is not None and row["expires_at"] <= now
        asset = None
        if not expired:
            asset = {
                "id": str(row["asset_id"]),
                "url": await storage.url(row["storage_key"]),
                "download_url": await storage.url(
                    row["storage_key"],
                    download_name=_generation_download_name(
                        row["model_id"],
                        row["params"],
                        row["created_at"],
                        row["storage_key"],
                        row["mime"],
                    ),
                ),
                "thumbnail_url": (
                    await storage.url(row["thumbnail_storage_key"])
                    if row["thumbnail_storage_key"] is not None
                    and (
                        row["thumbnail_expires_at"] is None
                        or row["thumbnail_expires_at"] > now
                    )
                    else None
                ),
                "mime": row["mime"],
                "width": row["width"],
                "height": row["height"],
            }
        nodes.append({
            "parent_job_id": (
                str(row["parent_job_id"])
                if row["parent_job_id"] is not None else None
            ),
            "output_asset_ids": [str(asset_id) for asset_id in row["output_asset_ids"]],
            "entry": await serialize_lineage_entry(row, now),
            "generation": {
                "id": str(row["job_id"]),
                "model_id": row["model_id"],
                "source_asset_id": (
                    str(row["source_asset_id"])
                    if row["source_asset_id"] is not None else None
                ),
                "has_derivatives": row["has_derivatives"],
                "params": row["params"],
                "state": row["state"],
                "attempt": row["attempt"],
                "progress": (
                    live_progress.get(row["job_id"])
                    if row["state"] == "running" else None
                ),
                "gpu_ms": row["gpu_ms"],
                "input_fetch_ms": row["input_fetch_ms"],
                "load_ms": row["load_ms"],
                "postprocess_ms": row["postprocess_ms"],
                "failure_reason": row["failure_reason"],
                "starred_at": (
                    row["starred_at"].isoformat()
                    if row["starred_at"] is not None else None
                ),
                "created_at": row["created_at"].isoformat(),
                "dispatched_at": (
                    row["dispatched_at"].isoformat()
                    if row["dispatched_at"] is not None else None
                ),
                "finished_at": (
                    row["finished_at"].isoformat()
                    if row["finished_at"] is not None else None
                ),
                "assets": [asset] if asset is not None else [],
                "expired_favorite": bool(row["starred_at"] and expired),
            },
        })
    return {
        "nodes": nodes,
        "truncated": rows[0]["truncated"],
        "remaining_count_lower_bound": rows[0]["hidden_count_lower_bound"],
        "max_depth": LINEAGE_SUBTREE_MAX_DEPTH,
        "max_nodes": LINEAGE_SUBTREE_MAX_NODES,
    }


@router.get("/api/v1/generations/{job_id}/lineage")
async def generation_lineage(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    await owned_job(session, job_id, user)
    params = {
        "job_id": job_id,
        "user_id": user.id,
        "max_depth": LINEAGE_SUBTREE_MAX_DEPTH,
    }
    ancestor_result = await session.execute(text("""
        WITH RECURSIVE ancestor_walk AS (
            SELECT
                source.id AS asset_id,
                source.job_id,
                parent.model_id,
                parent.source_asset_id,
                parent.created_at AS job_created_at,
                current.created_at AS reference_created_at,
                parent.state,
                model.capabilities,
                source.expires_at,
                1 AS depth,
                ARRAY[source.id]::uuid[] AS visited
            FROM jobs AS current
            JOIN assets AS source ON source.id = current.source_asset_id
            LEFT JOIN jobs AS parent
                ON parent.id = source.job_id
                AND parent.user_id = :user_id
            LEFT JOIN models AS model ON model.id = parent.model_id
            WHERE current.id = :job_id
                AND current.user_id = :user_id
                AND source.user_id = :user_id
                AND source.storage_key NOT LIKE '%-thumb.webp'

            UNION ALL

            SELECT
                source.id,
                source.job_id,
                parent.model_id,
                parent.source_asset_id,
                parent.created_at,
                ancestor_walk.job_created_at,
                parent.state,
                model.capabilities,
                source.expires_at,
                ancestor_walk.depth + 1,
                ancestor_walk.visited || ARRAY[source.id]
            FROM ancestor_walk
            JOIN assets AS source ON source.id = ancestor_walk.source_asset_id
            LEFT JOIN jobs AS parent
                ON parent.id = source.job_id
                AND parent.user_id = :user_id
            LEFT JOIN models AS model ON model.id = parent.model_id
            WHERE ancestor_walk.depth < :max_depth
                AND source.user_id = :user_id
                AND source.storage_key NOT LIKE '%-thumb.webp'
                AND NOT (source.id = ANY(ancestor_walk.visited))
        )
        SELECT
            ancestor_walk.asset_id,
            ancestor_walk.job_id,
            ancestor_walk.model_id,
            ancestor_walk.source_asset_id,
            COALESCE(
                ancestor_walk.job_created_at,
                ancestor_walk.reference_created_at
            ) AS created_at,
            ancestor_walk.state,
            ancestor_walk.capabilities,
            ancestor_walk.expires_at,
            thumbnail.storage_key AS thumbnail_storage_key,
            thumbnail.expires_at AS thumbnail_expires_at
        FROM ancestor_walk
        LEFT JOIN LATERAL (
            SELECT storage_key, expires_at
            FROM assets
            WHERE parent_asset_id = ancestor_walk.asset_id
                AND user_id = :user_id
                AND storage_key LIKE '%-thumb.webp'
            ORDER BY id
            LIMIT 1
        ) AS thumbnail ON TRUE
        ORDER BY ancestor_walk.depth DESC
    """), params)

    child_result = await session.execute(text("""
        SELECT
            child.id AS job_id,
            master.id AS asset_id,
            child.model_id,
            child.source_asset_id,
            child.created_at,
            child.state,
            model.capabilities,
            master.expires_at,
            thumbnail.storage_key AS thumbnail_storage_key,
            thumbnail.expires_at AS thumbnail_expires_at
        FROM jobs AS current
        JOIN assets AS current_asset
            ON current_asset.job_id = current.id
            AND current_asset.user_id = :user_id
            AND current_asset.storage_key NOT LIKE '%-thumb.webp'
        JOIN jobs AS child
            ON child.source_asset_id = current_asset.id
            AND child.user_id = :user_id
        JOIN assets AS master
            ON master.job_id = child.id
            AND master.user_id = :user_id
            AND master.storage_key NOT LIKE '%-thumb.webp'
        JOIN models AS model ON model.id = child.model_id
        LEFT JOIN LATERAL (
            SELECT storage_key, expires_at
            FROM assets
            WHERE parent_asset_id = master.id
                AND user_id = :user_id
                AND storage_key LIKE '%-thumb.webp'
            ORDER BY id
            LIMIT 1
        ) AS thumbnail ON TRUE
        WHERE current.id = :job_id
            AND current.user_id = :user_id
        ORDER BY child.created_at, child.id
    """), params)

    descendant_result = await session.execute(text("""
        WITH RECURSIVE descendants AS (
            SELECT
                child.id AS job_id,
                1 AS depth,
                ARRAY[current.id, child.id]::uuid[] AS visited
            FROM jobs AS current
            JOIN assets AS current_asset
                ON current_asset.job_id = current.id
                AND current_asset.user_id = :user_id
                AND current_asset.storage_key NOT LIKE '%-thumb.webp'
            JOIN jobs AS child
                ON child.source_asset_id = current_asset.id
                AND child.user_id = :user_id
            WHERE current.id = :job_id
                AND current.user_id = :user_id
                AND child.id != current.id

            UNION ALL

            SELECT
                child.id,
                descendants.depth + 1,
                descendants.visited || ARRAY[child.id]
            FROM descendants
            JOIN assets AS output
                ON output.job_id = descendants.job_id
                AND output.user_id = :user_id
                AND output.storage_key NOT LIKE '%-thumb.webp'
            JOIN jobs AS child
                ON child.source_asset_id = output.id
                AND child.user_id = :user_id
            WHERE descendants.depth < :max_depth
                AND NOT (child.id = ANY(descendants.visited))
        )
        SELECT
            count(DISTINCT job_id) AS descendant_count,
            EXISTS (
                SELECT 1
                FROM descendants
                JOIN assets AS output
                    ON output.job_id = descendants.job_id
                    AND output.user_id = :user_id
                    AND output.storage_key NOT LIKE '%-thumb.webp'
                JOIN jobs AS child
                    ON child.source_asset_id = output.id
                    AND child.user_id = :user_id
                WHERE descendants.depth = :max_depth
                    AND NOT (child.id = ANY(descendants.visited))
            ) AS descendants_truncated
        FROM descendants
    """), params)
    descendant = descendant_result.mappings().one()
    now = datetime.now(timezone.utc)
    return {
        "ancestors": [
            await serialize_lineage_entry(row, now)
            for row in ancestor_result.mappings()
        ],
        "children": [
            await serialize_lineage_entry(row, now)
            for row in child_result.mappings()
        ],
        "descendant_count": int(descendant["descendant_count"] or 0),
        "descendants_truncated": descendant["descendants_truncated"],
    }


@router.get("/api/v1/generations/{job_id}")
async def get_generation(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    job = await owned_job(session, job_id, user)
    return (await serialize_jobs(session, [job]))[0]


async def _set_star(
    session: AsyncSession, job_id: uuid.UUID, user: User, value,
) -> None:
    # Ownership through the shared helper, so the 404 matches every other
    # generations route; the UPDATE then stays a single idempotent statement
    # with the timestamp decided server side.
    await owned_job(session, job_id, user)
    await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.user_id == user.id)
        .values(starred_at=value)
    )
    await session.commit()


@router.post("/api/v1/generations/{job_id}/star", status_code=204)
async def star_generation(
    job_id: uuid.UUID,
    user: User = Depends(require_role("member")),
    session: AsyncSession = Depends(db.get_session),
) -> Response:
    await _set_star(session, job_id, user, func.coalesce(Job.starred_at, func.now()))
    return Response(status_code=204)


@router.delete("/api/v1/generations/{job_id}/star", status_code=204)
async def unstar_generation(
    job_id: uuid.UUID,
    user: User = Depends(require_role("member")),
    session: AsyncSession = Depends(db.get_session),
) -> Response:
    await _set_star(session, job_id, user, None)
    return Response(status_code=204)


@router.get("/api/v1/generations/{job_id}/events")
async def generation_events(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> StreamingResponse:
    job = await owned_job(session, job_id, user)
    # Subscribe before snapshotting so nothing falls between the two.
    queue: asyncio.Queue = asyncio.Queue()
    subscribers.setdefault(job_id, []).append(queue)
    snapshot = (await serialize_jobs(session, [job]))[0]

    async def stream() -> AsyncIterator[str]:
        try:
            yield f"data: {json.dumps({'job_id': str(job_id), 'state': snapshot['state']})}\n\n"
            if snapshot["state"] in TERMINAL_STATES:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # SSE comment: an on-demand model load can stay silent for
                    # minutes, and silent streams get killed by clients and
                    # proxies alike.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("state") in TERMINAL_STATES:
                    return
        finally:
            listeners = subscribers.get(job_id, [])
            if queue in listeners:
                listeners.remove(queue)
            if not listeners:
                subscribers.pop(job_id, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def job_dispatch_depth(worker: realtime.Worker) -> int:
    # Sessions-first: while a realtime slot is live, do not stack a second
    # queued job behind the one already waiting on the GPU lock.
    if worker.slots_in_use > 0:
        return 1
    return JOB_DISPATCH_DEPTH


def pick_job_worker(model_id: str) -> realtime.Worker | None:
    candidates = [
        worker for worker in realtime.workers.values()
        if model_id in worker.models
        and worker.jobs_in_flight < job_dispatch_depth(worker)
    ]
    return min(candidates, key=lambda worker: worker.jobs_in_flight, default=None)


def release_job_slot(worker: realtime.Worker) -> None:
    worker.jobs_in_flight = max(0, worker.jobs_in_flight - 1)


async def dispatch_loop() -> None:
    while True:
        await asyncio.sleep(DISPATCH_INTERVAL)
        try:
            await dispatch_step()
        except Exception:
            logger.exception("dispatch step failed")


async def sweep_stalled_jobs() -> None:
    """Requeue or fail jobs whose worker stopped reporting progress (issue #61)."""
    now = time.monotonic()
    stall = get_settings().job_stall_seconds
    for job_id in list(inflight):
        entry = inflight.pop(job_id, None)
        if entry is None:
            continue
        # Missing stamp means unsweepable; treat as stalled (issue #61).
        last = last_progress_at.get(job_id, 0.0)
        if now - last < stall:
            inflight[job_id] = entry
            continue
        live_progress.pop(job_id, None)
        last_progress_at.pop(job_id, None)
        release_job_slot(entry.worker)
        try:
            await requeue_or_fail(job_id, f"no progress for {stall:.0f}s")
        except Exception:
            # De-tracked above; hand the job to the lost_jobs conduit so the
            # next dispatch step retries the requeue instead of stranding it.
            lost_jobs.append(job_id)
            raise


async def dispatch_step() -> None:
    if db.session_factory is None:
        return
    while lost_jobs:
        await requeue_or_fail(lost_jobs.pop(0), "worker disconnected")
    await sweep_stalled_jobs()
    while True:
        job_id = await queues.pop(JOB_QUEUE)
        if job_id is None:
            return
        job_uuid = uuid.UUID(job_id)
        try:
            dispatched = await dispatch(job_uuid)
        except Exception:
            if job_uuid in inflight:
                # Worker may already be running; requeue would double-dispatch.
                logger.exception(
                    "dispatch failed after worker send for job %s; skipping requeue",
                    job_id,
                )
                return
            await queues.push(JOB_QUEUE, job_id, TIER_DEFAULT)  # never lose the entry
            raise
        if not dispatched:
            # No capacity for this job's model right now; back in the queue
            # and try again next step.
            await queues.push(JOB_QUEUE, job_id, TIER_DEFAULT)
            return


async def locked_job(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """Job state transitions interleave across await points (dispatch commit
    versus a worker dying mid-commit), so every writer takes the row lock and
    the last committed state is decided by PostgreSQL, not by task timing."""
    result = await session.execute(select(Job).where(Job.id == job_id).with_for_update())
    return result.scalar_one_or_none()


async def dispatch(job_id: uuid.UUID) -> bool:
    assert db.session_factory is not None
    async with db.session_factory() as session:
        job = await locked_job(session, job_id)
        if job is None or job.state != "queued":
            return True  # stale queue entry; drop it
        worker = pick_job_worker(job.model_id)
        if worker is None:
            return False
        storage_key, thumb_storage_key = storage_keys_for_attempt(
            job.user_id, job.id, job.attempt
        )
        target = await get_storage().upload_target(storage_key)
        thumb_target = await get_storage().upload_target(thumb_storage_key)
        # Bookkeeping before the send: if the worker dies from here on, its
        # disconnect handler finds the inflight entry and requeues the job.
        worker.jobs_in_flight += 1
        inflight[job.id] = InFlight(
            worker=worker, storage_key=storage_key, thumb_storage_key=thumb_storage_key,
            user_id=job.user_id, attempt=job.attempt,
        )
        last_progress_at[job_id] = time.monotonic()
        job.state = "running"
        job.dispatched_at = datetime.now(timezone.utc)
        dispatch_msg: dict = {
            "type": "dispatch_job",
            "job_id": str(job.id),
            "model_id": job.model_id,
            "params": job.params,
            "upload": {"url": target.url, "headers": target.headers},
            "thumb_upload": {"url": thumb_target.url, "headers": thumb_target.headers},
        }
        if job.source_asset_id is not None:
            source = await session.get(Asset, job.source_asset_id)
            if source is None:
                inflight.pop(job.id, None)
                release_job_slot(worker)
                job.state = "failed"
                await session.commit()
                publish(job_id, {"state": "failed", "reason": "source asset not found"})
                logger.warning("job %s failed: source asset not found", job_id)
                return True
            dispatch_msg["input"] = {
                "url": await get_storage().worker_fetch_url(source.storage_key),
            }
        try:
            await worker.ws.send_json(dispatch_msg)
        except Exception:  # the socket is dead however the transport spells it
            if realtime.workers.get(worker.id) is worker:
                del realtime.workers[worker.id]  # what the reaper would conclude
            if inflight.pop(job.id, None) is None:
                return True  # the disconnect handler beat us to it and requeued
            last_progress_at.pop(job.id, None)
            release_job_slot(worker)
            return False  # session rolls back, the job stays queued
        await session.commit()
    publish(job_id, {"state": "running"})
    logger.info("job %s dispatched to worker %s", job_id, worker.id)
    return True


async def on_worker_message(worker: realtime.Worker, control: dict) -> None:
    job_id = realtime.peer_uuid(control["job_id"])
    # Only the worker recorded for the attempt may speak for the job: after a
    # stall requeue the old worker may still be connected and reporting.
    current = inflight.get(job_id)
    if current is None or current.worker is not worker:
        return  # stale report from a previous incarnation or attempt
    if control["type"] == "job_progress":
        # float() raises TypeError on the list or dict json.loads accepts, and
        # OverflowError on the arbitrary-precision int it also accepts.
        # NaN as the default routes every unusable shape into the finite check
        # below, so one branch logs and drops them all.
        progress = _worker_float(control.get("progress"), default=float("nan"))
        if not math.isfinite(progress):
            # Stored, it breaks every generation list and detail response that
            # carries it; published, it emits the non-standard NaN token into
            # the SSE stream (issue #203). Progress is display only, so drop it.
            logger.warning("worker %s sent non-finite progress for job %s; ignored",
                           worker.id, job_id)
            return
        live_progress[job_id] = progress
        last_progress_at[job_id] = time.monotonic()
        publish(job_id, {"state": "running", "progress": progress})
        return
    image_dimensions = (0, 0)
    if control["type"] == "job_done":
        gpu_ms = _worker_int(control.get("gpu_ms"))
        phase_ms = {
            field: _worker_int(control[field])
            for field in ("input_fetch_ms", "load_ms", "postprocess_ms")
            if control.get(field) is not None
        }
        # Convert claimed dimensions before touching inflight as well. The
        # object is authoritative below, but malformed peer fields must not
        # strand the running row if conversion ever rejects one.
        _worker_int(control.get("width"))
        _worker_int(control.get("height"))
        if db.session_factory is None:
            logger.warning("job %s finished on the worker but the database is unavailable",
                           job_id)
            return
        try:
            image = await get_storage().image_info(current.storage_key)
        except Exception:
            logger.exception("could not inspect output for job %s", job_id)
            return
        # Before either branch. image_info awaits a thread, so a stall requeue
        # can have replaced this attempt while its output was being inspected,
        # and a late verdict must not fail the row or delete the objects that
        # now belong to the attempt after it.
        if inflight.get(job_id) is not current:
            return
        if (image is None or image.size <= 0
                or image.content_type != "image/png"):
            entry = inflight.pop(job_id, None)
            if entry is None:
                return
            live_progress.pop(job_id, None)
            last_progress_at.pop(job_id, None)
            release_job_slot(worker)
            await mark_failed(job_id, "worker output was missing or invalid")
            await purge_attempt_blobs(entry.user_id, job_id, entry.attempt)
            return
        image_dimensions = (image.width, image.height)

    entry = inflight.pop(job_id, None)
    live_progress.pop(job_id, None)
    last_progress_at.pop(job_id, None)
    if entry is None:
        return  # finished concurrently
    release_job_slot(worker)
    if db.session_factory is None:
        logger.warning("job %s finished on the worker but the database is unavailable",
                       job_id)
        return
    if control["type"] == "job_done":
        async with db.session_factory() as session:
            job = await locked_job(session, job_id)
            if job is None:
                return
            job.state = "succeeded"
            job.gpu_ms = gpu_ms
            for field, value in phase_ms.items():
                setattr(job, field, value)
            job.finished_at = datetime.now(timezone.utc)
            width, height = image_dimensions
            full = Asset(
                user_id=entry.user_id,
                job_id=job_id,
                parent_asset_id=job.source_asset_id,
                storage_key=entry.storage_key,
                mime="image/png",
                width=width,
                height=height,
            )
            session.add(full)
            await session.flush()
            if control.get("has_thumbnail") is True:
                edge = THUMBNAIL_MAX_EDGE
                thumb_width = min(width, edge) if width else 0
                thumb_height = min(height, edge) if height else 0
                if width and height and max(width, height) > edge:
                    scale = edge / max(width, height)
                    thumb_width = int(width * scale)
                    thumb_height = int(height * scale)
                session.add(Asset(
                    user_id=entry.user_id,
                    job_id=job_id,
                    parent_asset_id=full.id,
                    storage_key=entry.thumb_storage_key,
                    mime="image/webp",
                    width=thumb_width,
                    height=thumb_height,
                ))
            await session.commit()
        orphans = []
        if control.get("has_thumbnail") is not True:
            # This attempt may have uploaded a thumbnail it did not report.
            orphans.append(entry.thumb_storage_key)
        # Attempts no longer share one key, so a retry leaves the earlier
        # attempt's blobs behind instead of overwriting them. Nothing else
        # collects them: the asset row only ever names the winning key.
        for earlier in range(1, entry.attempt):
            orphans.extend(storage_keys_for_attempt(entry.user_id, job_id, earlier))
        for orphan in orphans:
            try:
                await get_storage().delete(orphan)
            except Exception:
                logger.warning("could not remove orphaned blob %s for job %s", orphan,
                               job_id, exc_info=True)
        url = await get_storage().url(entry.storage_key)
        publish(job_id, {"state": "succeeded", "url": url})
        logger.info("job %s succeeded, gpu_ms=%s", job_id, control.get("gpu_ms"))
        from app import usage_events
        usage_events.schedule_job(job_id, control)
    else:
        reason = str(control.get("reason", "worker reported failure"))
        await mark_failed(job_id, reason)
        # A reported failure can still have uploaded: nothing downstream names
        # those objects, so this is their only collector.
        await purge_attempt_blobs(entry.user_id, job_id, entry.attempt)


async def purge_attempt_blobs(user_id: uuid.UUID, job_id: uuid.UUID, attempt: int) -> None:
    """Remove the objects of this attempt and every earlier one.

    Terminal paths are the only collector these have: no asset row names them,
    and the success path never runs. A worker can upload a master and a
    thumbnail and then report a failure, and on S3 nothing bounds what it
    uploaded.

    S3Storage.delete purges every version of the key, not just the current
    one, so this reclaims the storage rather than hiding it.
    """
    for earlier in range(1, attempt + 1):
        for key in storage_keys_for_attempt(user_id, job_id, earlier):
            try:
                await get_storage().delete(key)
            except Exception:
                # Warning, not debug: a missing object does not normally make
                # a delete raise, so this is a real cleanup failure such as a
                # denied permission, and nothing retries it.
                logger.warning("could not remove blob %s for job %s", key, job_id,
                               exc_info=True)


async def mark_failed(job_id: uuid.UUID, reason: str) -> None:
    assert db.session_factory is not None
    async with db.session_factory() as session:
        job = await locked_job(session, job_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        job.state = "failed"
        job.failure_reason = reason
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
    publish(job_id, {"state": "failed", "reason": reason})
    logger.warning("job %s failed: %s", job_id, reason)


async def requeue_or_fail(job_id: uuid.UUID, reason: str) -> None:
    """Retry once, then fail visibly (docs/decisions.md, job failures)."""
    assert db.session_factory is not None
    async with db.session_factory() as session:
        job = await locked_job(session, job_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        if job.state == "queued":
            await queues.push(JOB_QUEUE, str(job_id), TIER_DEFAULT)
            logger.info("job %s requeued after %s (never left queued)", job_id, reason)
            return
        if job.attempt == 1:
            job.attempt = 2
            job.state = "queued"
            await session.commit()
            await queues.push(JOB_QUEUE, str(job_id), TIER_DEFAULT)
            publish(job_id, {"state": "queued", "attempt": 2})
            logger.info("job %s requeued after %s", job_id, reason)
            return
    await mark_failed(job_id, reason)


def on_worker_lost(worker: realtime.Worker) -> None:
    """Called from the disconnecting worker's own handler, whose task dies
    with the connection; the requeue work is handed to the dispatch loop,
    which outlives it."""
    for job_id, entry in list(inflight.items()):
        if entry.worker is worker:
            del inflight[job_id]
            live_progress.pop(job_id, None)
            last_progress_at.pop(job_id, None)
            lost_jobs.append(job_id)


async def recover() -> None:
    """Rebuild the queue from job rows after a restart; running jobs lost
    their worker reply with the process, so they get their retry."""
    assert db.session_factory is not None
    async with db.session_factory() as session:
        rows = await session.execute(
            select(Job).where(Job.state.in_(["queued", "running"])).order_by(Job.created_at)
        )
        pending = list(rows.scalars())
    for job in pending:
        if job.state == "running":
            await requeue_or_fail(job.id, "restart while running")
        else:
            await queues.push(JOB_QUEUE, str(job.id), TIER_DEFAULT)
