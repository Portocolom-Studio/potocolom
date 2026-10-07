"""Live simulation of connection handling, per docs/connection-handling.md.

Starts the real API server and real workers as subprocesses, drives a simulated
browser over real TCP WebSockets, and demonstrates registration, frame relay,
latest input wins, and session recovery when a worker dies mid session.

Run from the repository root, after the setup in docs/local-development.md:

    backend/.venv/bin/python scripts/simulate.py
"""

import asyncio
import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import websockets

# Importable because this script runs in an environment where the app package
# is installed (backend/.venv locally, one shared env in CI); the wire
# constants stay single-sourced. `ci_database` sits beside this file; Python
# puts the script directory on sys.path, so the import resolves when the
# Makefile or the workflow runs `python scripts/simulate.py`.
from app.realtime import CANVAS_FRAME, FRAME_HEADER_BYTES
from ci_database import (
    DEFAULT_URL,
    ci_from_environ,
    prepare_ci_database,
    refuse_developer_database,
)

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    """Several self-hosted runners share one machine, so a fixed port meant
    two simulations at once fought over it and the loser bound nothing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


PORT = int(os.environ.get("SIMULATE_PORT") or _free_port())
START = time.monotonic()
# Pytest uses the same default. This script is not production; an unset key
# now refuses to import the API (issues #245 and #260).
_FLEET_TOKEN = (
    os.environ.get("FLEET_TOKEN_KEY") or os.environ.get("FLEET_TOKEN") or "test-fleet-token"
)


def interpreter(component: str) -> str:
    """Component venv locally; the current interpreter in CI, where both
    packages are installed into one environment."""
    venv_python = ROOT / component / ".venv/bin/python"
    return str(venv_python) if venv_python.exists() else sys.executable


def log(who: str, text: str) -> None:
    print(f"[{time.monotonic() - START:6.2f}] {who:<9} {text}", flush=True)


_SIMULATE_STORAGE = tempfile.mkdtemp(prefix="potocolom-simulate-")


def spawn_api() -> subprocess.Popen:
    root_key = base64.b64encode(secrets.token_bytes(32)).decode()
    database_url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    env = os.environ | {
        "FLEET_TOKEN_KEY": _FLEET_TOKEN,
        "ROOT_KEYS": f"1:{root_key}",
        "DATABASE_URL": database_url,
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_PATH": _SIMULATE_STORAGE,
        "TELEMETRY": "false",
        "EMAIL_BACKEND": "none",
        "BILLING_ENABLED": "false",
    }
    return subprocess.Popen(
        [interpreter("backend"), "-m", "uvicorn", "app.main:app",
         "--port", str(PORT), "--log-level", "warning",
         "--ws-max-size", "2097152"],
        cwd=ROOT / "backend",
        env=env,
    )


def spawn_worker(number: int) -> subprocess.Popen:
    env = os.environ | {
        "API_URL": f"ws://127.0.0.1:{PORT}/api/v1/fleet",
        "WORKER_ID": f"worker-{number}",
        "INFERENCE_SECONDS": "0.12",
        "HEARTBEAT_SECONDS": "5",
        "FLEET_TOKEN": _FLEET_TOKEN,
    }
    process = subprocess.Popen(
        [interpreter("worker"), "-m", "worker"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def customer_rest_calls() -> None:
    """The REST calls a real SPA makes before any WebSocket work,
    per docs/api.md: liveness, then runtime configuration."""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/health", timeout=2) as response:
        log("customer", f"GET /api/v1/health -> {json.load(response)}")
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/config", timeout=2) as response:
        config = json.load(response)
    log("customer", f"GET /api/v1/config -> auth_methods={config['auth_methods']} "
                    f"billing_enabled={config['billing_enabled']} "
                    f"languages={config['languages']}")


class BrowserSim:
    def __init__(self, ws):
        self.ws = ws
        self.session: uuid.UUID | None = None
        # Input revisions: 1 for the first canvas frame, strictly increasing
        # for the life of the session (they never restart after a resume).
        self.revision = 0
        self.sent = 0
        self.rendered = 0
        self.latencies: list[float] = []
        self.controls: list[str] = []
        self.resumed = asyncio.Event()

    async def receiver(self) -> None:
        async for message in self.ws:
            if isinstance(message, bytes):
                self.rendered += 1
                stamp = message[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + 8]
                latency = time.monotonic() - struct.unpack("d", stamp)[0]
                self.latencies.append(latency)
                if self.rendered == 1 or self.rendered % 10 == 0:
                    log("browser", f"frame {self.rendered} rendered, {latency * 1000:.0f} ms")
            else:
                control = json.loads(message)
                self.controls.append(control["type"])
                log("browser", f"control: {control['type']}")
                if control["type"] == "resumed":
                    self.resumed.set()

    async def open(self, model_id: str) -> None:
        # params must satisfy the model's schema or the API refuses the session
        # with 4000 before assigning a worker. A real drawing client has the
        # same obligation, so this sends what one would send.
        await self.ws.send(json.dumps({
            "type": "open",
            "model_id": model_id,
            "params": {"prompt": "a red house on a hill"},
            "frame_header": 2,
        }))
        ready = json.loads(await self.ws.recv())
        assert ready["type"] == "ready", ready
        self.session = uuid.UUID(ready["session_id"])
        log("browser", f"session ready ({ready['session_id'][:8]})")

    async def stream(self, frames: int, fps: float) -> None:
        for _ in range(frames):
            payload = struct.pack("d", time.monotonic())
            self.revision += 1
            frame = (bytes([CANVAS_FRAME]) + self.session.bytes
                     + self.revision.to_bytes(4, "big") + payload)
            await self.ws.send(frame)
            self.sent += 1
            await asyncio.sleep(1 / fps)


async def open_browser() -> tuple:
    """Connect and open a session, retrying while the first worker registers.
    Fixed sleeps are not reliable on slow CI runners."""
    for _ in range(30):
        ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/api/v1/realtime")
        browser = BrowserSim(ws)
        try:
            await browser.open("sd-sim")
            return ws, browser
        except AssertionError:  # no registered worker serves the model yet
            await ws.close()
            await asyncio.sleep(0.5)
    raise RuntimeError("no worker accepted the session in time")


async def main() -> None:
    api = spawn_api()
    worker_1 = worker_2 = None
    ws = None
    try:
        await wait_for_api()
        customer_rest_calls()
        worker_1 = spawn_worker(1)
        ws, browser = await open_browser()
        # single reader from here on: the receiver owns recv()
        receiver = asyncio.create_task(browser.receiver())
        log("browser", "drawing at 10 fps against 0.12 s inference "
                       "(latest input wins should drop some)")
        await browser.stream(frames=30, fps=10)

        worker_2 = spawn_worker(2)
        await asyncio.sleep(2.0)  # registration; nothing to retry against here
        log("chaos", "killing worker-1 mid session")
        worker_1.kill()

        await asyncio.wait_for(browser.resumed.wait(), timeout=10)
        # The whole reassignment story, not only its last step: the browser
        # was told the session was interrupted, then that it resumed.
        assert "interrupted" in browser.controls, browser.controls
        assert browser.controls.index("interrupted") < browser.controls.index("resumed"), (
            browser.controls)
        rendered_before_resume = browser.rendered
        log("browser", "re-sending current canvas, drawing continues")
        await browser.stream(frames=20, fps=10)

        await asyncio.sleep(0.5)  # let the last frames render
        # A resumed session that renders nothing would pass every check above.
        assert browser.rendered > rendered_before_resume, (
            f"no frames rendered after the resume ({browser.rendered})")
        await ws.send(json.dumps({"type": "close"}))
        receiver.cancel()

        average = sum(browser.latencies) / len(browser.latencies)
        log("summary", f"sent={browser.sent} rendered={browser.rendered} "
                       f"dropped_by_latest_input_wins={browser.sent - browser.rendered} "
                       f"avg_latency={average * 1000:.0f} ms")
    finally:
        if ws is not None:
            await ws.close()
        for process in (worker_1, worker_2, api):
            if process is not None:
                process.kill()


if __name__ == "__main__":
    # CI is set by Actions. A missing DATABASE_URL would otherwise fall through
    # to the developer database and the next local auth-enable would redden main.
    database_url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    running_in_ci = ci_from_environ()
    refuse_developer_database(url=database_url, ci=running_in_ci)
    if running_in_ci:
        prepare_ci_database(database_url)
    asyncio.run(main())
