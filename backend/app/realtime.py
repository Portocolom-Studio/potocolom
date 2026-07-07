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
from contextlib import suppress
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket

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
    models: list[str]
    realtime_slots: int
    slots_in_use: int = 0
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def free_slots(self) -> int:
        return self.realtime_slots - self.slots_in_use


@dataclass
class Session:
    id: uuid.UUID
    model_id: str
    browser: WebSocket
    worker: Worker | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


workers: dict[str, Worker] = {}
sessions: dict[uuid.UUID, Session] = {}


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
            {"type": "open_session", "session_id": str(session.id), "model_id": session.model_id}
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
        with suppress(RuntimeError):
            await worker.ws.send_json({"type": "close_session", "session_id": str(session.id)})


async def reassign(session: Session) -> None:
    """The session's worker vanished: interrupted, new worker, resumed."""
    session.worker = None
    if session.id not in sessions:  # browser already gone
        return
    with suppress(RuntimeError):
        await session.browser.send_json({"type": "interrupted"})
    replacement = pick_worker(session.model_id)
    if replacement is None or not await assign(session, replacement):
        logger.warning("session %s lost its worker and no replacement was available", session.id)
        with suppress(RuntimeError):
            await session.browser.send_json({"type": "error", "code": CLOSE_NO_CAPACITY,
                                             "message": "no worker capacity"})
            await session.browser.close(code=CLOSE_NO_CAPACITY)
        return
    if session.id not in sessions:  # browser disconnected while we assigned
        await release(session)
        return
    logger.info("session %s resumed on worker %s", session.id, replacement.id)
    with suppress(RuntimeError):
        await session.browser.send_json({"type": "resumed"})


async def reap_once() -> None:
    cutoff = time.monotonic() - WORKER_DEAD_SECONDS
    for worker in [w for w in workers.values() if w.last_seen < cutoff]:
        logger.warning("worker %s silent for %ds, closing", worker.id, int(WORKER_DEAD_SECONDS))
        with suppress(RuntimeError):
            # Closing server side wakes the fleet handler, whose cleanup
            # removes the worker and reassigns its sessions.
            await worker.ws.close()


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
        worker = Worker(id=hello["worker_id"], ws=ws, models=hello["models"],
                        realtime_slots=hello["realtime_slots"])
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
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    session = sessions.get(frame_session_id(data))
                    if session is not None:
                        with suppress(RuntimeError):  # browser just closed
                            await session.browser.send_bytes(data)
                elif message.get("text") is not None:
                    control = parse_control(message["text"])
                    worker.last_seen = time.monotonic()
                    if control["type"] == "session_ready":
                        session = sessions.get(uuid.UUID(control["session_id"]))
                        if session is not None:
                            session.ready.set()
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
    except (ProtocolError, KeyError):
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if not model_known(model_id):
        await ws.send_json({"type": "error", "code": CLOSE_UNKNOWN_MODEL,
                            "message": "unknown model"})
        await ws.close(code=CLOSE_UNKNOWN_MODEL)
        return
    worker = pick_worker(model_id)
    if worker is None:
        await ws.send_json({"type": "error", "code": CLOSE_NO_CAPACITY,
                            "message": "no worker capacity"})
        await ws.close(code=CLOSE_NO_CAPACITY)
        return
    session = Session(id=uuid.uuid4(), model_id=model_id, browser=ws)
    sessions[session.id] = session
    try:
        if not await assign(session, worker):
            await ws.send_json({"type": "error", "code": CLOSE_NO_CAPACITY,
                                "message": "worker did not become ready"})
            await ws.close(code=CLOSE_NO_CAPACITY)
            return
        await ws.send_json({"type": "ready", "session_id": str(session.id)})
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    frame_session_id(message["bytes"])  # length gate, worker side trusts it
                    if session.worker is not None:
                        with suppress(RuntimeError):  # worker died, reassign in flight
                            await session.worker.ws.send_bytes(message["bytes"])
                elif message.get("text") is not None:
                    if parse_control(message["text"])["type"] == "close":
                        break
            except ProtocolError:
                await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
                break
    finally:
        sessions.pop(session.id, None)
        await release(session)
