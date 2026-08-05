#!/usr/bin/env python3
"""A/B torch.compile on DiffusersEngine (ROCm/CUDA). Not part of CI.

Usage (from repo root, worker venv with inference extra):

  worker/.venv/bin/python scripts/compile_ab.py \\
    --models-dir worker/models --device rocm \\
    --models vega-rt,sdxl-fast --repeats 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from worker.engine import DiffusersEngine  # noqa: E402
from worker.manifests import load_manifests  # noqa: E402


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def run_case(
    *,
    device: str,
    models_dir: str,
    manifest,
    torch_compile: bool,
    repeats: int,
    prompt: str,
) -> dict:
    engine = DiffusersEngine(
        device,
        memory_mode="full",
        models_dir=models_dir,
        torch_compile=torch_compile,
        attention_backend="_native_efficient" if torch_compile else "",
    )
    props = manifest.parameters.get("properties", {})
    width = (props.get("width") or {}).get("default") or 512
    height = (props.get("height") or {}).get("default") or 512
    steps = (props.get("steps") or {}).get("default") or 2
    guidance = (props.get("guidance") or {}).get("default") or 0.0
    params = {
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "guidance": float(guidance),
        "seed": 42,
    }

    async def scenario():
        load_ms_local = await engine.load_model(manifest)
        t0 = time.monotonic()
        cold = await engine.generate(manifest, params, lambda _: None)
        cold_wall_ms = (time.monotonic() - t0) * 1000.0
        warm_gpu: list[int] = []
        warm_wall: list[float] = []
        for _ in range(repeats):
            t1 = time.monotonic()
            result = await engine.generate(manifest, params, lambda _: None)
            warm_wall.append((time.monotonic() - t1) * 1000.0)
            warm_gpu.append(result.gpu_ms)
        await engine.unload_all()
        return load_ms_local, cold, cold_wall_ms, warm_gpu, warm_wall

    total_started = time.monotonic()
    load_ms, cold, cold_wall_ms, warm_gpu, warm_wall = asyncio.run(scenario())
    total_s = time.monotonic() - total_started
    return {
        "model": manifest.id,
        "torch_compile": torch_compile,
        "width": params["width"],
        "height": params["height"],
        "steps": params["steps"],
        "load_ms": load_ms,
        "cold_gpu_ms": cold.gpu_ms,
        "cold_wall_ms": round(cold_wall_ms, 1),
        "warm_gpu_ms": warm_gpu,
        "warm_wall_ms": [round(v, 1) for v in warm_wall],
        "warm_gpu_median": statistics.median(warm_gpu) if warm_gpu else None,
        "warm_gpu_p95": percentile([float(v) for v in warm_gpu], 95.0) if warm_gpu else None,
        "warm_wall_median": statistics.median(warm_wall) if warm_wall else None,
        "total_s": round(total_s, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default=str(ROOT / "worker" / "models"))
    parser.add_argument("--device", default="rocm", choices=("rocm", "cuda"))
    parser.add_argument("--models", default="vega-rt,sdxl-fast")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--prompt", default="a red cube on a table, studio light")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    manifests = {m.id: m for m in load_manifests(args.models_dir)}
    wanted = [item.strip() for item in args.models.split(",") if item.strip()]
    missing = [item for item in wanted if item not in manifests]
    if missing:
        raise SystemExit(f"unknown models: {missing}")

    rows: list[dict] = []
    for model_id in wanted:
        for compile_on in (False, True):
            label = "ON" if compile_on else "OFF"
            print(f"\n=== {model_id} torch_compile={label} ===", flush=True)
            row = run_case(
                device=args.device,
                models_dir=args.models_dir,
                manifest=manifests[model_id],
                torch_compile=compile_on,
                repeats=args.repeats,
                prompt=args.prompt,
            )
            print(json.dumps(row, indent=2), flush=True)
            rows.append(row)

    summary = []
    by_model: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_model.setdefault(row["model"], {})["ON" if row["torch_compile"] else "OFF"] = row
    for model_id, pair in by_model.items():
        off = pair.get("OFF")
        on = pair.get("ON")
        if not off or not on or off["warm_gpu_median"] is None or on["warm_gpu_median"] is None:
            continue
        off_m = float(off["warm_gpu_median"])
        on_m = float(on["warm_gpu_median"])
        speedup = None if on_m <= 0 else (off_m / on_m)
        summary.append({
            "model": model_id,
            "off_median_gpu_ms": off_m,
            "on_median_gpu_ms": on_m,
            "speedup_x": round(speedup, 3) if speedup is not None else None,
            "pct_faster": round((1.0 - on_m / off_m) * 100.0, 1) if off_m else None,
            "off_load_ms": off["load_ms"],
            "on_load_ms": on["load_ms"],
            "off_total_s": off["total_s"],
            "on_total_s": on["total_s"],
        })

    report = {"cases": rows, "summary": summary}
    text = json.dumps(report, indent=2)
    print("\n=== SUMMARY ===", flush=True)
    print(text, flush=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
