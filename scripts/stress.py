"""Deterministic stress test for the realtime and fleet WebSockets.

Targets an API that is already running (AUTH_MODE=none, no worker needed).
Fake workers speak the fleet protocol inside this process, so no GPU and no
worker process are involved. The schedule is fixed by --seed and the counts:
the same messages go out in the same order, and only timings, send stamps and
server-minted ids vary between runs. Prints one table and exits 1 when
any threshold fails.

    FLEET_TOKEN=... backend/.venv/bin/python scripts/stress.py --api http://localhost:8427
"""

import argparse
import asyncio
import json
import math
import os
import random
import struct
import sys
import time
import uuid
import zlib
from contextlib import asynccontextmanager, suppress

import httpx
import websockets

from app.realtime import (
    CANVAS_FRAME,
    CLOSE_NO_CAPACITY,
    CLOSE_PROTOCOL_VIOLATION,
    FRAME_HEADER_BYTES,
    GENERATED_FRAME,
    MAX_CANVAS_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
)

# Its own id, so a real worker's sd-sim is never mixed into what is measured.
MODEL_ID = "stress-sim"
MANIFEST = {
    "id": MODEL_ID, "name": "Stress", "capabilities": ["text_to_image", "realtime"],
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}},
                   "required": ["prompt"]},
}
LATENCY_P95_MS = 500  # the REALTIME_BAR_MS admission bar in app/realtime.py
STALE_MAX_S = 2.0
RSS_GROWTH_MB = 64
FD_GROWTH = 10
SCENARIOS = ["sessions", "throughput", "worker-churn", "conn-churn", "slow-consumer",
             "rest-burst"]


def tiny_png() -> bytes:
    """8x8 black PNG: job_done is refused unless the upload decodes as PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    rows = b"".join(b"\x00" + bytes(24) for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = tiny_png()


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def jain(values: list[int]) -> float:
    """1.0 when every session got the same share, 1/n when one got all of it."""
    total = sum(values)
    return total * total / (len(values) * sum(v * v for v in values)) if total else 0.0


def process(pid: int | None) -> tuple[float, int]:
    """API resident set in MiB and open descriptors, or (nan, 0) without --api-pid."""
    if pid is None:
        return float("nan"), 0
    with open(f"/proc/{pid}/status") as status:
        rss = next(int(line.split()[1]) for line in status if line.startswith("VmRSS"))
    return rss / 1024, len(os.listdir(f"/proc/{pid}/fd"))


class Runner:
    def __init__(self) -> None:
        self.pending: bytes | None = None
        self.wake = asyncio.Event()
        self.frames = 0
        self.task: asyncio.Task | None = None


class FakeWorker:
    """A protocol 4 worker whose GPU is one lock and a sleep. Latest input wins
    per session, like worker/worker/client.py: one pending frame, overwritten."""

    def __init__(self, args: argparse.Namespace, number: int, slots: int) -> None:
        self.args, self.id, self.slots = args, f"stress-{number}", slots
        self.gpu = asyncio.Lock()
        self.runners: dict[bytes, Runner] = {}
        self.dropped = 0
        self.http = httpx.AsyncClient(timeout=30)

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.args.api_ws + "/api/v1/fleet", max_size=2**21,
            additional_headers={"X-Fleet-Token": self.args.fleet_token})
        await self.ws.send(json.dumps({
            "type": "hello", "protocol_version": PROTOCOL_VERSION, "worker_id": self.id,
            "models": [MANIFEST], "realtime_slots": self.slots,
            "device": "stress", "memory_mode": "stress"}))
        reply = json.loads(await self.ws.recv())
        if reply["type"] != "registered":
            raise RuntimeError(f"{self.id} not registered: {reply}")
        self.loop = asyncio.create_task(self.serve())

    async def serve(self) -> None:
        beat = asyncio.create_task(self.heartbeat())
        try:
            async for message in self.ws:
                await self.handle(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            beat.cancel()
            for runner in self.runners.values():
                if runner.task is not None:
                    runner.task.cancel()
            self.runners.clear()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(20)  # under the API's 90 s reap, like the real 30 s
            await self.ws.send(json.dumps({"type": "heartbeat", "slots_in_use": 0,
                                           "loaded_models": [], "frame_p95_ms": {}}))

    async def handle(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            runner = self.runners.get(message[1:FRAME_HEADER_BYTES])
            if runner is not None:
                self.dropped += runner.pending is not None
                runner.pending = message
                runner.wake.set()
            return
        control = json.loads(message)
        kind = control["type"]
        if kind == "open_session":
            sid = uuid.UUID(control["session_id"]).bytes
            self.stop(sid)
            runner = self.runners[sid] = Runner()
            runner.task = asyncio.create_task(self.render(runner))
            await self.ws.send(json.dumps({"type": "session_ready",
                                           "session_id": control["session_id"],
                                           "control_generation": control["control_generation"]}))
        elif kind == "close_session":
            frames = self.stop(uuid.UUID(control["session_id"]).bytes)
            await self.ws.send(json.dumps({
                "type": "session_closed", "session_id": control["session_id"],
                "control_generation": control["control_generation"], "frames": frames,
                "gpu_ms": frames * self.args.infer_ms, "duration_ms": 0, "category": "other"}))
        elif kind == "dispatch_job":
            asyncio.create_task(self.job(control))

    def stop(self, sid: bytes) -> int:
        runner = self.runners.pop(sid, None)
        if runner is None:
            return 0
        if runner.task is not None:
            runner.task.cancel()
        return runner.frames

    async def render(self, runner: Runner) -> None:
        while True:
            await runner.wake.wait()
            runner.wake.clear()
            frame, runner.pending = runner.pending, None
            if frame is None:
                continue
            async with self.gpu:
                await asyncio.sleep(self.args.infer_ms / 1000)
            # The canvas payload carries its send time; echoing it back is
            # what lets the browser side measure end-to-end latency.
            await self.ws.send(bytes([GENERATED_FRAME]) + frame[1:])
            runner.frames += 1

    async def job(self, control: dict) -> None:
        async with self.gpu:
            await asyncio.sleep(self.args.job_ms / 1000)
        upload = control["upload"]
        response = await self.http.put(upload["url"], content=PNG, headers=upload["headers"])
        report: dict = {"job_id": control["job_id"], "dispatch_token": control["dispatch_token"]}
        if response.is_success:
            report |= {"type": "job_done", "gpu_ms": self.args.job_ms, "duration_ms":
                       self.args.job_ms, "category": "other", "width": 8, "height": 8}
        else:
            report |= {"type": "job_failed", "reason": f"upload {response.status_code}"}
        with suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps(report))

    async def die(self) -> None:
        """Abrupt TCP loss, as when the process is killed."""
        self.ws.transport.abort()
        await self.loop

    async def close(self) -> None:
        await self.http.aclose()
        await self.ws.close()
        await self.loop


@asynccontextmanager
async def fleet(args: argparse.Namespace, count: int, slots: int):
    workers = [FakeWorker(args, number, slots) for number in range(count)]
    try:
        await asyncio.gather(*(worker.connect() for worker in workers))
        yield workers
    finally:
        await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)


class Browser:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.session = b""
        self.sent = 0
        self.latencies: list[float] = []
        self.interrupted = self.resumed = 0
        self.since_resume = 0
        self.close_code: int | None = None
        self.reader: asyncio.Task | None = None

    async def open(self, read: bool = True, **connect) -> bool:
        self.ws = await websockets.connect(self.args.api_ws + "/api/v1/realtime",
                                           max_size=2**21, **connect)
        await self.ws.send(json.dumps({"type": "open", "model_id": MODEL_ID,
                                       "params": {"prompt": "stress"}}))
        try:
            reply = json.loads(await self.ws.recv())
        except websockets.ConnectionClosed:
            self.close_code = self.ws.close_code
            return False
        if reply["type"] != "ready":
            self.close_code = reply.get("code")
            await self.ws.close()
            return False
        self.session = uuid.UUID(reply["session_id"]).bytes
        if read:
            self.reader = asyncio.create_task(self.receive())
        return True

    async def receive(self) -> None:
        with suppress(websockets.ConnectionClosed):
            async for message in self.ws:
                if isinstance(message, bytes):
                    (stamp,) = struct.unpack_from("d", message, FRAME_HEADER_BYTES)
                    self.latencies.append(time.monotonic() - stamp)
                    self.since_resume += 1
                elif (kind := json.loads(message)["type"]) == "interrupted":
                    self.interrupted += 1
                elif kind == "resumed":
                    self.resumed += 1
                    self.since_resume = 0
        self.close_code = self.ws.close_code

    async def stream(self, frames: int, fps: float, offset: float = 0.0,
                     pad: bytes = b"") -> None:
        start = time.monotonic() + offset
        with suppress(websockets.ConnectionClosed):
            for index in range(frames):
                await asyncio.sleep(max(0.0, start + index / fps - time.monotonic()))
                await self.ws.send(bytes([CANVAS_FRAME]) + self.session
                                   + struct.pack("d", time.monotonic()) + pad)
                self.sent += 1

    async def finish(self) -> None:
        with suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps({"type": "close"}))
        await self.ws.close()
        if self.reader is not None:
            await self.reader


Rows = list[tuple[str, str, str, str, bool | None]]


class Report:
    def __init__(self, scenario: str, rows: Rows) -> None:
        self.scenario, self.rows = scenario, rows

    def add(self, metric: str, value: object, ok: bool | None = None, limit: str = "") -> None:
        shown = f"{value:.1f}" if isinstance(value, float) else str(value)
        self.rows.append((self.scenario, metric, shown, limit, ok))

    def latency(self, browsers: list[Browser]) -> None:
        values = [s * 1000 for browser in browsers for s in browser.latencies]
        self.add("latency_p50_ms", pct(values, 50))
        self.add("latency_p95_ms", pct(values, 95), pct(values, 95) <= LATENCY_P95_MS,
                 f"<= {LATENCY_P95_MS}")
        self.add("latency_p99_ms", pct(values, 99))


async def open_all(args: argparse.Namespace, count: int) -> tuple[list[Browser], list[Browser]]:
    browsers = [Browser(args) for _ in range(count)]
    opened = await asyncio.gather(*(browser.open() for browser in browsers))
    ready = [browser for browser, ok in zip(browsers, opened) if ok]
    return ready, [browser for browser, ok in zip(browsers, opened) if not ok]


async def realtime_load(args, rng, report: Report, count: int, fps: float) -> tuple[int, int]:
    """Shared by sessions (oversubscribed) and throughput (at capacity)."""
    capacity = args.workers * args.slots
    async with fleet(args, args.workers, args.slots) as workers:
        ready, refused = await open_all(args, count)
        frames = int(args.seconds * fps)
        offsets = [rng.uniform(0, 1 / fps) for _ in ready]
        started = time.monotonic()
        await asyncio.gather(*(b.stream(frames, fps, o) for b, o in zip(ready, offsets)))
        await asyncio.sleep(0.5)
        elapsed = time.monotonic() - started
        await asyncio.gather(*(browser.finish() for browser in ready))
        expected = min(count, capacity)
        report.add("ready", len(ready), len(ready) == expected, f"== {expected}")
        refused_4003 = sum(b.close_code == CLOSE_NO_CAPACITY for b in refused)
        report.add("refused_4003", refused_4003, refused_4003 == count - expected,
                   f"== {count - expected}")
        sent = sum(browser.sent for browser in ready)
        rendered = [len(browser.latencies) for browser in ready]
        report.add("frames_sent", sent)
        report.add("frames_rendered", sum(rendered))
        report.add("dropped_latest_wins", sum(worker.dropped for worker in workers))
        report.add("rendered_per_s", sum(rendered) / elapsed)
        report.add("starved_sessions", rendered.count(0), 0 not in rendered, "== 0")
        report.add("fairness_jain", round(jain(rendered), 3), jain(rendered) >= 0.9, ">= 0.9")
        report.latency(ready)
        return sent, sum(rendered)


async def sessions(args, rng, report: Report) -> None:
    await realtime_load(args, rng, report, args.sessions, fps=10)


async def throughput(args, rng, report: Report) -> None:
    sent, rendered = await realtime_load(args, rng, report, args.workers * args.slots, fps=4)
    report.add("delivered_ratio", rendered / max(sent, 1), rendered >= 0.95 * sent, ">= 0.95")


async def worker_churn(args, rng, report: Report) -> None:
    """Sessions fill all but one worker's slots, so a lost worker always has
    somewhere to go. Two workers die at fixed times and come back 0.5 s later."""
    count, fps, seconds = (args.workers - 1) * args.slots, 4, 6.0
    victims = [rng.randrange(args.workers) for _ in range(2)]
    async with fleet(args, args.workers, args.slots) as workers:
        ready, _ = await open_all(args, count)

        async def chaos() -> None:
            for at, victim in zip((1.5, 3.5), victims):
                await asyncio.sleep(at - (time.monotonic() - started))
                await workers[victim].die()
                await asyncio.sleep(0.5)
                await workers[victim].connect()

        started = time.monotonic()
        await asyncio.gather(chaos(), *(b.stream(int(seconds * fps), fps) for b in ready))
        await asyncio.sleep(0.5)
        await asyncio.gather(*(browser.finish() for browser in ready))
        await asyncio.sleep(0.5)
        interrupted = sum(browser.interrupted for browser in ready)
        report.add("ready", len(ready), len(ready) == count, f"== {count}")
        report.add("interrupted", interrupted, interrupted > 0, "> 0")
        report.add("resumed", sum(b.resumed for b in ready),
                   all(b.resumed == b.interrupted for b in ready), "== interrupted")
        report.add("closed_4003", sum(b.close_code == CLOSE_NO_CAPACITY for b in ready),
                   not any(b.close_code == CLOSE_NO_CAPACITY for b in ready), "== 0")
        stuck = sum(b.interrupted > 0 and b.since_resume == 0 for b in ready)
        report.add("no_frame_after_resume", stuck, stuck == 0, "== 0")
        leaked = sum(len(worker.runners) for worker in workers)
        report.add("worker_runners_left", leaked, leaked == 0, "== 0")
        report.latency(ready)


async def conn_churn(args, rng, report: Report) -> None:
    kinds = ["silent", "garbage", "open-drop", "ready-close", "ready-abort"]
    plan = [rng.choice(kinds) for _ in range(args.churn)]
    codes: dict[str, list[int | None]] = {kind: [] for kind in kinds}
    gate = asyncio.Semaphore(20)

    async def one(kind: str) -> None:
        async with gate:
            ws = await websockets.connect(args.api_ws + "/api/v1/realtime")
            opening = json.dumps({"type": "open", "model_id": MODEL_ID,
                                  "params": {"prompt": "churn"}})
            with suppress(websockets.ConnectionClosed):
                if kind == "garbage":
                    await ws.send("not json")
                    await ws.recv()
                elif kind == "open-drop":
                    await ws.send(opening)
                elif kind in ("ready-close", "ready-abort"):
                    await ws.send(opening)
                    reply = json.loads(await ws.recv())
                    if kind == "ready-abort" and reply["type"] == "ready":
                        session = uuid.UUID(reply["session_id"]).bytes
                        for _ in range(3):
                            await ws.send(bytes([CANVAS_FRAME]) + session + bytes(8))
                        ws.transport.abort()
                        return
                    await ws.send(json.dumps({"type": "close"}))
            await ws.close()
            codes[kind].append(ws.close_code)

    async with fleet(args, args.workers, args.slots) as workers:
        rss_before, fds_before = process(args.api_pid)
        await asyncio.gather(*(one(kind) for kind in plan))
        await asyncio.sleep(2.0)
        leaked = sum(len(worker.runners) for worker in workers)
        capacity = args.workers * args.slots
        ready, _ = await open_all(args, capacity)
        await asyncio.gather(*(browser.finish() for browser in ready))
        await asyncio.sleep(1.0)
        rss_after, fds_after = process(args.api_pid)
    garbage = codes["garbage"]
    report.add("connections", len(plan))
    report.add("garbage_closed_4000", garbage.count(CLOSE_PROTOCOL_VIOLATION),
               garbage.count(CLOSE_PROTOCOL_VIOLATION) == len(garbage), f"== {len(garbage)}")
    report.add("worker_runners_left", leaked, leaked == 0, "== 0")
    report.add("capacity_after", len(ready), len(ready) == capacity, f"== {capacity}")
    if args.api_pid is not None:
        report.add("api_rss_growth_mb", rss_after - rss_before,
                   rss_after - rss_before <= RSS_GROWTH_MB, f"<= {RSS_GROWTH_MB}")
        report.add("api_fd_growth", fds_after - fds_before,
                   fds_after - fds_before <= FD_GROWTH, f"<= {FD_GROWTH}")


async def slow_consumer(args, rng, report: Report) -> None:
    """One browser stops reading while a neighbour on the same worker draws.
    Frames are the largest the API relays, so the backlog dwarfs the kernel's
    socket buffers and shows up in the API's resident set if it holds it.
    Random, because both ends negotiate permessage-deflate and a zero-filled
    megabyte compresses to almost nothing."""
    stall, fps, pad = 8.0, 10, rng.randbytes(MAX_CANVAS_PAYLOAD_BYTES - 8)
    async with fleet(args, 1, 2):
        slow, neighbour = Browser(args), Browser(args)
        # max_queue=1: the client library stops reading after one message, so
        # everything beyond the kernel buffers has to sit in the API.
        await slow.open(read=False, max_queue=1)
        await neighbour.open()
        rss_before, _ = process(args.api_pid)
        await asyncio.gather(slow.stream(int(stall * fps), fps, pad=pad),
                             neighbour.stream(int(stall * fps), fps))
        rss_peak, _ = process(args.api_pid)
        slow.reader = asyncio.create_task(slow.receive())
        await asyncio.sleep(3.0)
        await asyncio.gather(slow.finish(), neighbour.finish())
    report.add("slow_frames_sent", slow.sent)
    report.add("backlog_delivered", len(slow.latencies))
    # The oldest frame is not the API's to bound: whatever the kernel socket
    # buffers took in the first second of the stall still arrives, stamped
    # then. What the API decides is what comes after them, so the gate is the
    # newest frame once the backlog has had its 3 s to drain.
    report.add("oldest_frame_s", max(slow.latencies, default=0.0))
    newest = slow.latencies[-1] if slow.latencies else math.inf
    report.add("newest_frame_s", newest, newest <= STALE_MAX_S, f"<= {STALE_MAX_S}")
    report.latency([neighbour])
    if args.api_pid is not None:
        report.add("api_rss_growth_mb", rss_peak - rss_before,
                   rss_peak - rss_before <= RSS_GROWTH_MB, f"<= {RSS_GROWTH_MB}")


async def rest_burst(args, rng, report: Report) -> None:
    prompts = [f"stress {rng.randrange(10**6)}" for _ in range(args.jobs)]
    async with fleet(args, args.workers, args.slots), \
            httpx.AsyncClient(base_url=args.api, timeout=60) as client:
        started = time.monotonic()

        async def one(prompt: str) -> tuple[int, str, float]:
            response = await client.post("/api/v1/generations", json={
                "model_id": MODEL_ID, "params": {"prompt": prompt}})
            if response.status_code != 202:
                return response.status_code, "refused", 0.0
            job_id = response.json()["job_id"]
            while time.monotonic() - started < 60:
                await asyncio.sleep(0.25)
                polled = await client.get(f"/api/v1/generations/{job_id}")
                state = polled.json()["state"] if polled.is_success else "unknown"
                if state in ("succeeded", "failed", "cancelled"):
                    return 202, state, time.monotonic() - started
            return 202, "timeout", 60.0

        results = await asyncio.gather(*(one(prompt) for prompt in prompts))
    accepted = sum(status == 202 for status, _, _ in results)
    succeeded = [seconds for _, state, seconds in results if state == "succeeded"]
    report.add("accepted_202", accepted, accepted == args.jobs, f"== {args.jobs}")
    report.add("succeeded", len(succeeded), len(succeeded) == args.jobs, f"== {args.jobs}")
    report.add("done_p50_s", pct(succeeded, 50))
    report.add("done_p95_s", pct(succeeded, 95))
    report.add("jobs_per_s", len(succeeded) / max(max(succeeded, default=1.0), 0.001))


async def main(args: argparse.Namespace) -> int:
    rows: Rows = []
    for name in args.scenario or SCENARIOS:
        report = Report(name, rows)
        # One RNG per scenario, so selecting a subset does not shift the others.
        rng = random.Random(f"{args.seed}:{name}")
        runner = globals()[name.replace("-", "_")]
        try:
            await asyncio.wait_for(runner(args, rng, report), 120)
        except Exception as error:
            report.add("error", f"{type(error).__name__}: {error}"[:60], False)
        print(f"{name} done", file=sys.stderr, flush=True)
    print(f"{'scenario':<14} {'metric':<22} {'value':>10}  {'threshold':<16} result")
    for scenario, metric, value, limit, ok in rows:
        verdict = "" if ok is None else "PASS" if ok else "FAIL"
        print(f"{scenario:<14} {metric:<22} {value:>10}  {limit:<16} {verdict}")
    return 1 if any(ok is False for *_, ok in rows) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--fleet-token", default=os.environ.get("FLEET_TOKEN"))
    parser.add_argument("--api-pid", type=int, help="API process, for RSS and fd checks")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS,
                        help="repeatable; all when omitted")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--slots", type=int, default=8, help="realtime slots per worker")
    parser.add_argument("--sessions", type=int, default=48, help="browsers in `sessions`")
    parser.add_argument("--seconds", type=float, default=4.0, help="streaming time")
    parser.add_argument("--churn", type=int, default=200, help="connections in conn-churn")
    parser.add_argument("--jobs", type=int, default=100, help="POSTs in rest-burst")
    parser.add_argument("--infer-ms", type=int, default=20, help="fake frame time")
    parser.add_argument("--job-ms", type=int, default=50, help="fake job time")
    parsed = parser.parse_args()
    if not parsed.fleet_token:
        parser.error("--fleet-token or FLEET_TOKEN is required")
    parsed.api = parsed.api.rstrip("/")
    parsed.api_ws = "ws" + parsed.api.removeprefix("http")
    sys.exit(asyncio.run(main(parsed)))
