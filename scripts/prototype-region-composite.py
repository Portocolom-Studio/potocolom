"""Measure RGB change-mask compositing after full adapter-conditioned frames.

The realtime UNet still renders the whole canvas, but unchanged pixels can
keep the previous bitmap. This prototype measures the resulting pixel churn
and the serial full-frame cost without changing the shipped worker path.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageOps

os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

region_composite = importlib.import_module("worker.region_composite")
composite_rgb = region_composite.composite_rgb
feather_change_mask = region_composite.feather_change_mask
max_channel_difference = region_composite.max_channel_difference
sketch_change_mask = region_composite.sketch_change_mask

MODELS_DIR = ROOT / "worker" / "models"
BAR_MS = 500.0
SIZE = 512
SEED = 1337
PROMPT = "a cosy stone cottage in a green valley at sunset, oil painting"
SWEEP = ((0, 0), (8, 8), (16, 16))


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(len(ordered) * pct / 100.0))
    return ordered[min(rank, len(ordered)) - 1]


def threshold_map(image: Image.Image) -> Image.Image:
    mapped = ImageOps.invert(image.convert("L"))
    return mapped.point(lambda value: 255 if value >= 128 else 0).convert("RGB")


def canvas_map(kind: str) -> Image.Image:
    canvas = Image.new("RGB", (SIZE, SIZE), "white")
    draw = ImageDraw.Draw(canvas)
    draw.line([(40, 390), (150, 250), (275, 320), (440, 190)], fill=(17, 24, 39), width=6)
    if kind in ("small_stroke", "erasure"):
        draw.line([(315, 400), (350, 370)], fill=(17, 24, 39), width=6)
    elif kind != "base":
        raise ValueError(f"unknown case: {kind}")
    return threshold_map(canvas)


def case_maps(name: str) -> tuple[Image.Image, Image.Image]:
    if name == "small_stroke":
        return canvas_map("base"), canvas_map("small_stroke")
    if name == "erasure":
        return canvas_map("erasure"), canvas_map("base")
    if name == "shape_close":
        open_map = Image.new("RGB", (SIZE, SIZE), "white")
        draw = ImageDraw.Draw(open_map)
        draw.line([(90, 280), (170, 140), (330, 140), (420, 280)], fill=(17, 24, 39), width=6)
        closed_map = open_map.copy()
        ImageDraw.Draw(closed_map).line(
            [(420, 280), (330, 400), (170, 400), (90, 280)],
            fill=(17, 24, 39),
            width=6,
        )
        return (
            threshold_map(open_map),
            threshold_map(closed_map),
        )
    raise ValueError(f"unknown case: {name}")


def build_pipeline(manifest: dict[str, Any], dtype: Any) -> Any:
    import torch
    from diffusers import (
        AutoencoderKL,
        LCMScheduler,
        StableDiffusionXLAdapterPipeline,
        StableDiffusionXLPipeline,
        T2IAdapter,
    )

    kwargs: dict[str, Any] = {"torch_dtype": dtype}
    if manifest.get("vae"):
        kwargs["vae"] = AutoencoderKL.from_pretrained(manifest["vae"], **kwargs)
    try:
        base = StableDiffusionXLPipeline.from_pretrained(
            manifest["source"], variant="fp16", **kwargs
        )
    except Exception:
        base = StableDiffusionXLPipeline.from_pretrained(manifest["source"], **kwargs)
    if manifest.get("lora"):
        repo, _, weight = manifest["lora"].rpartition("/")
        base.load_lora_weights(repo, weight_name=weight)
        base.fuse_lora()
    if manifest.get("scheduler") == "lcm":
        base.scheduler = LCMScheduler.from_config(base.scheduler.config)
    base.to("cuda")
    for module in (base.unet, base.vae):
        module.to(memory_format=torch.channels_last)
    base.set_progress_bar_config(disable=True)
    adapter = T2IAdapter.from_pretrained(manifest["t2i_adapter"], torch_dtype=dtype)
    pipeline = StableDiffusionXLAdapterPipeline.from_pipe(
        base, adapter=adapter, torch_dtype=dtype
    )
    pipeline.to("cuda")
    pipeline.set_progress_bar_config(disable=True)
    return pipeline


def encode_prompt(pipeline: Any) -> dict[str, Any]:
    encoded = pipeline.encode_prompt(
        PROMPT, device="cuda", num_images_per_prompt=1,
        do_classifier_free_guidance=False,
    )
    names = (
        "prompt_embeds",
        "negative_prompt_embeds",
        "pooled_prompt_embeds",
        "negative_pooled_prompt_embeds",
    )
    return {
        name: value
        for name, value in zip(names, encoded, strict=True)
        if value is not None
    }


def render(
    pipeline: Any,
    prompt_kwargs: dict[str, Any],
    sketch: Image.Image,
    steps: int,
    scale: float,
    seed: int,
    output_type: str = "pil",
) -> Image.Image | Any:
    import torch

    with torch.inference_mode():
        result = pipeline(
            **prompt_kwargs,
            image=sketch,
            num_inference_steps=steps,
            guidance_scale=0.0,
            adapter_conditioning_scale=scale,
            generator=torch.Generator(device="cuda").manual_seed(seed),
            height=SIZE,
            width=SIZE,
            output_type=output_type,
        )
    return result.images if output_type == "latent" else result.images[0]


def decode_preview(pipeline: Any, decoder: Any, latent: Any) -> Image.Image:
    import torch

    with torch.inference_mode():
        decoded = decoder.decode(latent, return_dict=False)[0].detach()
        if decoded.dim() == 3:
            decoded = decoded.unsqueeze(0)
    return pipeline.image_processor.postprocess(decoded, output_type="pil")[0]


def changed_fraction(previous: Image.Image, current: Image.Image) -> float:
    changed = max_channel_difference(previous, current).point(
        lambda value: 1 if value else 0
    )
    return sum(changed.getdata()) / (previous.width * previous.height)


def exact_where(alpha: Image.Image, left: Image.Image, right: Image.Image, value: int) -> bool:
    selected = alpha.point(lambda pixel: 255 if pixel == value else 0)
    any_difference = max_channel_difference(left, right)
    return ImageChops.multiply(selected, any_difference).getbbox() is None


def webp_round_trip(image: Image.Image) -> float:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=90, method=0)
    buffer.seek(0)
    with Image.open(buffer) as opened:
        decoded = opened.convert("RGB")
    any_difference = max_channel_difference(image, decoded)
    return sum(any_difference.getdata()) / (image.width * image.height * 255)


def save_image(image: Image.Image, path: Path | None) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="sdxl-turbo,vega-rt")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--out", default="")
    parser.add_argument("--save-dir", default="")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be >= 1")
    save_dir = Path(args.save_dir).resolve() if args.save_dir else None
    if save_dir is not None:
        try:
            save_dir.relative_to((ROOT / ".local").resolve())
        except ValueError:
            parser.error("--save-dir must be under .local")

    import torch
    from diffusers import AutoencoderTiny

    report: dict[str, Any] = {"bar_ms": BAR_MS, "seed": SEED, "models": []}
    for model_id in [item.strip() for item in args.models.split(",") if item.strip()]:
        manifest = json.loads((MODELS_DIR / f"{model_id}.json").read_text())
        properties = manifest["parameters"]["properties"]
        steps = int(properties["steps"]["default"])
        scale = float(properties["structure_strength"]["default"])
        pipeline = None
        decoder = None
        try:
            pipeline = build_pipeline(manifest, torch.float16)
            prompt_kwargs = encode_prompt(pipeline)
            decoder = AutoencoderTiny.from_pretrained(
                manifest["preview_decoder"], torch_dtype=torch.float16
            ).to("cuda")
            decoder.eval()
            render(
                pipeline, prompt_kwargs, canvas_map("base"), steps, scale, SEED, "latent"
            )
            model_report: dict[str, Any] = {
                "model": model_id,
                "steps": steps,
                "adapter_scale": scale,
                "cases": [],
            }
            for case_name in ("small_stroke", "erasure", "shape_close"):
                previous_map, current_map = case_maps(case_name)
                previous_latent = render(
                    pipeline, prompt_kwargs, previous_map, steps, scale, SEED, "latent"
                )
                current_latent = render(
                    pipeline, prompt_kwargs, current_map, steps, scale, SEED, "latent"
                )
                previous = decode_preview(pipeline, decoder, previous_latent)
                current = decode_preview(pipeline, decoder, current_latent)
                frame_times: list[float] = []
                for _ in range(args.frames):
                    started = time.perf_counter()
                    frame_latent = render(
                        pipeline, prompt_kwargs, current_map, steps, scale, SEED, "latent"
                    )
                    decode_preview(pipeline, decoder, frame_latent)
                    torch.cuda.synchronize()
                    frame_times.append((time.perf_counter() - started) * 1000)
                raw_mask = sketch_change_mask(previous_map, current_map)
                sweeps: list[dict[str, Any]] = []
                for dilation_px, feather_px in SWEEP:
                    alpha = feather_change_mask(raw_mask, dilation_px, feather_px)
                    composited = composite_rgb(previous, current, alpha)
                    save_image(
                        composited,
                        save_dir / f"{model_id}-{case_name}-d{dilation_px}-f{feather_px}.png"
                        if save_dir
                        else None,
                    )
                    sweeps.append({
                        "dilation_px": dilation_px,
                        "feather_px": feather_px,
                        "composited_changed_frac": changed_fraction(previous, composited),
                        "outside_mask_prev_exact": exact_where(alpha, composited, previous, 0),
                        "inside_mask_new_exact": exact_where(alpha, composited, current, 255),
                    })
                model_report["cases"].append({
                    "name": case_name,
                    "uncomposited_changed_frac": changed_fraction(previous, current),
                    "p95_ms": percentile(frame_times, 95),
                    "webp_round_trip_mean_max_channel_over_255": webp_round_trip(current),
                    "sweeps": sweeps,
                })
            report["models"].append(model_report)
        finally:
            del pipeline, decoder
            torch.cuda.empty_cache()
    print(json.dumps(report, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
