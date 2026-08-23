"""Measure serial and staggered Stream Batch inference on the GPU.

The worker does not use this prototype. It records the cost and pixel error of
the proposed queue math before that path is connected to realtime rendering.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import pathlib
import statistics
import sys
import time
from typing import Any

os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

stream_batch = importlib.import_module("worker.stream_batch")
assemble_batch = stream_batch.assemble_batch
commit_latent_buffer = stream_batch.commit_latent_buffer
predicted_x0 = stream_batch.predicted_x0
shift_condition_buffer = stream_batch.shift_condition_buffer
stack_adapter_states = stream_batch.stack_adapter_states

MODELS_DIR = pathlib.Path(__file__).resolve().parents[1] / "worker" / "models"
BAR_MS = 500.0


def sketch_map(size: int, draw_offset: int) -> Any:
    from PIL import Image, ImageDraw, ImageOps

    canvas = Image.new("RGB", (size, size), "white")
    pen = ImageDraw.Draw(canvas)
    pen.line(
        [(10, 210 + draw_offset), (150, 140), (300, 180), (size - 12, 150)],
        fill=(17, 24, 39),
        width=5,
    )
    pen.ellipse([(150, 300), (360, 380)], outline=(17, 24, 39), width=5)
    grey = ImageOps.invert(canvas.convert("L"))
    return grey.point(lambda value: 255 if value >= 128 else 0).convert("RGB")


def build(manifest: dict, dtype: Any, device: str) -> Any:
    from diffusers import (
        AutoencoderKL,
        LCMScheduler,
        StableDiffusionXLAdapterPipeline,
        StableDiffusionXLPipeline,
        T2IAdapter,
    )

    kwargs: dict[str, Any] = {"torch_dtype": dtype}
    if manifest.get("vae"):
        kwargs["vae"] = AutoencoderKL.from_pretrained(
            manifest["vae"], torch_dtype=dtype
        )
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
    base.to(device)
    for module in (base.unet, base.vae):
        module.to(memory_format=__import__("torch").channels_last)
    if manifest.get("scheduler") == "lcm":
        base.scheduler = LCMScheduler.from_config(base.scheduler.config)
    base.set_progress_bar_config(disable=True)
    adapter = T2IAdapter.from_pretrained(manifest["t2i_adapter"], torch_dtype=dtype)
    pipeline = StableDiffusionXLAdapterPipeline.from_pipe(
        base, adapter=adapter, torch_dtype=dtype
    )
    pipeline.to(device)
    pipeline.set_progress_bar_config(disable=True)
    return pipeline


def nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stats(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": nearest_rank(values, 95.0),
        "max_ms": max(values),
    }


def prompt_embeds(pipeline: Any, prompt: str, device: str) -> dict[str, Any]:
    positive, _, pooled, _ = pipeline.encode_prompt(
        prompt=prompt,
        device=device,
        num_images_per_prompt=1,
        do_classifier_free_guidance=False,
    )
    return {"prompt_embeds": positive, "pooled_prompt_embeds": pooled}


def adapter_state(pipeline: Any, image: Any, size: int, scale: float) -> list[Any]:
    from diffusers.pipelines.t2i_adapter.pipeline_stable_diffusion_xl_adapter import (
        _preprocess_adapter_image,
    )

    tensor = _preprocess_adapter_image(image, size, size).to(
        device=pipeline._execution_device, dtype=pipeline.adapter.dtype
    )
    return [state * scale for state in pipeline.adapter(tensor)]


def time_decode(pipeline: Any, decoder: Any, latent: Any) -> Any:
    import torch

    with torch.inference_mode():
        decoded = decoder.decode(latent, return_dict=False)[0]
    decoded = decoded.detach()
    return pipeline.image_processor.postprocess(decoded, output_type="pil")[0]


def lcm_scalings(scheduler: Any, timestep: Any) -> tuple[Any, Any]:
    method = getattr(scheduler, "get_scalings_for_boundary_condition_discrete", None)
    if method is not None:
        return method(timestep)
    sigma_data = 0.5
    scaled = timestep * scheduler.config.timestep_scaling
    denominator = scaled**2 + sigma_data**2
    return sigma_data**2 / denominator, scaled / denominator**0.5


def stream_frame(
    pipeline: Any,
    decoder: Any,
    embeds: dict[str, Any],
    current_map: Any,
    adapter_buffer: list[list[Any]],
    latent_buffer: list[Any],
    timesteps: Any,
    noise_sequence: list[Any],
    size: int,
    scale: float,
    is_lcm: bool,
) -> tuple[Any, list[list[Any]], list[Any]]:
    import torch

    current_adapter = adapter_state(pipeline, current_map, size, scale)
    if not adapter_buffer:
        adapter_buffer = [
            [torch.zeros_like(state) for state in current_adapter]
            for _ in latent_buffer
        ]
    adapter_batch = [current_adapter, *adapter_buffer]
    latent = noise_sequence[0]
    noise_sequence.pop(0)
    latent_batch = assemble_batch(latent, latent_buffer)
    model_inputs = [
        pipeline.scheduler.scale_model_input(item, timestep)
        for item, timestep in zip(latent_batch, timesteps, strict=True)
    ]
    model_input = torch.cat(model_inputs, dim=0)
    batch_size = len(latent_batch)
    prompt = embeds["prompt_embeds"].repeat(batch_size, 1, 1)
    pooled = embeds["pooled_prompt_embeds"].repeat(batch_size, 1)
    time_ids = pipeline._get_add_time_ids(
        (size, size),
        (0, 0),
        (size, size),
        dtype=prompt.dtype,
        text_encoder_projection_dim=pipeline.text_encoder_2.config.projection_dim,
    ).to(model_input.device).repeat(batch_size, 1)
    residuals = stack_adapter_states(
        adapter_batch, lambda states: torch.cat(states, dim=0)
    )
    noise_pred = pipeline.unet(
        model_input,
        torch.stack(list(timesteps)),
        encoder_hidden_states=prompt,
        added_cond_kwargs={"text_embeds": pooled, "time_ids": time_ids},
        down_intrablock_additional_residuals=residuals,
        return_dict=False,
    )[0]
    if is_lcm:
        x0_batch = []
        alphas = []
        betas = []
        for index, timestep in enumerate(timesteps):
            alpha_bar = pipeline.scheduler.alphas_cumprod[timestep.long()]
            alpha = alpha_bar**0.5
            beta = (1 - alpha_bar) ** 0.5
            c_skip, c_out = lcm_scalings(pipeline.scheduler, timestep)
            x0_batch.append(
                predicted_x0(
                    latent_batch[index],
                    noise_pred[index : index + 1],
                    alpha,
                    beta,
                    c_skip,
                    c_out,
                )
            )
            alphas.append(alpha)
            betas.append(beta)
        noises = [torch.randn_like(item) for item in latent_batch[:-1]]
        finished, next_latents = commit_latent_buffer(
            x0_batch,
            alphas[1:],
            betas[1:],
            noises,
            do_add_noise=True,
        )
    else:
        pipeline.scheduler.set_timesteps(len(timesteps), device=timesteps.device)
        stepped = [
            pipeline.scheduler.step(
                noise_pred[index : index + 1],
                timestep,
                latent_batch[index],
                return_dict=False,
            )[0]
            for index, timestep in enumerate(timesteps)
        ]
        finished, next_latents = stepped[-1], stepped[:-1]
    next_adapters = shift_condition_buffer(current_adapter, adapter_buffer)
    return time_decode(pipeline, decoder, finished), next_adapters, next_latents


def run(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np
    import torch

    manifest = json.loads((MODELS_DIR / f"{args.model}.json").read_text())
    properties = manifest["parameters"]["properties"]
    steps = args.steps or int(properties["steps"]["default"])
    scale = float(properties["structure_strength"]["default"])
    size = args.size
    device = "cuda"
    dtype = torch.float16
    pipeline = build(manifest, dtype, device)
    decoder = (
        __import__("diffusers")
        .AutoencoderTiny.from_pretrained(
            "madebyollin/taesdxl", torch_dtype=dtype
        ).to(device)
        if args.tiny_vae
        else pipeline.vae
    )
    prompt = "mountains in the alps, a lake, photorealistic"
    embeds = prompt_embeds(pipeline, prompt, device)
    maps = [sketch_map(size, index * 7) for index in range(args.frames + steps - 1)]
    generators = [
        torch.Generator(device=device).manual_seed(args.seed + index)
        for index in range(args.frames + steps)
    ]
    stream_generators = [
        torch.Generator(device=device).manual_seed(args.seed + index)
        for index in range(args.frames + steps)
    ]
    serial_images: list[Any] = []
    serial_times: list[float] = []
    for index, image in enumerate(maps):
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            latent = pipeline(
                **embeds,
                image=image,
                num_inference_steps=steps,
                guidance_scale=0.0,
                adapter_conditioning_scale=scale,
                generator=generators[index],
                height=size,
                width=size,
                output_type="latent",
            ).images
            serial_images.append(time_decode(pipeline, decoder, latent))
        torch.cuda.synchronize()
        elapsed = (time.perf_counter() - started) * 1000
        if index < args.frames:
            serial_times.append(elapsed)
    pipeline.scheduler.set_timesteps(steps, device=device)
    timesteps = pipeline.scheduler.timesteps
    shape = (
        1,
        pipeline.unet.config.in_channels,
        size // pipeline.vae_scale_factor,
        size // pipeline.vae_scale_factor,
    )
    noise = [
        torch.randn(shape, generator=generator, device=device, dtype=dtype)
        * pipeline.scheduler.init_noise_sigma
        for generator in stream_generators
    ]
    latent_buffer: list[Any] = [
        torch.zeros_like(noise[0]) for _ in range(max(0, steps - 1))
    ]
    adapter_buffer: list[list[Any]] = []
    stream_times: list[float] = []
    paired: list[float] = []
    fill_started = time.perf_counter()
    for index in range(args.frames + steps - 1):
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            output, adapter_buffer, latent_buffer = stream_frame(
                pipeline,
                decoder,
                embeds,
                maps[index],
                adapter_buffer,
                latent_buffer,
                timesteps,
                noise[index:],
                size,
                scale,
                manifest.get("scheduler") == "lcm",
            )
        torch.cuda.synchronize()
        elapsed = (time.perf_counter() - started) * 1000
        discard_count = max(0, steps - 1)
        if index < discard_count:
            if index == discard_count - 1:
                fill_ms = (time.perf_counter() - fill_started) * 1000
            continue
        stream_times.append(elapsed)
        serial_index = index - (steps - 1)
        if 0 <= serial_index < len(serial_images):
            actual = np.asarray(output).astype(np.float32)
            expected = np.asarray(serial_images[serial_index]).astype(np.float32)
            paired.append(float(np.abs(actual - expected).mean() / 255.0))
    fill_ms = 0.0 if discard_count == 0 else fill_ms
    serial_stats = stats(serial_times)
    stream_stats = stats(stream_times)
    result = {
        "model": manifest["id"],
        "steps": steps,
        "serial": serial_stats,
        "stream": stream_stats,
        "ratio_serial_p95_stream_p95": serial_stats["p95_ms"] / stream_stats["p95_ms"],
        "lag_frames": steps - 1,
        "fill_ms": fill_ms,
        "quality_mean_channel_abs_diff": statistics.mean(paired) if paired else 0.0,
        "inside_500": stream_stats["p95_ms"] <= BAR_MS,
        "notes": "1-step should be near 1x; do not advertise N=4 turbo",
    }
    print(json.dumps(result, indent=2))
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="vega-rt")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--out")
    parser.add_argument("--tiny-vae", action=argparse.BooleanOptionalAction, default=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
