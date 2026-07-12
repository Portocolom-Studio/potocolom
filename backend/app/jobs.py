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
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count
from typing import Protocol

import jsonschema
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, realtime, registry
from app.auth import current_user
from app.storage import get_storage
from app.tables import Asset, Job, User

logger = logging.getLogger("potocolom.jobs")

router = APIRouter()

JOB_QUEUE = "queue:jobs"
TIER_DEFAULT = 1  # 0 resuming, 1 paid, 2 trial; one tier until billing exists
DISPATCH_INTERVAL = 0.1  # the scheduler step cadence (docs/blueprint.md)

TERMINAL_STATES = ("succeeded", "failed")


class Queues(Protocol):
    async def push(self, queue: str, id: str, tier: int) -> None: ...

    async def pop(self, queue: str) -> str | None: ...

    async def position(self, queue: str, id: str) -> int | None: ...


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

    async def position(self, queue: str, id: str) -> int | None:
        entries = sorted(self._heaps.get(queue, []))
        for index, (_, _, entry_id) in enumerate(entries):
            if entry_id == id:
                return index
        return None


queues: Queues = InProcessQueues()


@dataclass
class InFlight:
    worker: realtime.Worker
    storage_key: str
    user_id: uuid.UUID


inflight: dict[uuid.UUID, InFlight] = {}
lost_jobs: list[uuid.UUID] = []  # drained by the dispatch loop

# Latest reported denoising fraction per running job. Transient by design:
# the job row is the source of truth for state, progress is display only.
live_progress: dict[uuid.UUID, float] = {}

# SSE subscribers per job; events are transient, the job row is the truth.
subscribers: dict[uuid.UUID, list[asyncio.Queue]] = {}


def publish(job_id: uuid.UUID, event: dict) -> None:
    event = {"job_id": str(job_id), **event}
    for queue in subscribers.get(job_id, []):
        queue.put_nowait(event)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    params: dict = {}


@router.post("/api/v1/generations", status_code=202)
async def create_generation(
    request: GenerationRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    manifest = registry.available().get(request.model_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="unknown model")
    try:
        jsonschema.validate(instance=request.params, schema=manifest.parameters)
    except jsonschema.ValidationError as error:
        raise HTTPException(status_code=422, detail=f"params: {error.message}") from error
    except jsonschema.SchemaError:
        logger.warning("model %s has an invalid parameter schema; accepting params unchecked",
                       manifest.id)
    # The model row may be missing if the worker registered while the database
    # was down; upserting here keeps the foreign key satisfied either way.
    await registry.persist_manifests([manifest])
    job = Job(user_id=user.id, model_id=request.model_id, params=request.params)
    session.add(job)
    await session.commit()
    await queues.push(JOB_QUEUE, str(job.id), TIER_DEFAULT)
    return {"job_id": str(job.id)}


async def serialize_jobs(session: AsyncSession, jobs: list[Job]) -> list[dict]:
    assets: dict[uuid.UUID, list[Asset]] = {}
    if jobs:
        rows = await session.execute(select(Asset).where(Asset.job_id.in_([j.id for j in jobs])))
        for asset in rows.scalars():
            if asset.job_id is not None:
                assets.setdefault(asset.job_id, []).append(asset)
    storage = get_storage()
    return [
        {
            "id": str(job.id),
            "model_id": job.model_id,
            "params": job.params,
            "state": job.state,
            "progress": live_progress.get(job.id) if job.state == "running" else None,
            "gpu_ms": job.gpu_ms,
            "created_at": job.created_at.isoformat(),
            "assets": [
                {
                    "id": str(asset.id),
                    "url": await storage.url(asset.storage_key),
                    "mime": asset.mime,
                    "width": asset.width,
                    "height": asset.height,
                }
                for asset in assets.get(job.id, [])
            ],
        }
        for job in jobs
    ]


@router.get("/api/v1/generations")
async def list_generations(
    limit: int = 50,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> list[dict]:
    rows = await session.execute(
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return await serialize_jobs(session, list(rows.scalars()))


async def owned_job(session: AsyncSession, job_id: uuid.UUID, user: User) -> Job:
    job = await session.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="no such generation")
    return job


@router.get("/api/v1/generations/{job_id}")
async def get_generation(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(db.get_session),
) -> dict:
    job = await owned_job(session, job_id, user)
    return (await serialize_jobs(session, [job]))[0]


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

    return StreamingResponse(stream(), media_type="text/event-stream")


def pick_job_worker(model_id: str) -> realtime.Worker | None:
    for worker in realtime.workers.values():
        if model_id in worker.models and not worker.job_busy:
            return worker
    return None


async def dispatch_loop() -> None:
    while True:
        await asyncio.sleep(DISPATCH_INTERVAL)
        try:
            await dispatch_step()
        except Exception:
            logger.exception("dispatch step failed")


async def dispatch_step() -> None:
    if db.session_factory is None:
        return
    while lost_jobs:
        await requeue_or_fail(lost_jobs.pop(0), "worker disconnected")
    while True:
        job_id = await queues.pop(JOB_QUEUE)
        if job_id is None:
            return
        try:
            dispatched = await dispatch(uuid.UUID(job_id))
        except Exception:
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
        storage_key = f"{job.user_id}/{job.id}.webp"
        target = await get_storage().upload_target(storage_key)
        # Bookkeeping before the send: if the worker dies from here on, its
        # disconnect handler finds the inflight entry and requeues the job.
        worker.job_busy = True
        inflight[job.id] = InFlight(worker=worker, storage_key=storage_key, user_id=job.user_id)
        job.state = "running"
        try:
            await worker.ws.send_json({
                "type": "dispatch_job",
                "job_id": str(job.id),
                "model_id": job.model_id,
                "params": job.params,
                "upload": {"url": target.url, "headers": target.headers},
            })
        except Exception:  # the socket is dead however the transport spells it
            if realtime.workers.get(worker.id) is worker:
                del realtime.workers[worker.id]  # what the reaper would conclude
            if inflight.pop(job.id, None) is None:
                return True  # the disconnect handler beat us to it and requeued
            worker.job_busy = False
            return False  # session rolls back, the job stays queued
        await session.commit()
    publish(job_id, {"state": "running"})
    logger.info("job %s dispatched to worker %s", job_id, worker.id)
    return True


async def on_worker_message(worker: realtime.Worker, control: dict) -> None:
    job_id = uuid.UUID(control["job_id"])
    if control["type"] == "job_progress":
        live_progress[job_id] = float(control.get("progress") or 0.0)
        publish(job_id, {"state": "running", "progress": control.get("progress")})
        return
    entry = inflight.pop(job_id, None)
    live_progress.pop(job_id, None)
    worker.job_busy = False
    if entry is None:
        return  # stale report from a previous incarnation
    if db.session_factory is None:
        return
    if control["type"] == "job_done":
        async with db.session_factory() as session:
            job = await locked_job(session, job_id)
            if job is None:
                return
            job.state = "succeeded"
            job.gpu_ms = int(control.get("gpu_ms", 0))
            session.add(Asset(
                user_id=entry.user_id,
                job_id=job_id,
                storage_key=entry.storage_key,
                mime="image/webp",
                width=int(control.get("width", 0)),
                height=int(control.get("height", 0)),
            ))
            await session.commit()
        url = await get_storage().url(entry.storage_key)
        publish(job_id, {"state": "succeeded", "url": url})
        logger.info("job %s succeeded, gpu_ms=%s", job_id, control.get("gpu_ms"))
    else:
        reason = str(control.get("reason", "worker reported failure"))
        await mark_failed(job_id, reason)


async def mark_failed(job_id: uuid.UUID, reason: str) -> None:
    assert db.session_factory is not None
    async with db.session_factory() as session:
        job = await locked_job(session, job_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        job.state = "failed"
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
