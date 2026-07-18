"""Diffusion Illusions optimizer (issue #115).

Implements Burgert et al., "Diffusion Illusions: Hiding Images in Plain
Sight" (https://diffusionillusions.com): prime images parameterized by
Fourier Feature Networks are optimized so that fixed, differentiable,
physically realizable arrangements of them (flip, rotation overlay,
hidden overlay) match per-derived-image text prompts or a target image.

Two-phase optimization against a frozen text-to-image diffusion model:
Score Distillation Loss first, then Dream Target Loss (SDEdit targets at
a decreasing strength schedule, regressed with SSIM + MSE). Gradients
never flow through the diffusion network.

CLI (run inside the worker venv, needs the inference extra):

    python -m worker.illusions --type flip --prompt "a dog" \
        --prompt "a sloth" --out out/flip

torch is imported at module level on purpose: this module is only useful
with the inference extra installed. Nothing else in the package imports
it, so the worker still runs without torch.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import torch
import torch.nn.functional as tf
from torch import Tensor, nn

ProgressFn = Callable[[float], None]

RESOLUTION = 512


# ---------------------------------------------------------------- primes


class FourierFeatureNetwork(nn.Module):
    """Implicit image: pixel coordinates -> RGB through fixed Fourier
    features and a small MLP. High-frequency-capable but smooth enough to
    survive printing, per the paper's Sec. 4.3."""

    frequencies: Tensor

    def __init__(self, features: int = 256, hidden: int = 256, scale: float = 10.0) -> None:
        super().__init__()
        self.register_buffer("frequencies", torch.randn(2, features) * scale)
        self.mlp = nn.Sequential(
            nn.Linear(2 * features, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Linear(hidden, 3),
        )

    def forward(self, coords: Tensor) -> Tensor:
        projected = 2 * math.pi * coords @ self.frequencies
        encoded = torch.cat([torch.sin(projected), torch.cos(projected)], dim=-1)
        return torch.sigmoid(self.mlp(encoded))

    def image(self, resolution: int = RESOLUTION) -> Tensor:
        """Render to a (1, 3, H, W) image in [0, 1]."""
        device = self.frequencies.device
        axis = torch.linspace(-1.0, 1.0, resolution, device=device)
        grid = torch.stack(torch.meshgrid(axis, axis, indexing="ij"), dim=-1)
        rgb = self(grid.reshape(-1, 2)).reshape(resolution, resolution, 3)
        return rgb.permute(2, 0, 1).unsqueeze(0)


# ---------------------------------------------------------- arrangements


def rot90(image: Tensor, quarter_turns: int) -> Tensor:
    """Differentiable rotation by multiples of 90 degrees, (1,3,H,W)."""
    return torch.rot90(image, k=quarter_turns % 4, dims=(2, 3))


def overlay(primes: list[Tensor], brightness: float) -> Tensor:
    """Light through stacked transparencies: multiply, brighten, and
    normalize with tanh so dynamic range is not lost (paper Appendix A.1)."""
    product = primes[0]
    for prime in primes[1:]:
        product = product * prime
    return torch.tanh(brightness * product)


@dataclass(frozen=True)
class IllusionSpec:
    """One illusion type: n primes -> m derived images (paper Table 1)."""

    n_primes: int
    weights: list[float]
    arrange: Callable[[list[Tensor]], list[Tensor]]


def _flip(primes: list[Tensor]) -> list[Tensor]:
    return [primes[0], rot90(primes[0], 2)]


def _rotation_overlay(primes: list[Tensor]) -> list[Tensor]:
    base, rotator = primes
    return [overlay([base, rot90(rotator, j)], brightness=2.0) for j in range(4)]


def _hidden_overlay(primes: list[Tensor]) -> list[Tensor]:
    return [*primes, overlay(primes, brightness=3.0)]


ILLUSIONS: dict[str, IllusionSpec] = {
    "flip": IllusionSpec(1, [1.0, 1.0], _flip),
    "rotate": IllusionSpec(2, [1.0] * 4, _rotation_overlay),
    # the hidden image is what the illusion is for; weight it 3 (paper 3.3.2)
    "hidden": IllusionSpec(4, [1.0, 1.0, 1.0, 1.0, 3.0], _hidden_overlay),
}


# ----------------------------------------------------------------- ssim


def ssim(a: Tensor, b: Tensor, window: int = 11) -> Tensor:
    """Mean SSIM over an image batch, differentiable, inputs in [0, 1]."""
    sigma = 1.5
    half = window // 2
    coords = torch.arange(window, device=a.device, dtype=a.dtype) - half
    gauss = torch.exp(-(coords**2) / (2 * sigma**2))
    kernel_1d = (gauss / gauss.sum()).reshape(1, 1, 1, window)
    channels = a.shape[1]

    def blur(x: Tensor) -> Tensor:
        k_row = kernel_1d.expand(channels, 1, 1, window)
        k_col = kernel_1d.reshape(1, 1, window, 1).expand(channels, 1, window, 1)
        x = tf.conv2d(x, k_row, padding=(0, half), groups=channels)
        return tf.conv2d(x, k_col, padding=(half, 0), groups=channels)

    mu_a, mu_b = blur(a), blur(b)
    var_a = blur(a * a) - mu_a**2
    var_b = blur(b * b) - mu_b**2
    cov = blur(a * b) - mu_a * mu_b
    c1, c2 = 0.01**2, 0.03**2
    score = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / (
        (mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2)
    )
    return score.mean()


def image_similarity_loss(derived: Tensor, target: Tensor) -> Tensor:
    """Dream Target regression: SSIM structure term plus pixel MSE (Eq. 5)."""
    return (1.0 - ssim(derived, target)) + tf.mse_loss(derived, target)


def sdedit_steps(base_steps: int, strength: float) -> int:
    """Inference steps for an SDEdit call at `strength`.

    img2img truncates the schedule to int(steps * strength) denoise steps,
    so a fixed few-step schedule rounds the late, low-strength polish
    rounds down to zero steps and returns garbage. Grow the schedule so at
    least two denoise steps always run.
    """
    return max(base_steps, math.ceil(2 / strength))


# ------------------------------------------------------- diffusion adapter


class DiffusionAdapter:
    """Thin wrapper around a frozen latent diffusion pipeline exposing the
    two operations the optimizer needs: an SDS gradient and SDEdit."""

    DEFAULT_DREAM_MODEL = "lykon/dreamshaper-8-lcm"

    def __init__(
        self,
        model_id: str,
        device: str,
        dream_model_id: str | None = DEFAULT_DREAM_MODEL,
    ) -> None:
        from diffusers import (  # imported here: heavy, inference extra only
            AutoPipelineForImage2Image,
            AutoPipelineForText2Image,
        )

        dtype = torch.float16 if device != "cpu" else torch.float32
        self.model_id = model_id
        self.dream_model_id = dream_model_id
        self.pipe = AutoPipelineForText2Image.from_pretrained(
            model_id, torch_dtype=dtype, safety_checker=None
        ).to(device)
        self.pipe.unet.requires_grad_(False)
        self.pipe.vae.requires_grad_(False)
        self.pipe.text_encoder.requires_grad_(False)
        self.img2img = AutoPipelineForImage2Image.from_pipe(self.pipe)
        self.device = device
        self.dtype = dtype
        self.scheduler = self.pipe.scheduler
        self.embeddings: dict[str, Tensor] = {}
        # Dream Target defaults: keep SDS img2img until begin_dream_phase()
        self.dream_inference_steps = 25
        self.dream_guidance = 7.5

    def begin_dream_phase(self) -> None:
        """Free the SDS backbone and load the Dream Target img2img pipeline.

        When `dream_model_id` is set (default LCM), unload SD 1.5 first so
        both models are not resident in VRAM. When unset, keep the SDS
        img2img path with the classic 25-step / CFG 7.5 schedule.
        """
        from diffusers import AutoPipelineForImage2Image, LCMScheduler

        if self.dream_model_id is None or self.dream_model_id == self.model_id:
            self.dream_inference_steps = 25
            self.dream_guidance = 7.5
            return

        del self.pipe
        del self.img2img
        self.pipe = None
        self.scheduler = None
        self.embeddings.clear()
        if self.device != "cpu" and hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()

        self.img2img = AutoPipelineForImage2Image.from_pretrained(
            self.dream_model_id, torch_dtype=self.dtype, safety_checker=None
        ).to(self.device)
        # The LCM checkpoints ship a PNDM scheduler config; sampling an
        # LCM-distilled UNet with it gives mush from weakly structured
        # inputs. Swap in the scheduler the model was distilled for.
        self.img2img.scheduler = LCMScheduler.from_config(self.img2img.scheduler.config)
        self.img2img.unet.requires_grad_(False)
        self.img2img.vae.requires_grad_(False)
        if hasattr(self.img2img, "text_encoder") and self.img2img.text_encoder is not None:
            self.img2img.text_encoder.requires_grad_(False)
        self.dream_inference_steps = 4
        self.dream_guidance = 2.0

    def embed(self, prompt: str) -> Tensor:
        """Cached [uncond, cond] embeddings for classifier-free guidance."""
        if prompt not in self.embeddings:
            cond, uncond = self.pipe.encode_prompt(
                prompt,
                device=self.device,
                num_images_per_prompt=1,
                do_classifier_free_guidance=True,
            )[:2]
            self.embeddings[prompt] = torch.cat([uncond, cond])
        return self.embeddings[prompt]

    def encode_latent(self, image: Tensor) -> Tensor:
        scaled = (image * 2 - 1).to(self.dtype)
        posterior = self.pipe.vae.encode(scaled).latent_dist
        return posterior.sample() * self.pipe.vae.config.scaling_factor

    def sds_loss(self, derived: Tensor, prompt: str, guidance_scale: float, generator) -> Tensor:
        """Score Distillation Loss for one derived image (paper 3.3.1)."""
        return self.sds_loss_batch(derived, [prompt], [1.0], guidance_scale, generator)

    def sds_loss_batch(
        self,
        derived: Tensor,
        prompts: list[str],
        weights: list[float],
        guidance_scale: float,
        generator,
    ) -> Tensor:
        """Batched SDS: one shared timestep and one CFG-doubled UNet call
        for all prompt-target derived images. `derived` is (B, 3, H, W)."""
        if len(prompts) != derived.shape[0] or len(weights) != derived.shape[0]:
            raise ValueError("prompts, weights, and derived batch size must match")
        if derived.shape[0] == 0:
            return torch.zeros((), device=self.device)

        latent = self.encode_latent(derived)
        batch = latent.shape[0]
        train_steps = self.scheduler.config.num_train_timesteps
        timestep = int(
            torch.randint(
                int(0.02 * train_steps),
                int(0.98 * train_steps),
                (1,),
                generator=generator,
                device=self.device,
            ).item()
        )
        timesteps = torch.full((batch,), timestep, device=self.device, dtype=torch.long)
        noise = torch.randn(
            latent.shape,
            generator=generator,
            device=self.device,
            dtype=latent.dtype,
        )
        with torch.no_grad():
            noised = self.scheduler.add_noise(latent.detach(), noise, timesteps)
            # CFG layout: [uncond_0..B-1, cond_0..B-1] matching chunk(2) on dim 0
            model_in = torch.cat([noised, noised], dim=0)
            timesteps_cfg = torch.cat([timesteps, timesteps], dim=0)
            unconds = [self.embed(prompt)[:1] for prompt in prompts]
            conds = [self.embed(prompt)[1:] for prompt in prompts]
            encoder_hidden_states = torch.cat(unconds + conds, dim=0)
            predicted = self.pipe.unet(
                model_in,
                timesteps_cfg,
                encoder_hidden_states=encoder_hidden_states,
            ).sample
            uncond, cond = predicted.chunk(2)
            guided = uncond + guidance_scale * (cond - uncond)
            gradient = (guided - noise).float()
        per_item = (latent.float() * gradient).reshape(batch, -1).sum(dim=1)
        weight_t = torch.tensor(weights, device=per_item.device, dtype=per_item.dtype)
        return (per_item * weight_t).sum()

    def sdedit(self, image: Tensor, prompt: str, strength: float, generator) -> Tensor:
        """SDEdit img2img: noise the derived image and denoise it toward
        the prompt, producing a Dream Target (paper 3.3.2)."""
        strength = max(strength, 0.05)
        with torch.no_grad():
            result = self.img2img(
                prompt=prompt,
                image=image.to(self.dtype),
                strength=strength,
                num_inference_steps=sdedit_steps(self.dream_inference_steps, strength),
                guidance_scale=self.dream_guidance,
                generator=generator,
                output_type="pt",
            ).images
        return result.float().clamp(0, 1)


# ------------------------------------------------------------- optimizer


@dataclass
class IllusionConfig:
    illusion: str
    prompts: list[str]  # one per derived image; a Tensor target may replace
    target_image: Tensor | None = None  # optional fixed target, last slot
    model_id: str = "stable-diffusion-v1-5/stable-diffusion-v1-5"
    dream_model_id: str | None = DiffusionAdapter.DEFAULT_DREAM_MODEL
    sds_steps: int = 500
    sds_guidance: float = 100.0
    sds_low_res: int = 256
    # 0 = ladder off. SDS at 256px runs on 32x32 latents, off-distribution
    # for the SD 1.5 UNet: it stalls subject formation and the Dream Target
    # phase cannot recover the loss. Opt in only for quick throwaway runs.
    sds_low_res_fraction: float = 0.0
    dream_rounds: int = 8
    dream_steps: int = 300
    learning_rate: float = 1e-3
    seed: int = 0
    device: str = "cuda"
    strengths: list[float] = field(default_factory=list)

    def strength_schedule(self) -> list[float]:
        if self.strengths:
            return self.strengths
        # 0.90 down toward 0 (paper 3.3.2); rounds are configurable so the
        # smoke config can run in minutes
        return [
            0.9 * (1 - index / max(self.dream_rounds - 1, 1)) + 0.05
            for index in range(self.dream_rounds)
        ]


@dataclass
class IllusionResult:
    primes: list[Tensor]
    derived: list[Tensor]


def targets_for(config: IllusionConfig, spec: IllusionSpec) -> list[str | Tensor]:
    """Per-derived-image targets: prompts, with the optional image target
    taking the final (hidden) slot."""
    targets: list[str | Tensor] = list(config.prompts)
    if config.target_image is not None:
        targets.append(config.target_image)
    if len(targets) != len(spec.weights):
        raise ValueError(f"{config.illusion} needs {len(spec.weights)} targets, got {len(targets)}")
    return targets


def optimize_illusion(
    config: IllusionConfig, progress: ProgressFn = lambda fraction: None
) -> IllusionResult:
    """Run both optimization phases and return primes and derived images."""
    spec = ILLUSIONS[config.illusion]
    targets = targets_for(config, spec)
    torch.manual_seed(config.seed)
    generator = torch.Generator(device=config.device).manual_seed(config.seed)

    networks = [FourierFeatureNetwork().to(config.device) for _ in range(spec.n_primes)]
    parameters = [p for network in networks for p in network.parameters()]
    optimizer = torch.optim.Adam(parameters, lr=config.learning_rate)
    adapter = DiffusionAdapter(config.model_id, config.device, config.dream_model_id)

    def render_derived(resolution: int = RESOLUTION) -> list[Tensor]:
        return spec.arrange([network.image(resolution) for network in networks])

    total = config.sds_steps + config.dream_rounds * config.dream_steps
    done = 0
    low_res_steps = int(config.sds_steps * config.sds_low_res_fraction)

    # Phase 1: Score Distillation Loss on prompt targets (one UNet call per step)
    for step in range(config.sds_steps):
        resolution = config.sds_low_res if step < low_res_steps else RESOLUTION
        optimizer.zero_grad()
        loss = torch.zeros((), device=config.device)
        prompt_images: list[Tensor] = []
        prompt_texts: list[str] = []
        prompt_weights: list[float] = []
        for derived, target, weight in zip(
            render_derived(resolution), targets, spec.weights, strict=True
        ):
            if isinstance(target, str):
                prompt_images.append(derived)
                prompt_texts.append(target)
                prompt_weights.append(weight)
            else:
                target_image = target.to(config.device)
                if target_image.shape[-1] != resolution:
                    target_image = tf.interpolate(
                        target_image,
                        size=(resolution, resolution),
                        mode="bilinear",
                        align_corners=False,
                    )
                loss = loss + weight * image_similarity_loss(derived, target_image)
        if prompt_images:
            loss = loss + adapter.sds_loss_batch(
                torch.cat(prompt_images, dim=0),
                prompt_texts,
                prompt_weights,
                config.sds_guidance,
                generator,
            )
        loss.backward()
        optimizer.step()
        done += 1
        progress(done / total)

    adapter.begin_dream_phase()

    # Phase 2: Dream Target Loss at a decreasing SDEdit strength schedule
    for strength in config.strength_schedule():
        dream_targets: list[Tensor] = []
        with torch.no_grad():
            current = render_derived(RESOLUTION)
        for derived, target in zip(current, targets, strict=True):
            if isinstance(target, str):
                dream_targets.append(adapter.sdedit(derived, target, strength, generator))
            else:
                dream_targets.append(target.to(config.device))
        for _ in range(config.dream_steps):
            optimizer.zero_grad()
            loss = torch.zeros((), device=config.device)
            for derived, dream, weight in zip(
                render_derived(RESOLUTION), dream_targets, spec.weights, strict=True
            ):
                loss = loss + weight * image_similarity_loss(derived, dream)
            loss.backward()
            optimizer.step()
            done += 1
            progress(done / total)

    with torch.no_grad():
        primes = [network.image().clamp(0, 1) for network in networks]
        final = [image.clamp(0, 1) for image in spec.arrange(primes)]
    return IllusionResult(primes=primes, derived=final)


# ------------------------------------------------------------------- cli


def save_image(tensor: Tensor, path: Path) -> None:
    from PIL import Image

    array = (tensor.squeeze(0).permute(1, 2, 0).cpu().numpy() * 255).clip(0, 255).astype("uint8")
    Image.fromarray(array).save(path)


def load_image(path: Path, resolution: int = RESOLUTION) -> Tensor:
    from PIL import Image

    with Image.open(path) as image:
        rgb = image.convert("RGB").resize((resolution, resolution))
    data = torch.frombuffer(bytearray(rgb.tobytes()), dtype=torch.uint8)
    pixels = data.reshape(resolution, resolution, 3).float() / 255.0
    return pixels.permute(2, 0, 1).unsqueeze(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=sorted(ILLUSIONS), required=True)
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        help="one per derived image, in arrangement order",
    )
    parser.add_argument(
        "--target-image",
        type=Path,
        default=None,
        help="fixed image target for the final derived slot (e.g. a QR code)",
    )
    parser.add_argument("--model", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    parser.add_argument(
        "--dream-model",
        default=DiffusionAdapter.DEFAULT_DREAM_MODEL,
        help=(
            "img2img checkpoint for Dream Targets (default: LCM, 4 steps). "
            "Pass 'none' to reuse --model with the classic 25-step schedule."
        ),
    )
    parser.add_argument("--sds-steps", type=int, default=500)
    parser.add_argument("--sds-guidance", type=float, default=100.0)
    parser.add_argument(
        "--sds-low-res",
        type=int,
        default=256,
        help="render resolution for the early SDS ladder stage",
    )
    parser.add_argument(
        "--sds-low-res-fraction",
        type=float,
        default=0.0,
        help=(
            "fraction of SDS steps run at --sds-low-res before finishing at "
            "512; 0 (default) disables the ladder - 256px SDS stalls subject "
            "formation on SD 1.5"
        ),
    )
    parser.add_argument("--dream-rounds", type=int, default=8)
    parser.add_argument("--dream-steps", type=int, default=300)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()

    dream_model = None if args.dream_model.lower() == "none" else args.dream_model
    config = IllusionConfig(
        illusion=args.type,
        prompts=args.prompt,
        target_image=(load_image(args.target_image) if args.target_image else None),
        model_id=args.model,
        dream_model_id=dream_model,
        sds_steps=args.sds_steps,
        sds_guidance=args.sds_guidance,
        sds_low_res=args.sds_low_res,
        sds_low_res_fraction=args.sds_low_res_fraction,
        dream_rounds=args.dream_rounds,
        dream_steps=args.dream_steps,
        seed=args.seed,
        device=args.device,
    )
    result = optimize_illusion(config, progress=lambda f: print(f"\rprogress {f:6.1%}", end=""))
    print()

    args.out.mkdir(parents=True, exist_ok=True)
    for index, prime in enumerate(result.primes, start=1):
        save_image(prime, args.out / f"prime_{index}.png")
    for index, derived in enumerate(result.derived, start=1):
        save_image(derived, args.out / f"derived_{index}.png")
    print(
        f"wrote {len(result.primes)} primes and {len(result.derived)} derived images to {args.out}"
    )


if __name__ == "__main__":
    main()
