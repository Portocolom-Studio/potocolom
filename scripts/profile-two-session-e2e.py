#!/usr/bin/env python3
"""Two realtime sessions through the real API and one GPU worker.

Not CI. Times ping-pong round trips on the realtime WebSocket: send a real
canvas image, wait for the generated frame, both sessions in lockstep.
gpu_ms from Engine.frame is occupancy under the GPU lock. This script times
browser-to-browser wall clock against the 500 ms bar.

  worker/.venv/bin/python scripts/profile-two-session-e2e.py
  worker/.venv/bin/python scripts/profile-two-session-e2e.py --models vega-rt
  worker/.venv/bin/python scripts/profile-two-session-e2e.py --sim
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import websockets
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
CANVAS_FRAME = 0x01
GENERATED_FRAME = 0x02
FRAME_HEADER_BYTES = 17
BAR_MS = 500
CLOSE_NO_CAPACITY = 4003
FLEET_TOKEN = "test-fleet-token"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def canvas_bytes() -> bytes:
    image = Image.new("RGB", (512, 512), "white")
    pen = ImageDraw.Draw(image)
    pen.line(
        [(10, 210), (150, 140), (300, 180), (500, 150)],
        fill=(17, 24, 39),
        width=5,
    )
    pen.ellipse([(150, 300), (360, 380)], outline=(17, 24, 39), width=5)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def canvas_frame(session_id: uuid.UUID, image: bytes) -> bytes:
    return bytes([CANVAS_FRAME]) + session_id.bytes + image


def session_stats(latencies: list[float], sent: int, received: int) -> dict:
    return {
        "sent": sent,
        "received": received,
        "rtt_median_ms": statistics.median(latencies) if latencies else None,
        "rtt_p95_ms": percentile(latencies, 95.0) if latencies else None,
        "rtt_max_ms": max(latencies) if latencies else None,
        "samples_rtt_ms": [round(value, 1) for value in latencies],
    }


def recv_gaps(recv_at: list[list[float]]) -> list[float]:
    if len(recv_at) < 2 or any(not row for row in recv_at):
        return []
    count = min(len(row) for row in recv_at)
    gaps = []
    for index in range(count):
        times = [row[index] for row in recv_at]
        gaps.append((max(times) - min(times)) * 1000.0)
    return gaps


def inside_bar(p95_values: list[float], bar_ms: float = BAR_MS) -> bool:
    return bool(p95_values) and all(value <= bar_ms for value in p95_values)


def close_code(reply: object) -> int | None:
    rcvd = getattr(reply, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, int):
        return code
    sent = getattr(reply, "sent", None)
    code = getattr(sent, "code", None)
    if isinstance(code, int):
        return code
    return None


def is_no_capacity(reply: object) -> bool:
    if isinstance(reply, dict):
        return reply.get("type") == "error" and reply.get("code") == CLOSE_NO_CAPACITY
    return close_code(reply) == CLOSE_NO_CAPACITY


def advertised_slots(log_text: str, model_id: str) -> int | None:
    marker = f"warmup realtime model={model_id} slots="
    hits: list[int] = []
    for line in log_text.splitlines():
        if marker not in line:
            continue
        tail = line.split(marker, 1)[1].strip().split()[0]
        if tail.isdigit():
            hits.append(int(tail))
    return hits[-1] if hits else None


def interpreter(component: str) -> str:
    venv = ROOT / component / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def isolate_model(models_dir: Path, model_id: str, dest: Path) -> Path:
    src = models_dir / f"{model_id}.json"
    if not src.is_file():
        raise SystemExit(f"no manifest {src}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest / src.name)
    return dest


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def tail_text(path: Path, limit: int = 80) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def spawn_api(port: int, storage: Path) -> subprocess.Popen:
    env = os.environ | {
        "AUTH_MODE": "none",
        "FLEET_TOKEN_KEY": FLEET_TOKEN,
        "TELEMETRY": "false",
        "BENCHMARK_API": "0",
        "STORAGE_LOCAL_PATH": str(storage),
        "PUBLIC_URL": f"http://127.0.0.1:{port}",
        "ALLOWED_ORIGINS": "",
    }
    log = open(storage / "api.log", "w")
    return subprocess.Popen(
        [interpreter("backend"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / "backend",
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def spawn_worker(
    port: int,
    *,
    models_dir: str,
    device: str,
    slots: int,
    log_path: Path,
    sim: bool,
) -> subprocess.Popen:
    env = dict(os.environ)
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        env.pop(key, None)
    env.update({
        "API_URL": f"ws://127.0.0.1:{port}/api/v1/fleet",
        "WORKER_ID": "worker-e2e",
        "FLEET_TOKEN": FLEET_TOKEN,
        "REALTIME_SLOTS": str(slots),
        "HEARTBEAT_SECONDS": "5",
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
    })
    if sim:
        env["DEVICE"] = "cpu"
        env["INFERENCE_SECONDS"] = "0.12"
        env.pop("MODELS_DIR", None)
    else:
        env["DEVICE"] = device
        env["MODELS_DIR"] = models_dir
    log = open(log_path, "w")
    return subprocess.Popen(
        [interpreter("worker"), "-m", "worker"],
        cwd=ROOT / "worker",
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


async def wait_for_api(port: int, process: subprocess.Popen) -> None:
    url = f"http://127.0.0.1:{port}/api/v1/health"
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError(f"the API exited during startup ({process.returncode})")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError("the API never answered its health check")


def fetch_models(port: int) -> list[dict]:
    url = f"http://127.0.0.1:{port}/api/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("models", [])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def model_p95(models: list[dict], model_id: str) -> int | None:
    for item in models:
        if item.get("id") != model_id:
            continue
        value = item.get("realtime_p95_ms")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return round(value)
    return None


async def wait_frame(ws, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no generated frame")
        message = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(message, bytes):
            if len(message) >= FRAME_HEADER_BYTES and message[0] == GENERATED_FRAME:
                return message
            continue
        control = json.loads(message)
        kind = control.get("type")
        if kind in {"error", "session_refused", "resumed"}:
            raise RuntimeError(control)


async def ping_pong(
    ws, session_id: uuid.UUID, image: bytes, timeout: float,
) -> tuple[float, float]:
    started = time.monotonic()
    await ws.send(canvas_frame(session_id, image))
    await wait_frame(ws, timeout)
    finished = time.monotonic()
    return (finished - started) * 1000.0, finished


async def open_session(
    port: int,
    model_id: str,
    prompt: str,
    seed: int,
    *,
    retries: int,
    worker: subprocess.Popen,
    log_path: Path,
    fail_on_no_capacity: bool = False,
) -> tuple:
    last: object = None
    url = f"ws://127.0.0.1:{port}/api/v1/realtime"
    for attempt in range(retries):
        if worker.poll() is not None:
            raise RuntimeError(
                f"worker exited ({worker.returncode})\n{tail_text(log_path)}"
            )
        ws = None
        try:
            ws = await websockets.connect(url, max_size=None, open_timeout=5)
            await ws.send(json.dumps({
                "type": "open",
                "model_id": model_id,
                "params": {"prompt": prompt, "seed": seed, "guidance": 0.0},
            }))
            raw = await asyncio.wait_for(ws.recv(), 20)
            reply = json.loads(raw)
            if reply.get("type") == "ready":
                return ws, uuid.UUID(reply["session_id"])
            last = reply
            await ws.close()
            ws = None
            if fail_on_no_capacity and is_no_capacity(reply):
                raise RuntimeError(f"no worker capacity: {reply}")
        except RuntimeError:
            raise
        except Exception as error:
            last = error
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if fail_on_no_capacity and is_no_capacity(error):
                raise RuntimeError("no worker capacity") from error
        if attempt == 0 or attempt % 15 == 14:
            print(f"waiting for {model_id} ready ({attempt + 1}s)", flush=True)
        await asyncio.sleep(1)
    raise RuntimeError(f"could not open {model_id}: {last}\n{tail_text(log_path)}")


async def measure_case(
    port: int,
    model_id: str,
    sessions: int,
    samples: int,
    prompt: str,
    seed: int,
    frame_timeout: float,
    retries: int,
    worker: subprocess.Popen,
    log_path: Path,
) -> dict:
    image = canvas_bytes()
    clients: list[tuple] = []
    refusal: str | None = None
    try:
        try:
            for index in range(sessions):
                ws, session_id = await open_session(
                    port, model_id, f"{prompt} session {index + 1}", seed + index,
                    retries=retries, worker=worker, log_path=log_path,
                    fail_on_no_capacity=bool(clients),
                )
                clients.append((ws, session_id))
        except RuntimeError as error:
            if not clients:
                raise
            refusal = str(error)
        latencies: list[list[float]] = [[] for _ in clients]
        recv_at: list[list[float]] = [[] for _ in clients]
        sent = [0] * len(clients)
        received = [0] * len(clients)
        if len(clients) == sessions:
            await asyncio.gather(*[
                ping_pong(ws, session_id, image, frame_timeout)
                for ws, session_id in clients
            ])
            for _ in range(samples):
                round_hits = await asyncio.gather(*[
                    ping_pong(ws, session_id, image, frame_timeout)
                    for ws, session_id in clients
                ])
                for index, (rtt, arrived) in enumerate(round_hits):
                    latencies[index].append(rtt)
                    recv_at[index].append(arrived)
                    sent[index] += 1
                    received[index] += 1
    finally:
        for ws, _session_id in clients:
            try:
                await ws.send(json.dumps({"type": "close"}))
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
    reports = [
        {"session": index, **session_stats(latencies[index], sent[index], received[index])}
        for index in range(len(clients))
    ]
    p95s = [
        report["rtt_p95_ms"] for report in reports
        if report["rtt_p95_ms"] is not None
    ]
    gaps = recv_gaps(recv_at)
    models = fetch_models(port)
    advertised_p95 = model_p95(models, model_id)
    slots = advertised_slots(log_path.read_text(errors="replace"), model_id)
    max_p95 = max(p95s) if p95s else None
    return {
        "model": model_id,
        "admitted": len(clients),
        "requested": sessions,
        "refusal": refusal,
        "samples": samples,
        "warmup_discarded": len(clients) == sessions and samples > 0,
        "advertised_realtime_p95_ms": advertised_p95,
        "advertised_slots": slots,
        "sessions": reports,
        "max_rtt_p95_ms": max_p95,
        "recv_gap_median_ms": statistics.median(gaps) if gaps else None,
        "recv_gap_p95_ms": percentile(gaps, 95.0) if gaps else None,
        "inside_bar": inside_bar(p95s) if len(clients) == sessions else False,
        "bar_ms": BAR_MS,
    }


async def run_models(args: argparse.Namespace) -> dict:
    port = free_port()
    cases = []
    with tempfile.TemporaryDirectory(prefix="potocolom-e2e-") as raw:
        root = Path(raw)
        storage = root / "storage"
        storage.mkdir()
        api = spawn_api(port, storage)
        worker = None
        try:
            await wait_for_api(port, api)
            wanted = ["sd-sim"] if args.sim else [
                item.strip() for item in args.models.split(",") if item.strip()
            ]
            if not wanted:
                raise SystemExit("no models")
            for model_id in wanted:
                isolated = root / "models" / model_id
                if isolated.exists():
                    shutil.rmtree(isolated)
                models_dir = ""
                if not args.sim:
                    isolate_model(Path(args.models_dir), model_id, isolated)
                    models_dir = str(isolated)
                log_path = root / f"worker-{model_id}.log"
                stop(worker)
                worker = spawn_worker(
                    port,
                    models_dir=models_dir,
                    device=args.device,
                    slots=args.slots,
                    log_path=log_path,
                    sim=args.sim,
                )
                retries = 60 if args.sim else args.ready_timeout
                case = await measure_case(
                    port, model_id, args.sessions, args.samples, args.prompt,
                    args.seed, args.frame_timeout, retries, worker, log_path,
                )
                cases.append(case)
        finally:
            stop(worker)
            stop(api)
    return {
        "bar_ms": BAR_MS,
        "sessions": args.sessions,
        "samples": args.samples,
        "mode": "sim" if args.sim else "gpu",
        "device": "cpu" if args.sim else args.device,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default=str(ROOT / "worker" / "models"))
    parser.add_argument("--device", default="rocm", choices=("rocm", "cuda", "cpu"))
    parser.add_argument("--models", default="sdxl-turbo,vega-rt")
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--prompt", default="a red cube on a table, studio light")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--frame-timeout", type=float, default=30.0)
    parser.add_argument("--ready-timeout", type=int, default=1200)
    parser.add_argument("--out", default="")
    parser.add_argument("--sim", action="store_true")
    args = parser.parse_args()
    if args.sessions < 1 or args.samples < 1:
        raise SystemExit("--sessions and --samples must be positive")
    payload = asyncio.run(run_models(args))
    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    failed = [
        case["model"] for case in payload["cases"]
        if not case.get("inside_bar")
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
