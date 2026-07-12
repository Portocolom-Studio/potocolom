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

from worker.engine import Engine, SimulatedEngine
from worker.manifests import SIMULATED_MANIFEST, Manifest, load_manifests
from worker.settings import Settings, get_settings

logger = logging.getLogger("potocolom.worker")

# Wire constants; keep in sync with backend/app/realtime.py.
PROTOCOL_VERSION = 1
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17
CLOSE_PROTOCOL_VIOLATION = 4000

UPLOAD_TIMEOUT = 60.0


class RegistrationRejected(Exception):
    """The API refused this worker's protocol version; do not retry."""

BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 30.0
BACKOFF_JITTER = 0.25


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
            await ws.send(bytes([GENERATED_FRAME]) + self.session_id.bytes + generated)

    def close(self) -> None:
        self._task.cancel()


async def run_job(ws, engine: Engine, manifest: Manifest, control: dict) -> None:
    """One queued job: generate, upload to the given target, report the result.
    Failures are reported, never raised: the connection outlives the job."""
    job_id = control["job_id"]

    def progress(fraction: float) -> None:
        asyncio.ensure_future(send_progress(fraction))

    async def send_progress(fraction: float) -> None:
        with suppress(websockets.WebSocketException):
            await ws.send(json.dumps({"type": "job_progress", "job_id": job_id,
                                      "progress": round(fraction, 4)}))

    try:
        params = manifest.with_defaults(control.get("params") or {})
        result = await engine.generate(manifest, params, progress)
        upload = control["upload"]
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            response = await client.put(upload["url"], content=result.data,
                                        headers=upload.get("headers") or {})
            response.raise_for_status()
        await ws.send(json.dumps({"type": "job_done", "job_id": job_id,
                                  "gpu_ms": result.gpu_ms,
                                  "width": result.width, "height": result.height}))
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
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
                # docs/connection-handling.md: protocol violations close with
                # 4000 from either side; run() then reconnects with backoff.
                logger.warning("protocol violation from the API (%s), closing", error)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                return
    finally:
        heartbeat_task.cancel()
        for runner in runners.values():
            runner.close()
        for task in jobs:
            task.cancel()


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
