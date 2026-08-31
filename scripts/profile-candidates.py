#!/usr/bin/env python3
"""Candidate-model latency and VRAM envelope through DiffusersEngine.

Companion to profile-realtime-frame.py for manifests that cannot enter
engine.frame() yet because they declare no t2i_adapter. Times engine.generate()
the way queued jobs run it (t2i at the manifest default, i2i at 512 px), then
repeats 512 px i2i as the canvas-tick analog, collecting torch peak memory per
phase so rung decisions have numbers. Not CI.

Defaults to --memory-mode full: a model that does not fit raises instead of
sliding down the ladder, because the group_offload rung streams every leaf
module from disk per step and on a card that also drives the display it makes
no progress while holding all the VRAM. One model failing no longer cancels
the rest of the run.

  worker/.venv/bin/python scripts/profile-candidates.py \
      --models flux2-klein-4b,z-image-turbo,sana-sprint-06b
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

GB = 1024**3

# Free VRAM a phase wants before it starts. The engine's own guard sizes a
# model's weights at rest (min_vram_gb) and can only evict, never refuse, so
# nothing upstream sees the activations a single call needs: sana-sprint-06b
# asked for 4.50 GiB of them after its weights were already resident.
PHASE_HEADROOM_GB = 2.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered) + 0.5)))
    return ordered[min(len(ordered), rank) - 1]


def canvas_bytes(size: int = 512) -> bytes:
    image = Image.new("RGB", (size, size), "white")
    pen = ImageDraw.Draw(image)
    pen.line(
        [(10, size * 0.4), (size * 0.3, size * 0.27),
         (size * 0.6, size * 0.35), (size - 10, size * 0.29)],
        fill=(17, 24, 39),
        width=5,
    )
    pen.ellipse(
        [(size * 0.3, size * 0.6), (size * 0.7, size * 0.75)],
        outline=(17, 24, 39),
        width=5,
    )
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def schema_default(manifest, key: str, fallback):
    return manifest.parameters.get("properties", {}).get(key, {}).get("default", fallback)


async def run_phase(engine, torch, manifest, label: str, params: dict,
                    samples: int, input_image: bytes | None = None,
                    save_path: Path | None = None) -> dict:

    def progress(_fraction: float) -> None:
        pass

    await engine.generate(manifest, dict(params), progress, input_image=input_image)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    gpu_ms: list[int] = []
    wall_ms: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        result = await engine.generate(
            manifest, dict(params), progress, input_image=input_image)
        wall_ms.append((time.perf_counter() - started) * 1000.0)
        gpu_ms.append(result.gpu_ms)
        if save_path is not None:
            save_path.write_bytes(result.data)
            save_path = None
    phase = {
        "label": label,
        "params": {k: v for k, v in params.items() if k != "prompt"},
        "samples": samples,
        "gpu_median_ms": statistics.median(gpu_ms),
        "gpu_p95_ms": percentile([float(v) for v in gpu_ms], 95.0),
        "wall_p95_ms": percentile(wall_ms, 95.0),
        "peak_gb": round(torch.cuda.max_memory_allocated() / GB, 2)
        if torch.cuda.is_available() else None,
    }
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    return phase


async def profile_model(device: str, models_dir: str, manifest,
                        samples: int, prompt: str, seed: int,
                        save_dir: Path | None, memory_mode: str = "full") -> dict:
    os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
    import torch

    from worker.engine import DiffusersEngine

    engine = DiffusersEngine(device, memory_mode=memory_mode, models_dir=models_dir)
    case: dict = {"model": manifest.id}
    if torch.cuda.is_available():
        free, _total = torch.cuda.mem_get_info()
        case["free_before_gb"] = round(free / GB, 2)
    started = time.perf_counter()
    await engine.load_model(manifest)
    case["load_s"] = round(time.perf_counter() - started, 2)
    case["rung"] = engine.model_rung(manifest.id)
    if case["rung"] == "group_offload":
        await engine.unload_all()
        case["skipped"] = (
            "group_offload streams every leaf module from disk per step. On a "
            "card that also drives the display this produces no image, holds "
            "the whole VRAM and wedges the driver. Refusing to run it."
        )
        return case

    default_width = int(schema_default(manifest, "width", 1024))
    t2i_params = {
        "prompt": prompt,
        "steps": int(schema_default(manifest, "steps", 4)),
        "guidance": float(schema_default(manifest, "guidance", 0)),
        "width": default_width,
        "height": default_width,
        "seed": seed,
    }
    case["phases"] = []

    async def phase(label: str, params: dict, **kwargs) -> None:
        # Each phase is banked as it completes. A later phase that runs out of
        # memory then costs its own numbers, not the whole model's: the first
        # sana-sprint-06b run measured t2i and threw it away when i2i OOMed.
        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info()
            if free < PHASE_HEADROOM_GB * GB:
                case["phases"].append({
                    "label": label,
                    "skipped": f"only {free / GB:.2f} GB free, want "
                               f"{PHASE_HEADROOM_GB:.2f} GB before starting a phase",
                })
                return
        try:
            case["phases"].append(await run_phase(
                engine, torch, manifest, label, params, samples, **kwargs))
        except Exception as error:
            case["phases"].append(
                {"label": label, "failed": f"{type(error).__name__}: {error}"})
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    await phase(
        f"t2i-{default_width}", t2i_params,
        save_path=save_dir / f"{manifest.id}-t2i.png" if save_dir else None,
    )

    small = dict(t2i_params, width=512, height=512)
    if "image_to_image" in manifest.capabilities:
        i2i_params = dict(small, strength=float(schema_default(manifest, "strength", 0.7)))
        await phase(
            "i2i-512-frame-analog", i2i_params, input_image=canvas_bytes(),
            save_path=save_dir / f"{manifest.id}-i2i.png" if save_dir else None,
        )
    else:
        await phase("t2i-512", small)

    await engine.unload_all()
    if torch.cuda.is_available():
        free_after, _total = torch.cuda.mem_get_info()
        case["free_after_gb"] = round(free_after / GB, 2)
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default=str(ROOT / "worker" / "models"))
    parser.add_argument("--device", default="rocm", choices=("rocm", "cuda", "cpu"))
    parser.add_argument(
        "--models", default="flux2-klein-4b,z-image-turbo,sana-sprint-06b")
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--prompt", default="a red cube on a table, studio light")
    parser.add_argument("--out", default="")
    parser.add_argument("--save-dir", default="")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--memory-mode", default="full",
        choices=("full", "auto", "model_offload", "group_offload"),
    )
    args = parser.parse_args()
    from worker.manifests import load_manifests

    if args.samples <= 0:
        raise SystemExit("--samples must be positive")

    manifests = {m.id: m for m in load_manifests(args.models_dir)}
    wanted = [item.strip() for item in args.models.split(",") if item.strip()]
    missing = [item for item in wanted if item not in manifests]
    if missing:
        raise SystemExit(f"unknown models: {missing}")
    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    for model_id in wanted:
        try:
            cases.append(asyncio.run(profile_model(
                args.device, args.models_dir, manifests[model_id], args.samples,
                args.prompt, args.seed, save_dir, args.memory_mode,
            )))
        except Exception as error:
            cases.append({"model": model_id, "failed": f"{type(error).__name__}: {error}"})
    text = json.dumps({"cases": cases}, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
