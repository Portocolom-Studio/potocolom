"""The worker's side of the fleet connection, per docs/connection-handling.md.

Inference is simulated: a configurable sleep stands in for the diffusion
pipeline, which arrives with issue #15. Everything else (dial out, backoff,
registration, heartbeats, latest input wins) is the real behavior.
"""

import asyncio
import json
import random
import uuid

import websockets

from worker.settings import Settings, get_settings

PROTOCOL_VERSION = 1
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17

SIMULATED_MODELS = ["sd-sim"]

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
    response = json.loads(await ws.recv())
    if response["type"] != "registered":
        raise RuntimeError(f"registration refused: {response}")
    print(f"worker {settings.worker_id}: registered", flush=True)

    runners: dict[uuid.UUID, SessionRunner] = {}

    async def heartbeats() -> None:
        while True:
            await asyncio.sleep(settings.heartbeat_seconds)
            await ws.send(json.dumps({"type": "heartbeat", "slots_in_use": len(runners)}))

    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        async for message in ws:
            if isinstance(message, bytes):
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
        except (OSError, websockets.WebSocketException) as error:
            print(f"worker {settings.worker_id}: connection lost ({error}), "
                  f"retrying in {delay:.0f}s", flush=True)
        await asyncio.sleep(delay * (1 + random.random() * BACKOFF_JITTER))
        delay = min(delay * 2, BACKOFF_CAP)
