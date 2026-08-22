#!/usr/bin/env python3
"""Shipped Engine.frame() p95 and GPU-lock stages, with a real PromptCache.

Not CI. Replaces the prototype-batch-sweep 160.2 / 219.8 pair as a baseline:
that script passes prompts as strings and times sketch map plus WebP with the
pipeline call. This script times DiffusersEngine.frame() the way the worker
runs it. gpu_ms is occupancy under the GPU lock; wall_ms includes codec work
off that lock.

  worker/.venv/bin/python scripts/profile-realtime-frame.py
  worker/.venv/bin/python scripts/profile-realtime-frame.py --models vega-rt
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def mean_stages(rows: list[dict[str, int] | None]) -> dict[str, float] | None:
    present = [row for row in rows if row is not None]
    if not present:
        return None
    keys = present[0].keys()
    return {
        key: round(sum(row[key] for row in present) / len(present), 1)
        for key in keys
    }


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


def defaults(manifest, prompt: str, seed: int) -> dict:
    properties = manifest.parameters.get("properties", {})
    return {
        "prompt": prompt,
        "steps": int(properties.get("steps", {}).get("default", 2)),
        "structure_strength": float(
            properties.get("structure_strength", {}).get("default", 1.0)
        ),
        "seed": seed,
        "guidance": 0.0,
    }


async def profile_model(
    device: str,
    models_dir: str,
    manifest,
    samples: int,
    prompt: str,
    seed: int,
) -> dict:
    os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
    from worker.engine import DiffusersEngine, PromptCache

    engine = DiffusersEngine(device, memory_mode="full", models_dir=models_dir)
    await engine.load_model(manifest)
    await engine.ensure_realtime_resident(manifest)
    payload = canvas_bytes()
    params = defaults(manifest, prompt, seed)
    cache = PromptCache()
    await engine.frame(manifest, params, payload, prompt_cache=cache)
    miss_cache = PromptCache()
    miss_params = dict(params)
    miss_params["prompt"] = f"{prompt} cache miss"
    miss = await engine.frame(
        manifest, miss_params, payload, prompt_cache=miss_cache, profile=True,
    )
    gpu_ms: list[int] = []
    wall_ms: list[float] = []
    stages: list[dict[str, int] | None] = []
    for _ in range(samples):
        started = time.perf_counter()
        result = await engine.frame(
            manifest, params, payload, prompt_cache=cache, profile=True,
        )
        wall_ms.append((time.perf_counter() - started) * 1000.0)
        gpu_ms.append(result.gpu_ms)
        stages.append(result.stages)
    await engine.unload_all()
    p95_gpu_ms = percentile([float(value) for value in gpu_ms], 95.0)
    return {
        "model": manifest.id,
        "samples": samples,
        "warmup_discarded": True,
        "cache_miss": {
            "gpu_ms": miss.gpu_ms,
            "stages": miss.stages,
        },
        "hit_stages_mean": mean_stages(stages),
        "samples_gpu_ms": gpu_ms,
        "samples_wall_ms": [round(value, 1) for value in wall_ms],
        "gpu_median_ms": statistics.median(gpu_ms) if gpu_ms else None,
        "gpu_p95_ms": p95_gpu_ms,
        "wall_median_ms": statistics.median(wall_ms) if wall_ms else None,
        "wall_p95_ms": percentile(wall_ms, 95.0),
        "slots_at_500": math.floor(500 / p95_gpu_ms) if p95_gpu_ms else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default=str(ROOT / "worker" / "models"))
    parser.add_argument("--device", default="rocm", choices=("rocm", "cuda", "cpu"))
    parser.add_argument("--models", default="sdxl-turbo,vega-rt")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--prompt", default="a red cube on a table, studio light")
    parser.add_argument("--out", default="")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    from worker.manifests import load_manifests

    if args.samples <= 0:
        raise SystemExit("--samples must be positive")

    manifests = {manifest.id: manifest for manifest in load_manifests(args.models_dir)}
    wanted = [item.strip() for item in args.models.split(",") if item.strip()]
    missing = [item for item in wanted if item not in manifests]
    if missing:
        raise SystemExit(f"unknown models: {missing}")
    cases = [
        asyncio.run(profile_model(
            args.device, args.models_dir, manifests[model_id], args.samples,
            args.prompt, args.seed,
        ))
        for model_id in wanted
    ]
    text = json.dumps({"cases": cases}, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
