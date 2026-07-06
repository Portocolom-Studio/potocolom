"""Live simulation of connection handling, per docs/connection-handling.md.

Starts the real API server and real workers as subprocesses, drives a simulated
browser over real TCP WebSockets, and demonstrates registration, frame relay,
latest input wins, and session recovery when a worker dies mid session.

Run from the repository root, after the setup in docs/local-development.md:

    backend/.venv/bin/python scripts/simulate.py
"""

import asyncio
import json
import os
import struct
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
PORT = 8901
CANVAS_FRAME = 0x01
START = time.monotonic()


def log(who: str, text: str) -> None:
    print(f"[{time.monotonic() - START:6.2f}] {who:<9} {text}", flush=True)


def spawn_api() -> subprocess.Popen:
    return subprocess.Popen(
        [str(ROOT / "backend/.venv/bin/python"), "-m", "uvicorn", "app.main:app",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT / "backend",
    )


def spawn_worker(number: int) -> subprocess.Popen:
    env = os.environ | {
        "API_URL": f"ws://127.0.0.1:{PORT}/api/v1/fleet",
        "WORKER_ID": f"worker-{number}",
        "INFERENCE_SECONDS": "0.12",
        "HEARTBEAT_SECONDS": "5",
    }
    process = subprocess.Popen(
        [str(ROOT / "worker/.venv/bin/python"), "-m", "worker"],
        env=env, stdout=subprocess.DEVNULL,
    )
    log("chaos", f"started worker-{number} (pid {process.pid})")
    return process


async def wait_for_api() -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/health", timeout=1)
            log("api", f"listening on :{PORT}")
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError("API did not come up")


class BrowserSim:
    def __init__(self, ws):
        self.ws = ws
        self.session: uuid.UUID | None = None
        self.sent = 0
        self.rendered = 0
        self.latencies: list[float] = []
        self.resumed = asyncio.Event()

    async def receiver(self) -> None:
        async for message in self.ws:
            if isinstance(message, bytes):
                self.rendered += 1
                latency = time.monotonic() - struct.unpack("d", message[17:25])[0]
                self.latencies.append(latency)
                if self.rendered == 1 or self.rendered % 10 == 0:
                    log("browser", f"frame {self.rendered} rendered, {latency * 1000:.0f} ms")
            else:
                control = json.loads(message)
                log("browser", f"control: {control['type']}")
                if control["type"] == "resumed":
                    self.resumed.set()

    async def open(self, model_id: str) -> None:
        await self.ws.send(json.dumps({"type": "open", "model_id": model_id}))
        ready = json.loads(await self.ws.recv())
        assert ready["type"] == "ready", ready
        self.session = uuid.UUID(ready["session_id"])
        log("browser", f"session ready ({ready['session_id'][:8]})")

    async def stream(self, frames: int, fps: float) -> None:
        for _ in range(frames):
            payload = struct.pack("d", time.monotonic())
            await self.ws.send(bytes([CANVAS_FRAME]) + self.session.bytes + payload)
            self.sent += 1
            await asyncio.sleep(1 / fps)


async def main() -> None:
    api = spawn_api()
    worker_1 = worker_2 = None
    try:
        await wait_for_api()
        worker_1 = spawn_worker(1)
        await asyncio.sleep(1.0)  # registration

        async with websockets.connect(f"ws://127.0.0.1:{PORT}/api/v1/realtime") as ws:
            browser = BrowserSim(ws)
            await browser.open("sd-sim")
            # single reader from here on: the receiver owns recv()
            receiver = asyncio.create_task(browser.receiver())
            log("browser", "drawing at 10 fps against 0.12 s inference "
                           "(latest input wins should drop some)")
            await browser.stream(frames=30, fps=10)

            worker_2 = spawn_worker(2)
            await asyncio.sleep(1.0)  # registration
            log("chaos", "killing worker-1 mid session")
            worker_1.kill()

            await asyncio.wait_for(browser.resumed.wait(), timeout=10)
            log("browser", "re-sending current canvas, drawing continues")
            await browser.stream(frames=20, fps=10)

            await asyncio.sleep(0.5)  # let the last frames render
            await ws.send(json.dumps({"type": "close"}))
            receiver.cancel()

            average = sum(browser.latencies) / len(browser.latencies)
            log("summary", f"sent={browser.sent} rendered={browser.rendered} "
                           f"dropped_by_latest_input_wins={browser.sent - browser.rendered} "
                           f"avg_latency={average * 1000:.0f} ms")
    finally:
        for process in (worker_1, worker_2, api):
            if process is not None:
                process.kill()


if __name__ == "__main__":
    asyncio.run(main())
