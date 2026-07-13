"""The worker's side of the fleet connection, per docs/connection-handling.md.

Inference is simulated: a configurable sleep stands in for the diffusion
pipeline, which arrives with issue #15. Everything else (dial out, backoff,
registration, heartbeats, latest input wins) is the real behavior.
"""

import asyncio
import json
import logging
import random
import uuid

import websockets

from worker.settings import Settings, get_settings

logger = logging.getLogger("potocolom.worker")

# Wire constants; keep in sync with backend/app/realtime.py.
PROTOCOL_VERSION = 1
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17
CLOSE_PROTOCOL_VIOLATION = 4000

SIMULATED_MODELS = ["sd-sim"]


class RegistrationRejected(Exception):
    """The API refused this worker's protocol version; do not retry."""

BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 30.0
BACKOFF_JITTER = 0.25


class SessionRunner:
    """Holds at most one pending canvas frame; newer input overwrites older."""

    def __init__(self, session_id: uuid.UUID, ws, inference_seconds: float):
        self.session_id = session_id
        self.pending: bytes | None = None
        self.arrived = asyncio.Event()
        self.dropped = 0
        self.frames = 0
        self._task = asyncio.create_task(self._run(ws, inference_seconds))

    def submit(self, payload: bytes) -> None:
        if self.pending is not None:
            self.dropped += 1
        self.pending = payload
        self.arrived.set()

    async def _run(self, ws, inference_seconds: float) -> None:
        while True:
            await self.arrived.wait()
            self.arrived.clear()
            payload, self.pending = self.pending, None
            if payload is None:  # unreachable today; narrows the Optional for mypy
                continue
            await asyncio.sleep(inference_seconds)  # simulated GPU time
            self.frames += 1
            await ws.send(bytes([GENERATED_FRAME]) + self.session_id.bytes + payload)

    def close(self) -> None:
        self._task.cancel()


async def serve_connection(ws, settings: Settings) -> None:
    await ws.send(json.dumps({
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "worker_id": settings.worker_id,
        "models": SIMULATED_MODELS,
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

    runners: dict[uuid.UUID, SessionRunner] = {}

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
                        runners[session_id] = SessionRunner(session_id, ws,
                                                            settings.inference_seconds)
                        await ws.send(json.dumps({"type": "session_ready",
                                                  "session_id": control["session_id"]}))
                    elif control["type"] == "close_session":
                        runner = runners.pop(uuid.UUID(control["session_id"]), None)
                        if runner is not None:
                            runner.close()
                            await ws.send(json.dumps({"type": "session_closed",
                                                      "session_id": control["session_id"],
                                                      "frames": runner.frames}))
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


async def run() -> None:
    settings = get_settings()
    delay = BACKOFF_INITIAL
    while True:
        try:
            async with websockets.connect(settings.api_url) as ws:
                delay = BACKOFF_INITIAL
                await serve_connection(ws, settings)
        except RegistrationRejected as error:
            logger.error("registration rejected (%s); update this worker, not retrying", error)
            return
        except (OSError, websockets.WebSocketException) as error:
            logger.warning("connection lost (%s), retrying in %.0fs", error, delay)
        await asyncio.sleep(delay * (1 + random.random() * BACKOFF_JITTER))
        delay = min(delay * 2, BACKOFF_CAP)
