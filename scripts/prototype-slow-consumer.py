"""Does one slow browser stall the others, once a GPU serves several sessions?

backend/app/realtime.py sends a generated frame with `await
safe_send(session.browser.send_bytes(data))` inside the loop that reads every
session's frames, controls and heartbeats from one worker, and there are no
per-session queues or writer tasks. Reading that, a browser that stops draining
its socket should hold up its neighbours. Measured here, it does not, which is
why this script exists rather than a paragraph asserting either.

    ./backend/.venv/bin/python scripts/prototype-slow-consumer.py

Real API, real worker, real TCP, the stub engine at 0.15 s a frame, which is
what the tiny decoder measures at. Three sessions on one worker. The victim
gets a 32 KB receive buffer set before connect and its reader is cancelled
outright, so its window really closes; an earlier version let the client
library keep draining and proved nothing at all.

Phase A: all three draw and read. Phase B: one stops reading, for 25 s.

What it found, over five runs: the healthy two are untouched, 200 of 200 frames
each with p50 within 0.1 ms of phase A. But nothing drops the stalled session's
frames either. On resume it receives its entire backlog in order, the oldest
25 s old, so a returning browser renders a quarter minute of stale canvas
before catching up. That is the case for the bounded latest-value mailboxes in
docs/connection-handling.md: not to stop one browser hurting another, which
does not happen, but because a slow browser should be shown the newest frame
and not every frame it missed.

Where the retained bytes live is not established. Kernel queues hold about
30 KB of the 24 MB, and resident-memory deltas cannot locate the rest because
both processes reuse freed heap, so this script makes no memory claim.
"""

import asyncio
import json
import os
import statistics
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import socket

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.realtime import CANVAS_FRAME, FRAME_HEADER_BYTES  # noqa: E402

def free_port() -> int:
    """A port this run owns. On a fixed port another service can answer the
    health and models polls, and then the script measures that instead."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = free_port()
FRAME_BYTES = 250_000  # deliberately past a WebP preview, to exceed socket buffers
FPS = 4.0
PHASE_SECONDS = 25.0
WARMUP_SECONDS = 2.0
PAD = b"\x00" * (FRAME_BYTES - 8)


def interpreter(component: str) -> str:
    venv = ROOT / component / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


class Browser:
    def __init__(self, name: str, ws):
        self.name, self.ws = name, ws
        self.session: uuid.UUID | None = None
        self.reading = True
        self.sent = self.rendered = self.drained = 0
        self.controls: list[str] = []
        self.latencies: list[tuple[float, float]] = []  # (wall clock, ms)

    async def receiver(self) -> None:
        while True:
            try:
                message = await self.ws.recv()
            except Exception:
                return
            if not self.reading:
                # A browser that stopped draining: park here holding the
                # message, so the library queue fills and back-pressure
                # reaches the API exactly as a stalled tab would.
                while not self.reading:
                    await asyncio.sleep(0.2)
                self.drained += 1
            if not isinstance(message, bytes):
                self.controls.append(json.loads(message).get("type", "?"))
                continue
            if isinstance(message, bytes):
                self.rendered += 1
                stamp = struct.unpack("d", message[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + 8])[0]
                self.latencies.append((time.monotonic(), (time.monotonic() - stamp) * 1000))

    async def open(self, model_id: str) -> None:
        await self.ws.send(json.dumps({
            "type": "open", "model_id": model_id,
            "params": {"prompt": "a red house on a hill"},
        }))
        reply = json.loads(await self.ws.recv())
        assert reply["type"] == "ready", f"{self.name}: {reply}"
        self.session = uuid.UUID(reply["session_id"])

    async def draw(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            payload = struct.pack("d", time.monotonic()) + PAD
            try:
                await asyncio.wait_for(
                    self.ws.send(bytes([CANVAS_FRAME]) + self.session.bytes + payload),
                    timeout=5.0)
            except Exception:
                return  # a send that cannot complete is itself the finding
            self.sent += 1
            await asyncio.sleep(1 / FPS)


def rss_mb(pid: int) -> float:
    """Where undelivered frames pile up, if they pile up in the API rather than
    in a socket buffer: a stalled browser is then a memory cost, not a stall.
    Counts the whole process tree, because uvicorn may not serve from the pid
    it was started as, and watching the wrong one reads as no growth at all."""
    total = 0.0
    pids = [pid]
    while pids:
        current = pids.pop()
        try:
            with open(f"/proc/{current}/statm") as handle:
                total += int(handle.read().split()[1]) * 4096 / 1024**2
            with open(f"/proc/{current}/task/{current}/children") as handle:
                pids.extend(int(child) for child in handle.read().split())
        except OSError:
            continue
    return total


def window(browser: Browser, start: float, end: float) -> list[float]:
    return [ms for at, ms in browser.latencies if start <= at <= end]


def summarise(label: str, values: list[float], frames: int) -> None:
    if not values:
        print(f"  {label:22s} no frames rendered ({frames} total)")
        return
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(0.95 * len(values)))]
    print(f"  {label:22s} n={len(values):3d}  p50 {statistics.median(values):7.1f} ms  "
          f"p95 {p95:7.1f} ms  max {values[-1]:7.1f} ms")


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def main() -> None:
    api = worker = None
    api = subprocess.Popen(
        [interpreter("backend"), "-m", "uvicorn", "app.main:app",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT / "backend")
    worker = subprocess.Popen(
        [interpreter("worker"), "-m", "worker"],
        env=os.environ | {
            "API_URL": f"ws://127.0.0.1:{PORT}/api/v1/fleet",
            "WORKER_ID": "worker-slow-test",
            "INFERENCE_SECONDS": "0.15",   # what the tiny decoder measures at
            "REALTIME_SLOTS": "3",
            "HEARTBEAT_SECONDS": "5",
        }, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    browsers: list[Browser] = []
    try:
        for _ in range(150):
            if api.poll() is not None:
                raise RuntimeError(f"the API exited during startup ({api.returncode})")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/health", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("the API never answered its health check")

        # The worker registers a moment after the API listens, and /models
        # only advertises what a connected worker can serve.
        realtime: list = []
        for _ in range(120):
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/models", timeout=5) as r:
                models = json.load(r)
            entries = models["models"] if isinstance(models, dict) else models
            realtime = [m for m in entries if "realtime" in m["capabilities"]]
            if realtime:
                break
            await asyncio.sleep(0.25)
        assert realtime, f"no realtime model advertised; saw {[m['id'] for m in entries]}"
        model_id = realtime[0]["id"]
        print(f"model {model_id}, frame {FRAME_BYTES / 1000:.0f} KB, {FPS:.0f} fps per browser")

        tasks = []
        for index in range(3):
            for attempt in range(40):
                try:
                    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    # Before connect, so autotuning cannot undo it: a small
                    # receive buffer means the window closes almost at once
                    # when the client stops reading, which is what puts real
                    # back-pressure on the API's inline send.
                    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
                    raw.setblocking(False)
                    await asyncio.get_running_loop().sock_connect(raw, ("127.0.0.1", PORT))
                    ws = await websockets.connect(
                        f"ws://127.0.0.1:{PORT}/api/v1/realtime",
                        sock=raw, max_size=None, max_queue=1)
                    if index == 2:
                        print("victim SO_RCVBUF =",
                              raw.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                              "bytes; socket in use:", ws.transport.get_extra_info("socket") is not None)
                    browser = Browser(f"browser-{index + 1}", ws)
                    await browser.open(model_id)
                    break
                except Exception as error:
                    last = error
                    await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"browser-{index + 1} could not open: {last}")
            browsers.append(browser)
            tasks.append(asyncio.create_task(browser.receiver()))
        print(f"{len(browsers)} sessions live on one worker\n")

        rss_start = rss_mb(api.pid)
        mine_start = rss_mb(os.getpid())
        phase_a = time.monotonic()
        await asyncio.gather(*(b.draw(WARMUP_SECONDS) for b in browsers))
        phase_a_end = time.monotonic()

        rss_after_a = rss_mb(api.pid)
        victim = browsers[-1]
        victim.reading = False
        tasks[-1].cancel()  # nothing drains the victim's socket at all now
        print(f"phase B: {victim.name} stops reading its socket")
        phase_b = time.monotonic()
        await asyncio.gather(*(b.draw(PHASE_SECONDS) for b in browsers))
        phase_b_end = time.monotonic()

        queues = subprocess.run(
            ["ss", "-tn", "sport", f"= :{PORT}", "or", "dport", f"= :{PORT}"],
            capture_output=True, text=True).stdout
        print("\nsocket queues during the stall (Recv-Q Send-Q):")
        for line in queues.splitlines()[:8]:
            print("   ", line.strip()[:96])
        rss_after_b = rss_mb(api.pid)
        print(f"test client resident memory {mine_start:.0f} -> {rss_mb(os.getpid()):.0f} MB")
        rss_after_b_at = time.monotonic()
        victim.reading = True
        tasks[-1] = asyncio.create_task(victim.receiver())
        await asyncio.sleep(6.0)
        drained = [ms for at, ms in victim.latencies if at >= rss_after_b_at]
        if drained:
            print(f"\ndrain: {victim.name} read again and {len(drained)} frames "
                  f"arrived; their age when they landed, measured from the input that asked "
                  f"for them rather than from when they were generated, ranges "
                  f"{min(drained) / 1000:.1f} s to {max(drained) / 1000:.1f} s. "
                  f"Stale means they were buffered during the stall; fresh means they "
                  f"were generated after it. Controls seen: {victim.controls[-4:]}")
        else:
            # Inconclusive on purpose. No binary frame in the drain window says
            # only that this client received none: dropped, still queued,
            # generated late, or a socket that died all look the same from here,
            # and separating them needs connection health and server-side queue
            # depth that this harness does not collect. Reporting it as loss
            # would be the same overclaim the buffered branch was written to
            # avoid. The state is reported rather than a liveness boolean:
            # close_code is None while a connection is open and also while it is
            # closing, so its absence proves nothing on its own.
            print(f"\ndrain: {victim.name} read again and no frames arrived within "
                  f"the drain window, which does not say why: dropped, still queued, "
                  f"generated late and a dead socket are indistinguishable here. "
                  f"WebSocket state: {victim.ws.state.name}, close code "
                  f"{victim.ws.close_code}. "
                  f"Controls seen: {victim.controls[-4:]}")
        print(f"\nAPI resident memory {rss_start:.0f} -> {rss_after_a:.0f} -> {rss_after_b:.0f} MB "
              f"across the three points, against "
              f"{FRAME_BYTES * int(PHASE_SECONDS * FPS) / 1024**2:.0f} MB undelivered. Freed heap is "
              f"reused, so this does not locate the retained frames either way.")
        print("\nphase A, all three reading")
        for b in browsers:
            summarise(b.name, window(b, phase_a, phase_a_end), b.rendered)
        print(f"\nphase B, {victim.name} not reading")
        for b in browsers:
            label = b.name + (" (stalled)" if b is victim else "")
            summarise(label, window(b, phase_b, phase_b_end), b.rendered)

        healthy = browsers[:-1]
        a = [ms for b in healthy for ms in window(b, phase_a, phase_a_end)]
        c = [ms for b in healthy for ms in window(b, phase_b, phase_b_end)]
        print("\nthe healthy two, before and during")
        summarise("phase A", a, sum(b.rendered for b in healthy))
        summarise("phase B", c, sum(b.rendered for b in healthy))
        if a and c:
            print(f"\n  p50 moved {statistics.median(c) - statistics.median(a):+.1f} ms while a "
                  f"neighbour was stalled, and the healthy two rendered {len(c)} frames of "
                  f"{int(PHASE_SECONDS * FPS) * len(healthy)} sent")
        for b in browsers:
            print(f"  {b.name}: sent {b.sent}, rendered {b.rendered}")
    finally:
        for b in browsers:
            b.reading = True
            try:
                await b.ws.close()
            except Exception:
                pass
        stop(worker)
        stop(api)


asyncio.run(main())
