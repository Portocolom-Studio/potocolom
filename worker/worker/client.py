"""The worker's side of the fleet connection, per docs/connection-handling.md.

Real inference lives behind the Engine seam (worker/engine.py): diffusers
when a models directory is configured, a simulated engine otherwise.
Everything else (dial out, backoff, registration, heartbeats, latest input
wins, job execution) is identical in both cases.
"""

import asyncio
import json
import logging
import random
import uuid
from contextlib import suppress

import httpx
import websockets

from worker.engine import Engine, SimulatedEngine, make_thumbnail_webp
from worker.manifests import SIMULATED_MANIFEST, Manifest, load_manifests
from worker.settings import Settings, get_settings

logger = logging.getLogger("potocolom.worker")

# Wire constants; keep in sync with backend/app/realtime.py.
PROTOCOL_VERSION = 1
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17
CLOSE_PROTOCOL_VIOLATION = 4000

UPLOAD_TIMEOUT = 60.0
# Heartbeat interval while a job runs without denoising progress (model load).
PROGRESS_KEEPALIVE_SECONDS = 60.0


class RegistrationRejected(Exception):
    """The API refused this worker's protocol version; do not retry."""

BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 30.0
BACKOFF_JITTER = 0.25


class LockedWebSocket:
    """Serialize ws.send calls; cheap insurance against concurrent writers."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send(self, data) -> None:
        async with self._lock:
            await self._ws.send(data)

    def __aiter__(self):
        return self._ws.__aiter__()

    def __getattr__(self, name):
        return getattr(self._ws, name)


def build_runtime(settings: Settings) -> tuple[list[Manifest], Engine]:
    """Built once per process: reconnects keep loaded pipelines warm."""
    if settings.models_dir:
        from worker.engine import DiffusersEngine

        return load_manifests(settings.models_dir), DiffusersEngine(settings.device)
    return [SIMULATED_MANIFEST], SimulatedEngine(settings.inference_seconds)


class SessionRunner:
    """Holds at most one pending canvas frame; newer input overwrites older."""

    def __init__(self, session_id: uuid.UUID, ws, engine: Engine, manifest: Manifest,
                 params: dict):
        self.session_id = session_id
        self.pending: bytes | None = None
        self.arrived = asyncio.Event()
        self.dropped = 0
        self.frames = 0
        self._task = asyncio.create_task(self._run(ws, engine, manifest, params))

    def submit(self, payload: bytes) -> None:
        if self.pending is not None:
            self.dropped += 1
        self.pending = payload
        self.arrived.set()

    async def _run(self, ws, engine: Engine, manifest: Manifest, params: dict) -> None:
        while True:
            await self.arrived.wait()
            self.arrived.clear()
            payload, self.pending = self.pending, None
            if payload is None:  # unreachable today; narrows the Optional for mypy
                continue
            try:
                generated = await engine.frame(manifest, params, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session %s dropped a frame on an inference error",
                                 self.session_id)
                continue
            self.frames += 1
            try:
                await ws.send(bytes([GENERATED_FRAME]) + self.session_id.bytes + generated)
            except websockets.WebSocketException:
                logger.warning("session %s lost the connection while sending a frame",
                               self.session_id)
                return

    def close(self) -> None:
        self._task.cancel()


async def run_job(ws, engine: Engine, manifest: Manifest, control: dict) -> None:
    """One queued job: generate, upload to the given target, report the result.
    Failures are reported, never raised: the connection outlives the job."""
    job_id = control["job_id"]
    progress_tasks: list[asyncio.Task[None]] = []
    last_fraction = 0.0

    def progress(fraction: float) -> None:
        nonlocal last_fraction
        last_fraction = fraction
        progress_tasks.append(asyncio.create_task(send_progress(fraction)))

    async def send_progress(fraction: float) -> None:
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_progress", "job_id": job_id,
                                      "progress": round(fraction, 4)}))

    async def progress_keepalive() -> None:
        while True:
            await asyncio.sleep(PROGRESS_KEEPALIVE_SECONDS)
            await send_progress(last_fraction)

    keepalive_task = asyncio.create_task(progress_keepalive())
    try:
        params = manifest.with_defaults(control.get("params") or {})
        result = await engine.generate(manifest, params, progress)
        upload = control["upload"]
        thumb_upload = control.get("thumb_upload")
        has_thumbnail = False
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            response = await client.put(upload["url"], content=result.data,
                                        headers=upload.get("headers") or {})
            response.raise_for_status()
            if thumb_upload:
                # Best effort: the full result is already stored, and the API
                # only records a thumbnail when job_done reports one.
                try:
                    thumb_data = make_thumbnail_webp(result.data)
                    response = await client.put(thumb_upload["url"], content=thumb_data,
                                                headers=thumb_upload.get("headers") or {})
                    response.raise_for_status()
                    has_thumbnail = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("job %s thumbnail failed; delivering without one",
                                     job_id)
        done_msg: dict = {"type": "job_done", "job_id": job_id,
                          "gpu_ms": result.gpu_ms,
                          "width": result.width, "height": result.height}
        if has_thumbnail:
            done_msg["has_thumbnail"] = True
        await ws.send(json.dumps(done_msg))
        logger.info("job %s done in %d gpu_ms", job_id, result.gpu_ms)
    except asyncio.CancelledError:
        raise
    except websockets.WebSocketException:
        logger.warning("job %s finished but the connection is gone; the API requeues it", job_id)
    except Exception as error:
        logger.exception("job %s failed", job_id)
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_failed", "job_id": job_id,
                                      "reason": str(error)}))
    finally:
        keepalive_task.cancel()
        with suppress(asyncio.CancelledError):
            await keepalive_task
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)


async def _gpu_load(ws, engine: Engine, by_id: dict[str, Manifest], control: dict) -> None:
    request_id = control["request_id"]
    try:
        manifest = by_id[control["model_id"]]
        load_ms = await engine.load_model(manifest)
        await ws.send(json.dumps({
            "type": "model_loaded",
            "request_id": request_id,
            "model_id": manifest.id,
            "load_ms": load_ms,
            "loaded_models": engine.loaded_models(),
        }))
    except Exception as error:
        logger.exception("load_model %s failed", control.get("model_id"))
        await ws.send(json.dumps({
            "type": "gpu_error",
            "request_id": request_id,
            "reason": str(error),
        }))


async def _gpu_unload(ws, engine: Engine, control: dict) -> None:
    request_id = control["request_id"]
    try:
        model_id = control.get("model_id")
        if model_id:
            await engine.unload_model(model_id)
        else:
            await engine.unload_all()
        await ws.send(json.dumps({
            "type": "model_unloaded",
            "request_id": request_id,
            "loaded_models": engine.loaded_models(),
        }))
    except Exception as error:
        logger.exception("unload_model failed")
        await ws.send(json.dumps({
            "type": "gpu_error",
            "request_id": request_id,
            "reason": str(error),
        }))


async def serve_connection(ws, settings: Settings, manifests: list[Manifest],
                           engine: Engine) -> None:
    await ws.send(json.dumps({
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": settings.worker_id,
        "models": [manifest.wire() for manifest in manifests],
        "realtime_slots": settings.realtime_slots,
    }))
    try:
        response = json.loads(await ws.recv())
        reply_type = response["type"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning("malformed registration reply (%s), closing to reconnect", error)
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if reply_type == "rejected":
        raise RegistrationRejected(
            f"{response.get('reason', 'rejected')}; "
            f"minimum supported version {response.get('min_supported_version')}"
        )
    if reply_type != "registered":
        raise RegistrationRejected(f"unexpected registration reply: {response}")
    logger.info("registered as %s", settings.worker_id)

    ws = LockedWebSocket(ws)
    by_id = {manifest.id: manifest for manifest in manifests}
    runners: dict[uuid.UUID, SessionRunner] = {}
    jobs: set[asyncio.Task] = set()

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            await ws.send(json.dumps({"type": "heartbeat", "slots_in_use": len(runners)}))

    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        async for message in ws:
            try:
                if isinstance(message, bytes):
                    if len(message) < FRAME_HEADER_BYTES:
                        raise ValueError("binary frame shorter than the header")
                    session_id = uuid.UUID(bytes=message[1:FRAME_HEADER_BYTES])
                    if session_id in runners:
                        runners[session_id].submit(message[FRAME_HEADER_BYTES:])
                else:
                    control = json.loads(message)
                    if control["type"] == "open_session":
                        session_id = uuid.UUID(control["session_id"])
                        manifest = by_id[control["model_id"]]
                        runners[session_id] = SessionRunner(
                            session_id, ws, engine, manifest,
                            manifest.with_defaults(control.get("params") or {}))
                        await ws.send(json.dumps({"type": "session_ready",
                                                  "session_id": control["session_id"]}))
                    elif control["type"] == "close_session":
                        runner = runners.pop(uuid.UUID(control["session_id"]), None)
                        if runner is not None:
                            runner.close()
                            await ws.send(json.dumps({"type": "session_closed",
                                                      "session_id": control["session_id"],
                                                      "frames": runner.frames}))
                    elif control["type"] == "dispatch_job":
                        task = asyncio.create_task(run_job(
                            ws, engine, by_id[control["model_id"]], control))
                        jobs.add(task)
                        task.add_done_callback(jobs.discard)
                    elif control["type"] == "gpu_status":
                        await ws.send(json.dumps({
                            "type": "gpu_status",
                            "request_id": control["request_id"],
                            "loaded_models": engine.loaded_models(),
                        }))
                    elif control["type"] == "load_model":
                        await _gpu_load(ws, engine, by_id, control)
                    elif control["type"] == "unload_model":
                        await _gpu_unload(ws, engine, control)
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
                # docs/connection-handling.md: protocol violations close with
                # 4000 from either side; run() then reconnects with backoff.
                logger.warning("protocol violation from the API (%s), closing", error)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                return
    finally:
        heartbeat_task.cancel()
        runner_tasks: list[asyncio.Task] = []
        for runner in runners.values():
            runner.close()
            runner_tasks.append(runner._task)
        for task in jobs:
            task.cancel()
        await asyncio.gather(heartbeat_task, *jobs, *runner_tasks, return_exceptions=True)


async def run() -> None:
    settings = get_settings()
    manifests, engine = build_runtime(settings)
    delay = BACKOFF_INITIAL
    while True:
        try:
            async with websockets.connect(settings.api_url) as ws:
                delay = BACKOFF_INITIAL
                await serve_connection(ws, settings, manifests, engine)
        except RegistrationRejected as error:
            logger.error("registration rejected (%s); update this worker, not retrying", error)
            return
        except (OSError, websockets.WebSocketException) as error:
            logger.warning("connection lost (%s), retrying in %.0fs", error, delay)
        await asyncio.sleep(delay * (1 + random.random() * BACKOFF_JITTER))
        delay = min(delay * 2, BACKOFF_CAP)
