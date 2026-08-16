"""Compare two canvas-conditioning mechanisms for the realtime bar.

The realtime engine currently runs img2img on the user's drawing. A strength
sweep on the production model proved there is no useful middle setting: 0.70,
0.85 and 0.95 all return the line drawing, and 1.0 produces a scene that
ignores the drawing. This prototype feeds the canvas to a conditioning model
attached to a fresh text-to-image latent instead of an init image, and
measures whether either mechanism works on this hardware (a ~8 GB ROCm card)
before any production code is written.

Two mechanisms are compared, exactly one per invocation (--arm):

- adapter: TencentARC/t2i-adapter-sketch-sdxl-1.0 on
  StableDiffusionXLAdapterPipeline. The adapter computes its conditioning
  features once before the denoising loop, so its cost is roughly constant in
  step count.
- controlnet: xinsir/controlnet-scribble-sdxl-1.0 on
  StableDiffusionXLControlNetPipeline. The ControlNet forward runs inside the
  denoising loop, so its cost scales with step count.

Both arms compose onto the same base pipeline (built as in
worker/worker/engine.py: Segmind-Vega, fp16, the VegaRT LCM LoRA fused in,
LCMScheduler) with from_pipe, so the UNet, text encoders and VAE are shared,
never duplicated. Loading both arms in one process would double the resident
weights and bias the allocator, so the script loads exactly one per run.

Each arm sweeps steps x conditioning scale x conversion variant across the
five real WebP canvas fixtures, then runs a 60-frame sustained stream (the
stream's per-frame timings are the only honest latency distribution; the
sweep is one shot per cell, for image quality comparison), then re-runs the
chosen configuration on fixed finalist seeds so a lucky seed cannot flatter
one arm. Outputs PNGs under .local/prototype-out/<arm>/ and machine-readable
latency/memory lines on stdout, every summary line prefixed with the arm so
two logs can be concatenated.

Run from the repository root (a CUDA-visible ROCm device is required):

    ./worker/.venv/bin/python scripts/prototype-canvas-conditioning.py --arm adapter
    ./worker/.venv/bin/python scripts/prototype-canvas-conditioning.py --arm controlnet
    ./worker/.venv/bin/python scripts/prototype-canvas-conditioning.py --arm controlnet --skip-sweep
    ./worker/.venv/bin/python scripts/prototype-canvas-conditioning.py --arm adapter \\
        --fixture house --steps 3 --scale 1.0 --frames 10 --seeds 2
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch
from diffusers import (
    AutoencoderKL,
    ControlNetModel,
    LCMScheduler,
    StableDiffusionXLAdapterPipeline,
    StableDiffusionXLControlNetPipeline,
    StableDiffusionXLPipeline,
    T2IAdapter,
)
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / ".local" / "fixtures"
OUT_DIR = ROOT / ".local" / "prototype-out"

SIZE = 512  # the realtime bar is 512 px (docs/decisions.md)
BASE_MODEL = "segmind/Segmind-Vega"
VAE_MODEL = "madebyollin/sdxl-vae-fp16-fix"  # fp16-safe, no decode upcast
LORA_REPO = "segmind/Segmind-VegaRT"
LORA_FILE = "pytorch_lora_weights.safetensors"
ADAPTER_MODEL = "TencentARC/t2i-adapter-sketch-sdxl-1.0"
CONTROLNET_MODEL = "xinsir/controlnet-scribble-sdxl-1.0"
PROMPT = (
    "a cosy stone cottage in a green valley at sunset, oil painting, warm "
    "golden light, dramatic sky, highly detailed"
)
SEED = 1337  # fixed so every combination is comparable
SEEDS = (1337, 2718, 3141, 6174, 9091)  # fixed table, sliced by --seeds
FIXTURES = ("blank", "sparse", "thin", "house", "thick")
STREAM_FIXTURES = ("house", "thick")
STEPS_SWEEP = (2, 3, 4)
SCALE_SWEEP = (0.5, 0.8, 1.0, 1.25)
THRESHOLD = 128  # binarization cutoff for the threshold variant
VARIANTS = ("plain", f"thr{THRESHOLD}")
WARMUP = ("house", "plain", 3, 1.0)  # discarded, excluded from statistics
VRAM_SAMPLE_EVERY = 5  # rocm-smi subprocess cost must not distort timings


def gib(value: float) -> float:
    return value / (1024 ** 3)


def percentile_nearest(values: list[float], pct: float) -> float:
    """Nearest-rank percentile, as in worker/worker/engine.py."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def parse_stream_config(raw: str) -> tuple[int, float, str]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) == 2:
        parts.append("plain")
    if len(parts) != 3:
        raise SystemExit(f"--stream-config expects STEPS,SCALE[,VARIANT], got {raw!r}")
    steps, scale, variant = int(parts[0]), float(parts[1]), parts[2]
    if steps < 1:
        raise SystemExit("--stream-config steps must be >= 1")
    if scale <= 0:
        raise SystemExit("--stream-config scale must be > 0")
    if variant not in VARIANTS:
        raise SystemExit(
            f"--stream-config variant must be one of {', '.join(VARIANTS)}"
        )
    return steps, scale, variant


def build_pipeline(arm: str, device: str, dtype: Any) -> tuple[Any, Any]:
    """Load base + VAE + LoRA + LCM scheduler exactly as engine.py builds
    vega-rt, then compose the chosen arm's conditioning pipeline with
    from_pipe so every heavy component is shared, not re-loaded."""
    try:
        vae = AutoencoderKL.from_pretrained(VAE_MODEL, torch_dtype=dtype)
        try:
            # fp16 variants halve the download; not every repo ships one.
            base_pipeline = StableDiffusionXLPipeline.from_pretrained(
                BASE_MODEL, variant="fp16", torch_dtype=dtype, vae=vae,
            )
        except Exception:
            base_pipeline = StableDiffusionXLPipeline.from_pretrained(
                BASE_MODEL, torch_dtype=dtype, vae=vae,
            )
        # Fused on CPU so the device move carries the final tensors, same as
        # engine.py.
        base_pipeline.load_lora_weights(LORA_REPO, weight_name=LORA_FILE)
        base_pipeline.fuse_lora()
        base_pipeline.to(device)
        if device == "cuda":
            for module in (base_pipeline.unet, base_pipeline.vae):
                module.to(memory_format=torch.channels_last)
        base_pipeline.scheduler = LCMScheduler.from_config(
            base_pipeline.scheduler.config
        )
        base_pipeline.set_progress_bar_config(disable=True)
        if arm == "adapter":
            adapter = T2IAdapter.from_pretrained(ADAPTER_MODEL, torch_dtype=dtype)
            arm_pipeline = StableDiffusionXLAdapterPipeline.from_pipe(
                base_pipeline,
                adapter=adapter,
                # from_pipe defaults to float32 and would upcast the shared unet.
                torch_dtype=dtype,
            )
        else:
            controlnet = ControlNetModel.from_pretrained(
                CONTROLNET_MODEL, torch_dtype=dtype
            )
            arm_pipeline = StableDiffusionXLControlNetPipeline.from_pipe(
                base_pipeline,
                controlnet=controlnet,
                # from_pipe defaults to float32 and would upcast the shared unet.
                torch_dtype=dtype,
            )
        arm_pipeline.to(device)
        arm_pipeline.set_progress_bar_config(disable=True)
    except Exception as error:
        raise SystemExit(
            f"could not load {BASE_MODEL} or the {arm} arm: {error}"
        ) from error
    # Loading a second pipeline from the base repo would duplicate the UNet
    # and the text encoders and exhaust the card; fail loudly if from_pipe
    # somehow did not share the weights.
    assert arm_pipeline.unet is base_pipeline.unet, (
        f"from_pipe duplicated the unet; the {arm} arm does not share "
        "weights with the base pipeline and the 8 GB card cannot hold both"
    )
    return arm_pipeline, base_pipeline


def prepare_sketch(path: Path, variant: str) -> Image.Image:
    """Canvas (dark strokes on white) to the conditioning-map convention
    (light strokes on black), with no learned preprocessor. 512x512
    throughout; both arms read the same map."""
    with Image.open(path) as opened:
        gray = opened.convert("L").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    inverted = ImageOps.invert(gray)
    if variant == "plain":
        sketch = inverted
    elif variant == f"thr{THRESHOLD}":
        # WebP ring halos invert to faint gray just above black; binarizing at
        # the midpoint kills the halo while the antialiased stroke cores stay.
        sketch = inverted.point(lambda value: 255 if value >= THRESHOLD else 0)
    else:
        raise ValueError(f"unknown variant: {variant}")
    # Both conditioning models expect three input channels (in_channels: 3).
    return sketch.convert("RGB")


def run_one(
    pipeline: Any,
    arm: str,
    fixture: str,
    variant: str,
    steps: int,
    scale: float,
    out_path: Path | None,
    device: str,
    seed: int = SEED,
) -> tuple[float, float]:
    """One generation. Returns (pipeline_ms, total_ms); total includes WebP
    decode, conditioning-map conversion and PNG encode. Raises on failure so
    the caller can skip to the next combination."""
    if arm == "adapter":
        conditioning_kwargs = {"adapter_conditioning_scale": scale}
    elif arm == "controlnet":
        conditioning_kwargs = {"controlnet_conditioning_scale": scale}
    else:
        raise ValueError(f"unknown arm: {arm}")
    total_start = time.perf_counter()
    map_image = prepare_sketch(FIXTURES_DIR / f"{fixture}.webp", variant)
    generator = torch.Generator(device).manual_seed(seed)
    if device == "cuda":
        torch.cuda.synchronize()  # the previous run must not leak into this one
    start = time.perf_counter()
    result = pipeline(
        prompt=PROMPT,
        image=map_image,
        height=SIZE,
        width=SIZE,
        num_inference_steps=steps,
        guidance_scale=0.0,
        generator=generator,
        **conditioning_kwargs,
    )
    if device == "cuda":
        torch.cuda.synchronize()  # wall clock must include GPU execution
    pipeline_ms = (time.perf_counter() - start) * 1000.0
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result.images[0].save(out_path, "PNG")
    total_ms = (time.perf_counter() - total_start) * 1000.0
    return pipeline_ms, total_ms


def run_sweep(
    pipeline: Any,
    arm: str,
    fixtures: list[str],
    steps_list: list[int],
    scales: list[float],
    device: str,
) -> tuple[list[tuple[str, str, int, float, float, float]],
           dict[tuple[int, float], list[float]]]:
    """Every fixture x variant x steps x scale combination, one failure
    tolerated at a time so a single OOM does not lose the sweep. Runs each
    cell once: the per-cell latencies are comparable, but the aggregate across
    the heterogeneous fixtures is a spread, not a latency distribution, so it
    is reported as min/max, never as percentiles."""
    rows: list[tuple[str, str, int, float, float, float]] = []
    summaries: dict[tuple[int, float], list[float]] = defaultdict(list)
    failed = 0
    for fixture in fixtures:
        for variant in VARIANTS:
            for steps in steps_list:
                for scale in scales:
                    out_path = (
                        OUT_DIR / arm
                        / f"{fixture}-{variant}-s{steps}-c{scale:.2f}.png"
                    )
                    try:
                        pipeline_ms, total_ms = run_one(
                            pipeline, arm, fixture, variant, steps, scale,
                            out_path, device,
                        )
                    except Exception as error:
                        failed += 1
                        print(
                            f"  failed {fixture}-{variant} s{steps} c{scale:.2f}: "
                            f"{error}",
                            flush=True,
                        )
                        continue
                    rows.append((fixture, variant, steps, scale, pipeline_ms, total_ms))
                    summaries[(steps, scale)].append(pipeline_ms)
                    print(
                        f"  {fixture}-{variant} s{steps} c{scale:.2f}: "
                        f"{pipeline_ms:.0f} ms -> {out_path.name}",
                        flush=True,
                    )
    print(f"{arm},sweep,done,runs,{len(rows)},failed,{failed}")
    return rows, summaries


def run_stream(
    pipeline: Any,
    arm: str,
    steps: int,
    scale: float,
    variant: str,
    frames: int,
    device: str,
) -> None:
    """Sustained-stream check: frames alternate house/thick so both drawing
    styles stay in the pipeline, with peak memory reset to isolate the
    session. Every frame's timing is kept; the percentiles over that sequence
    are the script's only honest latency distribution, and frame_* covers the
    complete path a session actually costs."""
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    failed = 0
    pipeline_times: list[float] = []
    frame_times: list[float] = []
    min_free_gib = float("inf")
    vram_samples = 0
    for index in range(frames):
        fixture = STREAM_FIXTURES[index % len(STREAM_FIXTURES)]
        try:
            pipeline_ms, total_ms = run_one(
                pipeline, arm, fixture, variant, steps, scale, None, device,
            )
        except Exception as error:
            failed += 1
            print(f"{arm},stream,frame,{index + 1},failed,{error}", flush=True)
            continue
        pipeline_times.append(pipeline_ms)
        frame_times.append(total_ms)
        if (index + 1) % 10 == 0:
            print(
                f"{arm},stream,frame,{index + 1},pipeline_ms,{pipeline_ms:.1f},"
                f"total_ms,{total_ms:.1f}",
                flush=True,
            )
        if device == "cuda" and (index + 1) % VRAM_SAMPLE_EVERY == 0:
            # Sampled between frames, after this frame's timings are already
            # recorded, so the rocm-smi subprocess never lands inside a
            # measured window.
            free = free_vram_gib()
            if free is not None:
                vram_samples += 1
                min_free_gib = min(min_free_gib, free)
    base = (
        f"{arm},stream,end,frames,{frames},failed,{failed},"
        f"pipeline_p50_ms,{percentile_nearest(pipeline_times, 50.0):.1f},"
        f"pipeline_p95_ms,{percentile_nearest(pipeline_times, 95.0):.1f},"
        f"frame_p50_ms,{percentile_nearest(frame_times, 50.0):.1f},"
        f"frame_p95_ms,{percentile_nearest(frame_times, 95.0):.1f}"
    )
    if device == "cuda":
        min_free = f"{min_free_gib:.2f}" if vram_samples else "n/a"
        print(
            f"{base},peak_allocated_gib,"
            f"{gib(torch.cuda.max_memory_allocated()):.2f},"
            f"peak_reserved_gib,{gib(torch.cuda.max_memory_reserved()):.2f},"
            f"min_free_vram_gib,{min_free},vram_samples,{vram_samples}"
        )
    else:
        print(base)


def run_finalists(
    pipeline: Any,
    arm: str,
    steps: int,
    scale: float,
    variant: str,
    seeds: tuple[int, ...],
    device: str,
) -> None:
    """Re-run the chosen configuration once per fixed seed, saving each image
    named with its seed, so quality can be judged without one lucky seed
    flattering an arm."""
    print(f"{arm},finalist,seeds,{','.join(str(seed) for seed in seeds)}")
    for index, seed in enumerate(seeds):
        fixture = STREAM_FIXTURES[index % len(STREAM_FIXTURES)]
        out_path = (
            OUT_DIR / arm
            / f"finalist-{fixture}-{variant}-s{steps}-c{scale:.2f}-seed{seed}.png"
        )
        try:
            pipeline_ms, _ = run_one(
                pipeline, arm, fixture, variant, steps, scale, out_path, device,
                seed=seed,
            )
        except Exception as error:
            print(f"{arm},finalist,seed,{seed},failed,{error}", flush=True)
            continue
        print(
            f"{arm},finalist,seed,{seed},{fixture}-{variant},"
            f"pipeline_ms,{pipeline_ms:.1f},-> {out_path.name}",
            flush=True,
        )


def vram_lines() -> list[str]:
    """Card usage from outside the process; the only reading that sees the
    whole system rather than this process's allocations."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmemuse"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except FileNotFoundError:
        return ["rocm-smi not found on PATH"]
    except subprocess.TimeoutExpired:
        return ["rocm-smi --showmemuse timed out"]
    return [line for line in result.stdout.splitlines() if "VRAM" in line]


def free_vram_gib() -> float | None:
    """System-wide free VRAM in GiB from outside the process, or None when
    rocm-smi cannot report it. Parses --showmeminfo vram, which lists each
    GPU's total and used bytes, so the reading sees the whole system rather
    than this process's allocations."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    totals = [
        float(value)
        for value in re.findall(r"VRAM Total Memory \(B\): (\d+)", result.stdout)
    ]
    used = [
        float(value)
        for value in re.findall(r"VRAM Total Used Memory \(B\): (\d+)", result.stdout)
    ]
    if not totals or len(used) != len(totals):
        return None
    return gib(sum(totals) - sum(used))


def report_memory(arm: str, label: str, device: str) -> None:
    if device != "cuda":
        return
    print(
        f"{arm},memory,{label},max_allocated_gib,"
        f"{gib(torch.cuda.max_memory_allocated()):.2f},"
        f"max_reserved_gib,{gib(torch.cuda.max_memory_reserved()):.2f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canvas-conditioning prototype against the realtime "
                    "fixtures, on the Segmind-VegaRT base pipeline: the sketch "
                    "T2I-Adapter arm or the scribble ControlNet arm.",
    )
    parser.add_argument(
        "--arm", choices=("adapter", "controlnet"), required=True,
        help="which conditioning mechanism to load and measure: adapter "
             "(TencentARC/t2i-adapter-sketch-sdxl-1.0, features computed "
             "once) or controlnet (xinsir/controlnet-scribble-sdxl-1.0, "
             "forward runs inside the denoising loop); exactly one per "
             "invocation so allocator state cannot bias the comparison",
    )
    parser.add_argument(
        "--device", default=os.environ.get("DEVICE", "rocm"),
        help="rocm or cuda (both map to torch device cuda); cpu for a smoke "
             "run without a GPU (default: %(default)s)",
    )
    parser.add_argument(
        "--steps", type=int, choices=list(STEPS_SWEEP),
        help="restrict the sweep to this step count",
    )
    parser.add_argument(
        "--scale", type=float, choices=list(SCALE_SWEEP),
        help="restrict the sweep to this conditioning scale",
    )
    parser.add_argument(
        "--fixture", choices=list(FIXTURES),
        help="restrict the sweep to this fixture",
    )
    parser.add_argument(
        "--frames", type=int, default=60,
        help="sustained-stream frame count; 0 skips the stream (default: "
             "%(default)s)",
    )
    parser.add_argument(
        "--seeds", type=int, default=3,
        help="how many fixed finalist seeds to re-run the chosen "
             "configuration with, saving each image named with its seed; "
             "0 skips the finalists (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-sweep", action="store_true",
        help="skip the parameter sweep and only run the sustained stream",
    )
    parser.add_argument(
        "--stream-config", default="3,1.0,thr128", metavar="STEPS,SCALE[,VARIANT]",
        help="sustained-stream configuration after the sweep picks the best "
             "look (default: %(default)s)",
    )
    args = parser.parse_args()
    if args.frames < 0:
        parser.error("--frames must be >= 0")
    if not 0 <= args.seeds <= len(SEEDS):
        parser.error(f"--seeds must be between 0 and {len(SEEDS)} (the fixed "
                     f"seed table has {len(SEEDS)} entries)")
    return args


def main() -> int:
    args = parse_args()
    device = "cuda" if args.device in ("cuda", "rocm") else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit(
            "cuda requested but torch.cuda.is_available() is False; is the "
            "ROCm torch build installed and the GPU visible?"
        )
    if args.device == "rocm":
        # RDNA3 consumer cards gate their fused attention kernels behind this
        # flag; read at first SDPA dispatch, so it must precede any inference
        # (engine.py). Never set HSA_OVERRIDE_GFX_VERSION here: this project's
        # stack runs without it and forcing a gfx arch can corrupt kernels.
        os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
    dtype = torch.float16 if device == "cuda" else torch.float32
    print(
        f"# prototype-canvas-conditioning arm={args.arm} device={device} "
        f"dtype={dtype} seed={SEED}"
    )

    load_start = time.perf_counter()
    arm_pipeline, _ = build_pipeline(args.arm, device, dtype)
    load_ms = (time.perf_counter() - load_start) * 1000.0
    # Cold vs cached: a cold run downloads both repositories and takes minutes.
    # With the HuggingFace cache warm (HF_HOME, default ~/.cache/huggingface/
    # hub, holding models--segmind--Segmind-Vega plus the chosen arm's
    # conditioning model), a second run loads in tens of seconds and load_ms
    # drops accordingly.
    print(f"{args.arm},load,cold_ms,{load_ms:.0f}")
    report_memory(args.arm, "after_load", device)
    for line in vram_lines():
        print(f"{args.arm},vram,after_load,{line}")

    warmup_fixture, warmup_variant, warmup_steps, warmup_scale = WARMUP
    try:
        warmup_ms, _ = run_one(
            arm_pipeline, args.arm, warmup_fixture, warmup_variant,
            warmup_steps, warmup_scale, None, device,
        )
    except Exception as error:
        print(f"warmup failed: {error}")
        return 1
    print(
        f"{args.arm},warmup,{warmup_fixture}-{warmup_variant},s{warmup_steps},"
        f"c{warmup_scale:.2f},pipeline_ms,{warmup_ms:.1f}"
    )

    if args.skip_sweep:
        fixtures: list[str] = []
        steps_list: list[int] = []
        scales: list[float] = []
    else:
        fixtures = [args.fixture] if args.fixture else list(FIXTURES)
        steps_list = [args.steps] if args.steps else list(STEPS_SWEEP)
        scales = [args.scale] if args.scale else list(SCALE_SWEEP)

    if fixtures:
        if device == "cuda":
            # Isolate the inference peak from the load peak so the two
            # memory reports separate weights from activations.
            torch.cuda.reset_peak_memory_stats()
        rows, summaries = run_sweep(
            arm_pipeline, args.arm, fixtures, steps_list, scales, device,
        )
        report_memory(args.arm, "after_sweep", device)
        for fixture, variant, steps, scale, pipeline_ms, total_ms in rows:
            print(
                f"{args.arm},run,{fixture},{variant},{steps},{scale:.2f},"
                f"{pipeline_ms:.2f},{total_ms:.2f}"
            )
        for (steps, scale), values in sorted(summaries.items()):
            print(
                f"{args.arm},summary,{steps},{scale:.2f},"
                f"min_ms,{min(values):.2f},max_ms,{max(values):.2f}"
            )

    if args.frames > 0 or args.seeds > 0:
        stream_steps, stream_scale, stream_variant = parse_stream_config(
            args.stream_config
        )
        if args.frames > 0:
            run_stream(
                arm_pipeline, args.arm, stream_steps, stream_scale,
                stream_variant, args.frames, device,
            )
        if args.seeds > 0:
            run_finalists(
                arm_pipeline, args.arm, stream_steps, stream_scale,
                stream_variant, SEEDS[: args.seeds], device,
            )

    for line in vram_lines():
        print(f"{args.arm},vram,end,{line}")
    if device == "cuda":
        peak = gib(torch.cuda.max_memory_allocated())
        print(
            f"{args.arm},verdict,peak_allocated_gib,{peak:.2f},"
            f"fits_8gb_card,{peak <= 8.0}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
