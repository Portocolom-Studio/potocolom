"""Does one GPU serve more than one person drawing? Measure, do not guess.

Two realtime sessions on one worker serialize on the GPU lock today, so each
sees twice a single frame: at the measured 278 ms for sdxl-turbo that is 556 ms
per cycle, past the 500 ms bar. Two things buy the second session, and the
sweep measures both, because the recorded order of work depends on which
dominates (decisions.md, "Realtime concurrency comes from one GPU serving
several sessions"). --tiny-vae is the larger one: the full VAE decode is about
half the frame, and replacing it puts two serialized sessions inside the bar
with no scheduler change at all, three once the prompt embeddings are cached
per session too. Batching is the smaller one, worth about seventeen percent,
and this script supplies its arithmetic: the cost of denoising N sessions'
frames as one batch, against N times the cost of one.

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

import os

# Before torch is imported anywhere, because SDPA reads this at its first
# dispatch and never again. RDNA 3 gates its fused attention kernels behind it,
# and without it torch falls back to math attention: docs/gpu-performance.md
# warns that any standalone ROCm script must set it, and this script did not,
# which made its first sweep measure a slower pipeline than the worker runs.
# DiffusersEngine sets the same variable for DEVICE=rocm; a script that builds
# a pipeline directly bypasses that and has to do it itself.
os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

import argparse
import io
import json
import math
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
    ap.add_argument("--frames", type=int, default=120,
                    help="timed frames per batch size; 12 gives a p85, not a p95")
    ap.add_argument("--size", type=int, default=512,
                    help="square edge in pixels; the bar is defined at 512")
    ap.add_argument("--tiny-vae", action="store_true",
                    help="decode the preview with TAESDXL instead of the full VAE, "
                         "which is what the recorded capacity curve was measured with")
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
          f"{'TAESDXL' if args.tiny_vae else 'full'} decode, bar {BAR_MS:.0f} ms")
    pipeline = build(manifest, dtype, device)

    tiny = None
    if args.tiny_vae:
        from diffusers import AutoencoderTiny

        # Half the frame is the full VAE decode at these step counts, so the
        # preview decoder is what moves the concurrency curve (decisions.md,
        # "Realtime concurrency comes from one GPU"). Latents go in
        # unscaled: AutoencoderTiny expects the pipeline's own latent scale.
        tiny = AutoencoderTiny.from_pretrained(
            "madebyollin/taesdxl", torch_dtype=dtype).to(device)

    # Does the adapter scale accept one value per sample? If it does not, every
    # session in a batch must share a Structure slider value, which narrows the
    # compatibility class the batching design depends on (issue #294).
    per_sample_scale = None

    for batch in range(1, args.max_batch + 1):
        prompts = [f"mountains in the alps, a lake, photorealistic, take {i}"
                   for i in range(batch)]
        maps = [sketch_map(i * 7) for i in range(batch)]
        torch.cuda.reset_peak_memory_stats()

        def one_frame(timed_encode: bool) -> tuple[float, float]:
            prepared_start = time.perf_counter()
            images = [sketch_map(i * 7) for i in range(batch)] if timed_encode else maps
            # A fresh generator per frame, as the worker builds one per frame
            # from the session seed: a reused generator advances its state.
            generators = [torch.Generator(device=device).manual_seed(1000 + i)
                          for i in range(batch)]
            call_start = time.perf_counter()
            out = pipeline(
                prompt=prompts,
                image=images,
                num_inference_steps=steps,
                guidance_scale=guidance,
                adapter_conditioning_scale=scale,
                generator=generators,
                height=SIZE, width=SIZE,
                output_type="latent" if tiny else "pil",
            ).images
            if tiny is not None:
                with torch.inference_mode():
                    decoded = tiny.decode(out, return_dict=False)[0]
                out = pipeline.image_processor.postprocess(decoded, output_type="pil")
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
        # Nearest rank by ceiling, not round: with n=12 the rounded form picks
        # the second highest sample, which is about the 85th percentile, and a
        # run that small has a better than even chance of never sampling the
        # true p95 at all. Hence the frame default.
        def nearest_rank(values: list[float], pct: float) -> float:
            return values[min(len(values) - 1, math.ceil(pct * len(values)) - 1)]

        p95 = nearest_rank(calls, 0.95)
        whole95 = nearest_rank(wholes, 0.95)
        whole99 = nearest_rank(wholes, 0.99)
        peak = gib(torch.cuda.max_memory_allocated())
        reserved = gib(torch.cuda.max_memory_reserved())
        # Resource cost per image, which is not what a session waits: every
        # member of a batch waits the whole batch.
        amortized = p95 / batch
        verdict = "inside" if whole95 <= BAR_MS else "past"
        print(f"batch {batch}: call p50 {statistics.median(calls):6.1f}  p95 {p95:6.1f}  "
              f"| whole-frame p50 {statistics.median(wholes):6.1f}  p95 {whole95:6.1f}  "
              f"p99 {whole99:6.1f} ({verdict} the bar)  "
              f"| amortized/image {amortized:6.1f}  "
              f"| peak alloc {peak:4.2f} reserved {reserved:4.2f} GiB")

        if per_sample_scale is None and batch > 1:
            # A different Structure value per session, which decides whether
            # Structure is a batching compatibility dimension. A Python list is
            # the wrong API and raises; diffusers multiplies the batched adapter
            # state by this value, so a [B,1,1,1] device tensor scales each
            # sample. Measured: each batch member matches a solo render at its
            # own scale to well under one level of mean absolute difference,
            # while the two members differ from each other by about 48.
            per_sample_scale = {}
            for form, value in (
                ("python list", [scale] * batch),
                ("tensor [B,1,1,1]", torch.tensor(
                    [[[[scale]]]] * batch, device=device, dtype=dtype)),
            ):
                gens = [torch.Generator(device=device).manual_seed(1000 + i)
                        for i in range(batch)]
                try:
                    pipeline(prompt=prompts, image=maps, num_inference_steps=steps,
                             guidance_scale=guidance,
                             adapter_conditioning_scale=value,
                             generator=gens, height=SIZE, width=SIZE)
                    per_sample_scale[form] = "accepted"
                except Exception as error:
                    per_sample_scale[form] = f"{type(error).__name__}"

    print(f"per-sample adapter scale: {per_sample_scale}")


if __name__ == "__main__":
    main()
