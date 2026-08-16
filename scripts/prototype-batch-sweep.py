"""Does one GPU serve more than one person drawing? Measure, do not guess.

Two realtime sessions on one worker serialize on the GPU lock today, so each
sees twice a single frame: at the measured 278 ms for sdxl-turbo that is 556 ms
per cycle, past the 500 ms bar. Cross-session batching is the recorded way out
(decisions.md, "Realtime concurrency comes from batching one GPU"), and whether
it works here is arithmetic this script supplies: the cost of denoising N
sessions' frames as one batch, against N times the cost of one.

It measures the batched pipeline call directly, without the worker or the API,
because the question is about the GPU and not about the relay. Two regions are
timed for every batch size, because issue #288 records that the project does
not yet agree on which one it means: the pipeline call alone, and the call plus
the sketch-map preparation and the WebP encode that a real frame also pays.

    ./worker/.venv/bin/python scripts/prototype-batch-sweep.py
    ./worker/.venv/bin/python scripts/prototype-batch-sweep.py --model vega-rt --steps 4

Reads a model's manifest for its source, adapter and defaults, so it measures
what ships rather than a second description of it.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import statistics
import time
from typing import Any

MODELS_DIR = pathlib.Path(__file__).resolve().parents[1] / "worker" / "models"
BAR_MS = 500.0  # the realtime bar, docs/decisions.md
SIZE = 512  # overridden by --size


def gib(value: float) -> float:
    return value / 1024**3


def sketch_map(draw_offset: int) -> Any:
    """A canvas like the panel sends: dark strokes on white, inverted and
    thresholded to the sketch map the adapter expects (engine.py)."""
    from PIL import Image, ImageDraw, ImageOps

    canvas = Image.new("RGB", (SIZE, SIZE), "white")
    pen = ImageDraw.Draw(canvas)
    pen.line([(10, 210 + draw_offset), (150, 140), (300, 180), (500, 150)],
             fill=(17, 24, 39), width=5)
    pen.ellipse([(150, 300), (360, 380)], outline=(17, 24, 39), width=5)
    grey = ImageOps.invert(canvas.convert("L"))
    return grey.point(lambda v: 255 if v >= 128 else 0).convert("RGB")


def encode_webp(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=90, method=0)
    return buffer.getvalue()


def build(manifest: dict, dtype: Any, device: str) -> Any:
    from diffusers import (
        AutoencoderKL,
        LCMScheduler,
        StableDiffusionXLAdapterPipeline,
        StableDiffusionXLPipeline,
        T2IAdapter,
    )
    import torch

    kwargs: dict[str, Any] = {"torch_dtype": dtype}
    if manifest.get("vae"):
        kwargs["vae"] = AutoencoderKL.from_pretrained(manifest["vae"], torch_dtype=dtype)
    try:
        base = StableDiffusionXLPipeline.from_pretrained(
            manifest["source"], variant="fp16", **kwargs)
    except Exception:
        base = StableDiffusionXLPipeline.from_pretrained(manifest["source"], **kwargs)
    if manifest.get("lora"):
        # "repo/path/weights.safetensors", split the way engine.py splits it.
        repo, _, weight = manifest["lora"].rpartition("/")
        base.load_lora_weights(repo, weight_name=weight)
        base.fuse_lora()
    base.to(device)
    for module in (base.unet, base.vae):
        module.to(memory_format=torch.channels_last)
    if manifest.get("scheduler") == "lcm":
        base.scheduler = LCMScheduler.from_config(base.scheduler.config)
    base.set_progress_bar_config(disable=True)
    adapter = T2IAdapter.from_pretrained(manifest["t2i_adapter"], torch_dtype=dtype)
    pipeline = StableDiffusionXLAdapterPipeline.from_pipe(
        base, adapter=adapter, torch_dtype=dtype)
    pipeline.to(device)
    pipeline.set_progress_bar_config(disable=True)
    return pipeline


def main() -> None:
    global SIZE

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sdxl-turbo")
    ap.add_argument("--steps", type=int, default=None, help="default: the manifest's")
    ap.add_argument("--max-batch", type=int, default=4)
    ap.add_argument("--frames", type=int, default=12, help="timed frames per batch size")
    ap.add_argument("--size", type=int, default=512,
                    help="square edge in pixels; the bar is defined at 512")
    args = ap.parse_args()

    import torch

    SIZE = args.size
    manifest = json.loads((MODELS_DIR / f"{args.model}.json").read_text())
    properties = manifest["parameters"]["properties"]
    steps = args.steps or properties["steps"]["default"]
    scale = float(properties.get("structure_strength", {}).get("default", 1.0))
    guidance = float(properties.get("guidance", {}).get("default", 0.0))
    device = "cuda"
    dtype = torch.float16

    print(f"{manifest['id']} at {steps} step(s), structure {scale}, {SIZE} px, "
          f"bar {BAR_MS:.0f} ms")
    pipeline = build(manifest, dtype, device)

    # Does the adapter scale accept one value per sample? If it does not, every
    # session in a batch must share a Structure slider value, which narrows the
    # compatibility class the batching design depends on (issue #294).
    per_sample_scale = None

    for batch in range(1, args.max_batch + 1):
        prompts = [f"mountains in the alps, a lake, photorealistic, take {i}"
                   for i in range(batch)]
        maps = [sketch_map(i * 7) for i in range(batch)]
        generators = [torch.Generator(device=device).manual_seed(1000 + i)
                      for i in range(batch)]
        torch.cuda.reset_peak_memory_stats()

        def one_frame(timed_encode: bool) -> tuple[float, float]:
            prepared_start = time.perf_counter()
            images = [sketch_map(i * 7) for i in range(batch)] if timed_encode else maps
            call_start = time.perf_counter()
            out = pipeline(
                prompt=prompts,
                image=images,
                num_inference_steps=steps,
                guidance_scale=guidance,
                adapter_conditioning_scale=scale,
                generator=generators,
                height=SIZE, width=SIZE,
            ).images
            torch.cuda.synchronize()
            call_ms = (time.perf_counter() - call_start) * 1000
            if timed_encode:
                for image in out:
                    encode_webp(image)
            whole_ms = (time.perf_counter() - prepared_start) * 1000
            return call_ms, whole_ms

        one_frame(False)  # warm the graph; never timed
        calls, wholes = [], []
        for _ in range(args.frames):
            call_ms, whole_ms = one_frame(True)
            calls.append(call_ms)
            wholes.append(whole_ms)
        calls.sort()
        wholes.sort()
        p95 = calls[max(0, round(0.95 * len(calls)) - 1)]
        whole95 = wholes[max(0, round(0.95 * len(wholes)) - 1)]
        peak = gib(torch.cuda.max_memory_allocated())
        per_frame = p95 / batch
        verdict = "inside" if whole95 <= BAR_MS else "past"
        print(f"batch {batch}: call p50 {statistics.median(calls):6.1f}  p95 {p95:6.1f}  "
              f"| whole-frame p95 {whole95:6.1f} ({verdict} the bar)  "
              f"| per session {per_frame:6.1f}  | peak {peak:5.2f} GiB")

        if per_sample_scale is None and batch > 1:
            try:
                pipeline(prompt=prompts, image=maps, num_inference_steps=steps,
                         guidance_scale=guidance,
                         adapter_conditioning_scale=[scale] * batch,
                         generator=generators, height=SIZE, width=SIZE)
                per_sample_scale = True
            except Exception as error:
                per_sample_scale = f"no: {type(error).__name__}: {str(error)[:80]}"

    print(f"per-sample adapter scale accepted: {per_sample_scale}")


if __name__ == "__main__":
    main()
