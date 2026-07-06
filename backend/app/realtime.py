"""Fleet and realtime WebSocket endpoints, per docs/connection-handling.md.

Single process, in-memory state: this is the self-hosted dispatch path. The
cloud profile replaces the in-process relay with Redis pub/sub behind the same
message flow (docs/blueprint.md); nothing on the wire changes.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket

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

router = APIRouter()


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
    """Open the session on a worker and wait for its slot. True when ready."""
    session.worker = worker
    session.ready.clear()
    worker.slots_in_use += 1
    await worker.ws.send_json(
        {"type": "open_session", "session_id": str(session.id), "model_id": session.model_id}
    )
    try:
        await asyncio.wait_for(session.ready.wait(), SESSION_READY_TIMEOUT)
    except TimeoutError:
        return False
    return True


async def release(session: Session) -> None:
    if session.worker is None:
        return
    worker, session.worker = session.worker, None
    worker.slots_in_use -= 1
    if worker.id in workers:  # still connected
        await worker.ws.send_json({"type": "close_session", "session_id": str(session.id)})


async def reassign(session: Session) -> None:
    """The session's worker vanished: interrupted, new worker, resumed."""
    session.worker = None
    await session.browser.send_json({"type": "interrupted"})
    replacement = pick_worker(session.model_id)
    if replacement is None or not await assign(session, replacement):
        await session.browser.send_json({"type": "error", "code": CLOSE_NO_CAPACITY,
                                         "message": "no worker capacity"})
        await session.browser.close(code=CLOSE_NO_CAPACITY)
        return
    await session.browser.send_json({"type": "resumed"})


@router.websocket("/api/v1/fleet")
async def fleet(ws: WebSocket) -> None:
    await ws.accept()
    hello = json.loads(await ws.receive_text())
    if hello.get("type") != "hello":
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    if hello["protocol_version"] < MIN_SUPPORTED_VERSION:
        await ws.send_json({"type": "rejected", "reason": "unsupported protocol version",
                            "min_supported_version": MIN_SUPPORTED_VERSION})
        await ws.close(code=CLOSE_UNSUPPORTED_VERSION)
        return
    worker = Worker(id=hello["worker_id"], ws=ws, models=hello["models"],
                    realtime_slots=hello["realtime_slots"])
    workers[worker.id] = worker
    await ws.send_json({"type": "registered"})
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                data = message["bytes"]
                session = sessions.get(uuid.UUID(bytes=data[1:FRAME_HEADER_BYTES]))
                if session is not None:
                    await session.browser.send_bytes(data)
            elif message.get("text") is not None:
                control = json.loads(message["text"])
                worker.last_seen = time.monotonic()
                if control["type"] == "heartbeat":
                    worker.slots_in_use = control["slots_in_use"]
                elif control["type"] == "session_ready":
                    session = sessions.get(uuid.UUID(control["session_id"]))
                    if session is not None:
                        session.ready.set()
    finally:
        workers.pop(worker.id, None)
        orphaned = [s for s in sessions.values() if s.worker is worker]
        for session in orphaned:
            asyncio.ensure_future(reassign(session))


@router.websocket("/api/v1/realtime")
async def realtime(ws: WebSocket) -> None:
    await ws.accept()
    opening = json.loads(await ws.receive_text())
    if opening.get("type") != "open":
        await ws.close(code=CLOSE_PROTOCOL_VIOLATION)
        return
    model_id = opening["model_id"]
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
            if message.get("bytes") is not None and session.worker is not None:
                await session.worker.ws.send_bytes(message["bytes"])
            elif message.get("text") is not None:
                if json.loads(message["text"])["type"] == "close":
                    break
    finally:
        sessions.pop(session.id, None)
        await release(session)
