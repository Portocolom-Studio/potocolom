"""Fleet and realtime WebSocket endpoints, per docs/connection-handling.md.

Single process, in-memory state: this is the self-hosted dispatch path. The
cloud profile replaces the in-process relay with Redis pub/sub behind the same
message flow (docs/blueprint.md); nothing on the wire changes.

Slot accounting has exactly one writer: assign() and release() on this side.
Worker heartbeats refresh liveness only; their self-reported counts are never
written back, so an in-flight heartbeat cannot undo a just-committed slot.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.manifests import Manifest, parse_manifests, validate_params

logger = logging.getLogger("potocolom.realtime")

# Wire constants; keep in sync with worker/worker/client.py.
PROTOCOL_VERSION = 1
MIN_SUPPORTED_VERSION = PROTOCOL_VERSION - 1

CANVAS_FRAME = 0x01
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17  # 1 byte kind + 16 byte session uuid

CLOSE_PROTOCOL_VIOLATION = 4000
CLOSE_UNSUPPORTED_VERSION = 4002
CLOSE_NO_CAPACITY = 4003
CLOSE_UNKNOWN_MODEL = 4004

SESSION_READY_TIMEOUT = 10.0
WORKER_DEAD_SECONDS = 90.0  # 3 missed heartbeats, docs/connection-handling.md

router = APIRouter()


class ProtocolError(Exception):
    """The peer violated docs/connection-handling.md; close with 4000."""


_SEND_FAILURES = (WebSocketDisconnect, RuntimeError, ConnectionError, BrokenPipeError)


async def safe_send(sending: "Coroutine[object, object, None]") -> None:
    """Send to a peer that may have just closed; a dead socket is not an error."""
    try:
        await sending
    except asyncio.CancelledError:
        raise
    except _SEND_FAILURES:
        return


async def refuse(ws: WebSocket, code: int, message: str) -> None:
    """Send a terminal error and close, tolerating a peer that is already gone."""
    try:
        await ws.send_json({"type": "error", "code": code, "message": message})
        await ws.close(code=code)
    except asyncio.CancelledError:
        raise
    except _SEND_FAILURES:
        return


def parse_control(text: str) -> dict:
    try:
        control = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError("malformed JSON") from error
    if not isinstance(control, dict) or "type" not in control:
        raise ProtocolError("control message without a type")
    return control


def frame_session_id(data: bytes) -> uuid.UUID:
    if len(data) < FRAME_HEADER_BYTES:
        raise ProtocolError("binary frame shorter than the header")
    return uuid.UUID(bytes=data[1:FRAME_HEADER_BYTES])


@dataclass
class Worker:
    id: str
    ws: WebSocket
    manifests: list[Manifest]
    realtime_slots: int
    slots_in_use: int = 0
    job_busy: bool = False  # queued jobs fill idle capacity, one at a time
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def models(self) -> list[str]:
        return [m.id for m in self.manifests]

    @property
    def free_slots(self) -> int:
        return self.realtime_slots - self.slots_in_use


@dataclass
class Session:
    id: uuid.UUID
    model_id: str
    browser: WebSocket
    params: dict = field(default_factory=dict)
    worker: Worker | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_live(self) -> bool:
        """False once the browser handler's teardown has removed the session."""
        return self.id in sessions


workers: dict[str, Worker] = {}
sessions: dict[uuid.UUID, Session] = {}
gpu_requests: dict[str, asyncio.Future] = {}


def pick_any_worker() -> Worker | None:
    return next(iter(workers.values()), None)


def pick_worker_for_model(model_id: str) -> Worker | None:
    for worker in workers.values():
        if model_id in worker.models:
            return worker
    return None


def resolve_gpu_request(control: dict) -> None:
    request_id = control.get("request_id")
    if not isinstance(request_id, str):
        return
    future = gpu_requests.pop(request_id, None)
    if future is not None and not future.done():
        future.set_result(control)


async def gpu_command(worker: Worker, command: dict, timeout: float = 120.0) -> dict:
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    gpu_requests[request_id] = future
    try:
        await worker.ws.send_json({**command, "request_id": request_id})
        result = await asyncio.wait_for(future, timeout)
        return result
    except TimeoutError as error:
        raise HTTPException(status_code=504,
                            detail="worker did not respond to gpu command") from error
    finally:
        gpu_requests.pop(request_id, None)


def pick_worker(model_id: str) -> Worker | None:
    candidates = [w for w in workers.values() if model_id in w.models and w.free_slots > 0]
    return max(candidates, key=lambda w: w.free_slots, default=None)


def model_known(model_id: str) -> bool:
    return any(model_id in w.models for w in workers.values())


async def assign(session: Session, worker: Worker) -> bool:
    """Open the session on a worker and wait for its slot. True when ready.

    On any failure the slot increment is compensated here, so no caller can
    leak a slot by abandoning the session mid-assignment.
    """
    session.worker = worker
    session.ready.clear()
    worker.slots_in_use += 1
    try:
        await worker.ws.send_json(
            {"type": "open_session", "session_id": str(session.id),
             "model_id": session.model_id, "params": session.params}
        )
        await asyncio.wait_for(session.ready.wait(), SESSION_READY_TIMEOUT)
    except (TimeoutError, RuntimeError):  # unresponsive worker, or its socket just closed
        worker.slots_in_use -= 1
        session.worker = None
        return False
    return True


async def release(session: Session) -> None:
    if session.worker is None:
        return
    worker, session.worker = session.worker, None
    worker.slots_in_use -= 1
    if workers.get(worker.id) is worker:  # still connected, same incarnation
        await safe_send(
            worker.ws.send_json({"type": "close_session", "session_id": str(session.id)})
        )


async def reassign(session: Session) -> None:
    """The session's worker vanished: interrupted, new worker, resumed."""
    session.worker = None
    if not session.is_live:  # browser already gone
        return
    await safe_send(session.browser.send_json({"type": "interrupted"}))
    replacement = pick_worker(session.model_id)
    if replacement is None or not await assign(session, replacement):
        logger.warning("session %s lost its worker and no replacement was available", session.id)
        await refuse(session.browser, CLOSE_NO_CAPACITY, "no worker capacity")
        return
    if not session.is_live:  # browser disconnected while we assigned
        await release(session)
        return
    logger.info("session %s resumed on worker %s", session.id, replacement.id)
    await safe_send(session.browser.send_json({"type": "resumed"}))


async def reap_once() -> None:
    cutoff = time.monotonic() - WORKER_DEAD_SECONDS
    for worker in [w for w in workers.values() if w.last_seen < cutoff]:
        logger.warning("worker %s silent for %ds, closing", worker.id, int(WORKER_DEAD_SECONDS))
        # Closing server side wakes the fleet handler, whose cleanup
        # removes the worker and reassigns its sessions.
        await safe_send(worker.ws.close())


async def reap_dead_workers() -> None:
    while True:
        await asyncio.sleep(WORKER_DEAD_SECONDS / 3)
        await reap_once()


@router.websocket("/api/v1/fleet")
async def fleet(ws: WebSocket) -> None:
    await ws.accept()
    try:
        hello = parse_control(await ws.receive_text())
        if hello["type"] != "hello":
            raise ProtocolError("first message must be hello")
        version = hello["protocol_version"]
        try:
            worker_manifests = parse_manifests(hello["models"])
        except ValueError as error:
            raise ProtocolError(str(error)) from error
        worker = Worker(id=hello["worker_id"], ws=ws, manifests=worker_manifests,
                        realtime_slots=hello["realtime_slots"])
        if not (isinstance(version, int) and isinstance(worker.id, str)
                and isinstance(worker.realtime_slots, int)):
            raise ProtocolError("hello fields have wrong types")
    except (ProtocolError, KeyError):
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if version < MIN_SUPPORTED_VERSION:
        logger.warning("worker %s rejected: protocol version %s below %s",
                       worker.id, version, MIN_SUPPORTED_VERSION)
        await ws.send_json({"type": "rejected", "reason": "unsupported protocol version",
                            "min_supported_version": MIN_SUPPORTED_VERSION})
        await ws.close(code=CLOSE_UNSUPPORTED_VERSION)
        return
    workers[worker.id] = worker
    logger.info("worker %s registered models=%s slots=%d",
                worker.id, worker.models, worker.realtime_slots)
    await ws.send_json({"type": "registered"})
    from app import gpu_samples, registry  # late import; registry reads this module's state
    await registry.persist_manifests(worker.manifests)
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    session = sessions.get(frame_session_id(data))
                    if session is not None:  # browser may have just closed
                        await safe_send(session.browser.send_bytes(data))
                elif message.get("text") is not None:
                    control = parse_control(message["text"])
                    worker.last_seen = time.monotonic()
                    if control["type"] == "session_ready":
                        session = sessions.get(uuid.UUID(control["session_id"]))
                        if session is not None:
                            session.ready.set()
                    elif control["type"] in ("job_progress", "job_done", "job_failed"):
                        from app import jobs  # late import; jobs reads this module's state
                        await jobs.on_worker_message(worker, control)
                    elif control["type"] in ("gpu_status", "model_loaded",
                                             "model_unloaded", "gpu_error"):
                        resolve_gpu_request(control)
                    elif control["type"] == "heartbeat":
                        gpu_samples.schedule_heartbeat_sample(worker.id, control)
                    # Heartbeats refresh last_seen only; slot accounting has
                    # one writer (assign/release), so self-reported counts
                    # are deliberately not written back.
            except (ProtocolError, KeyError, ValueError):
                logger.warning("worker %s violated the protocol, closing", worker.id)
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        if workers.get(worker.id) is worker:
            del workers[worker.id]
        from app import jobs
        jobs.on_worker_lost(worker)
        orphaned = [s for s in sessions.values() if s.worker is worker]
        if orphaned:
            logger.info("worker %s disconnected with %d sessions to reassign",
                        worker.id, len(orphaned))
        for session in orphaned:
            asyncio.ensure_future(reassign(session))


@router.websocket("/api/v1/realtime")
async def realtime(ws: WebSocket) -> None:
    await ws.accept()
    try:
        opening = parse_control(await ws.receive_text())
        if opening["type"] != "open":
            raise ProtocolError("first message must be open")
        model_id = opening["model_id"]
        params = opening.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError("params must be an object")
    except (ProtocolError, KeyError):
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if not model_known(model_id):
        await refuse(ws, CLOSE_UNKNOWN_MODEL, "unknown model")
        return
    from app import registry

    manifest = registry.available().get(model_id)
    if manifest is not None:
        if validate_params(manifest, params) is not None:
            await refuse(ws, CLOSE_PROTOCOL_VIOLATION, "invalid params")
            return
    worker = pick_worker(model_id)
    if worker is None:
        await refuse(ws, CLOSE_NO_CAPACITY, "no worker capacity")
        return
    session = Session(id=uuid.uuid4(), model_id=model_id, browser=ws, params=params)
    sessions[session.id] = session
    try:
        if not await assign(session, worker):
            await refuse(ws, CLOSE_NO_CAPACITY, "worker did not become ready")
            return
        await ws.send_json({"type": "ready", "session_id": str(session.id)})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    # The browser is untrusted: frames must be canvas frames
                    # for this connection's own session, nothing else.
                    if frame_session_id(data) != session.id or data[0] != CANVAS_FRAME:
                        raise ProtocolError("frame does not belong to this session")
                    if session.worker is not None:  # a dead worker means reassign is in flight
                        await safe_send(session.worker.ws.send_bytes(data))
                elif message.get("text") is not None:
                    if parse_control(message["text"])["type"] == "close":
                        break
            except ProtocolError:
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        sessions.pop(session.id, None)
        await release(session)
