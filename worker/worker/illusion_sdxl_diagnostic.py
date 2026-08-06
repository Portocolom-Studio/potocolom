"""Short SDXL integration diagnostic for illusion SDS.

This is not a creative benchmark. It checks direct generation, VAE
reconstruction, conditioning response, and the Euler-vs-training-noise
difference before SDXL is allowed back into a long campaign.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import torch

from worker.illusion_experiment import write_manifest_atomic
from worker.illusions import (
    DiffusionAdapter,
    add_training_noise,
    compute_sds_gradient,
    save_image,
    sds_timestep_weight,
)

DEFAULT_PROMPTS = (
    "a centered intricate HB pencil illustration of a pine tree, full object, "
    "strong silhouette, isolated on plain warm paper",
    "a centered intricate HB pencil illustration of an ornate chandelier, full object, "
    "strong silhouette, isolated on plain warm paper",
)


def _rms(tensor: torch.Tensor) -> float:
    return float(tensor.detach().float().square().mean().sqrt().item())


def _finite(tensor: torch.Tensor) -> bool:
    return bool(torch.isfinite(tensor).all().item())


def run_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    prompts = list(args.prompt or DEFAULT_PROMPTS)
    if len(prompts) != 2:
        raise ValueError("diagnostic requires exactly two prompts")

    torch.manual_seed(args.seed)
    generator = torch.Generator(device=args.device).manual_seed(args.seed)
    adapter = DiffusionAdapter(
        args.model,
        args.device,
        dream_model_id=None,
        vae_id=args.vae,
        model_variant=args.model_variant,
        sds_objective="weighted_sds",
    )

    report: dict[str, Any] = {
        "schema": "illusion-sdxl-diagnostic-v1",
        "model": args.model,
        "vae_id": args.vae,
        "model_variant": args.model_variant,
        "seed": args.seed,
        "prompts": prompts,
        "direct_t2i": [],
        "vae": {},
        "timesteps": [],
    }

    for index, prompt in enumerate(prompts, start=1):
        with torch.no_grad():
            image = adapter.pipe(
                prompt=prompt,
                height=512,
                width=512,
                num_inference_steps=args.inference_steps,
                guidance_scale=7.5,
                generator=generator,
                output_type="pt",
            ).images.float()
        save_image(image, out / f"direct_t2i_{index}.png")
        report["direct_t2i"].append(
            {"prompt_index": index, "finite": _finite(image), "rms": _rms(image)}
        )

    source = torch.linspace(
        0,
        1,
        512,
        device=args.device,
        dtype=torch.float32,
    )
    source = source[None, None, :, None].expand(1, 3, 512, 512)
    with torch.no_grad():
        latent = adapter.encode_latent(source, use_mean=True)
        decoded = adapter.pipe.vae.decode(latent / adapter.pipe.vae.config.scaling_factor).sample
        decoded = ((decoded.float() + 1) / 2).clamp(0, 1)
    report["vae"] = {
        "finite": _finite(decoded),
        "reconstruction_mse": float((decoded - source).square().mean().item()),
    }
    save_image(decoded, out / "vae_reconstruction.png")

    alphas = adapter.scheduler.alphas_cumprod.to(args.device)
    for timestep in args.timestep or [100, 500, 900]:
        t = torch.tensor([timestep], device=args.device, dtype=torch.long)
        noise = torch.randn(
            latent.shape,
            generator=generator,
            device=args.device,
            dtype=latent.dtype,
        )
        corrected = add_training_noise(latent, noise, t, alphas)
        old_error = None
        try:
            old_euler = adapter.scheduler.add_noise(latent, noise, t)
            old_rms = _rms(old_euler)
        except Exception as exc:  # diagnostic evidence, not a runner failure
            old_euler = None
            old_rms = None
            old_error = f"{type(exc).__name__}: {exc}"

        gradients = []
        conditioning_rms = []
        for prompt in prompts:
            with torch.no_grad():
                uncond, cond, _ = adapter._unet_cfg(corrected, t, [prompt])
            conditioning_rms.append(_rms(cond - uncond))
            gradient = compute_sds_gradient(
                objective="weighted_sds",
                noise=noise,
                uncond=uncond,
                cond=cond,
                guidance_scale=7.5,
                weight_t=sds_timestep_weight(alphas, timestep),
            )
            gradients.append(gradient.detach().float().flatten())
        denom = max(float(gradients[0].norm() * gradients[1].norm()), 1e-12)
        cosine = float(torch.dot(gradients[0], gradients[1]).item() / denom)
        report["timesteps"].append(
            {
                "timestep": timestep,
                "alpha": float(alphas[timestep].item()),
                "corrected_noised_rms": _rms(corrected),
                "old_euler_noised_rms": old_rms,
                "old_euler_error": old_error,
                "conditioning_delta_rms": conditioning_rms,
                "prompt_gradient_cosine": cosine,
                "finite": _finite(corrected)
                and all(math.isfinite(value) for value in conditioning_rms),
            }
        )

    report["pass"] = bool(
        all(row["finite"] for row in report["direct_t2i"])
        and report["vae"]["finite"]
        and report["vae"]["reconstruction_mse"] < 0.05
        and all(
            row["finite"] and min(row["conditioning_delta_rms"]) > 0 for row in report["timesteps"]
        )
    )
    write_manifest_atomic(out / "diagnostic.json", report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--vae")
    parser.add_argument("--model-variant")
    parser.add_argument("--prompt", action="append")
    parser.add_argument("--timestep", action="append", type=int)
    parser.add_argument("--inference-steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    report = run_diagnostic(build_arg_parser().parse_args(argv))
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
