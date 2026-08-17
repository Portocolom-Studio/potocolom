"""Diffusion Illusions optimizer (issue #115 / PR #118 reliability).

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

    python -m worker.illusions --type flip --prompt "a dog" \\
        --prompt "a sloth" --out out/flip

torch is imported at module level on purpose: this module is only useful
with the inference extra installed. Nothing else in the package imports
it, so the worker still runs without torch.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as tf
from torch import Tensor, nn

ProgressFn = Callable[[float], None]


@dataclass
class PhaseEvent:
    """Typed optimization phase observer payload.

    phase examples: sds_begin, sds_0060, sds_end, dream_begin, dream_round_01,
    dream_end, final.

    arm names the Dream arm the event belongs to when the Dream phase was
    forked from one SDS state; it is None for the shared SDS phase and for
    unforked runs.
    """

    phase: str
    arm: str | None = None
    step: int | None = None
    round: int | None = None
    strength: float | None = None
    primes: list[Tensor] | None = None
    derived: list[Tensor] | None = None
    targets: list[Tensor] | None = None
    loss: float | None = None
    loss_start: float | None = None
    loss_end: float | None = None
    grad_norm: float | None = None
    wall_s: float | None = None
    diagnostics: dict[str, Any] | None = None


PhaseFn = Callable[[PhaseEvent], None]
# Backward-compatible alias used by older harness code.
CheckpointFn = Callable[[str, list[Tensor], list[Tensor], dict[str, Any]], None]

RESOLUTION = 512

SDS_OBJECTIVES = ("legacy", "weighted_sds", "csd", "nfsd")

# NFSD published negative prompt (Katzir et al.)
NFSD_NEGATIVE_PROMPT = (
    "unrealistic, blurry, low quality, out of focus, ugly, low contrast, "
    "dull, dark, low-resolution, gloomy"
)

# Style templates: {} is replaced by the semantic subject text.
# "oil" keeps the exact legacy wording. "coherent_oil" is a distinct control.
STYLE_TEMPLATES: dict[str, str] = {
    "oil": "an oil painting of {}",
    "coherent_oil": "a coherent oil painting of {}",
    "pencil": "a detailed HB pencil sketch of {}",
    "editorial": "a centered editorial illustration of {} with a clear silhouette",
    # The wording of the one author-reference cell that has actually been run
    # and passed human review (the giraffe/penguin calibration smoke).
    "reference_sketch": "an intricate detailed hb pencil sketch of {}",
    # The heavier scaffolding the untested reference pairs carry, and that same
    # scaffolding with only the medium swapped. Holding the framing words
    # identical is what makes a pencil-versus-oil arm an attribution test of
    # the medium alone.
    "reference_pencil": (
        "a centered intricate HB pencil illustration of {}"
        ", full object, strong silhouette, isolated on plain warm paper"
    ),
    "reference_oil": (
        "a centered intricate oil painting of {}"
        ", full object, strong silhouette, isolated on plain warm canvas"
    ),
    # Window 3's wording screen, targeting the trade neither validated wording
    # wins: reference_sketch reads better raw (35 of 72 against 25) but loses 31 of
    # 72 to its own frames, while oil produces 0 frames in 78 and only ties on the
    # clean endpoint.
    #
    # BOTH were smoked at 1,500 steps before any block time was committed, on
    # moose_butterfly seed 11, against a plain-oil control at the same step count.
    # Chroma is the measure_colour statistic; the colour threshold is 20.
    #
    #   oil control       66.0 / 57.2   clean, full bleed
    #   monochrome_oil    18.6 / 27.0   WOODEN PICTURE FRAME, both arms
    #   charcoal           9.9 /  7.1   clean, faint edge only
    #
    # monochrome_oil is CUT and kept here only so it is not tried again. The word
    # "monochrome" works on colour - it cuts chroma by about 60% against the
    # control - but it also summons a framed painting, which is worse than anything
    # plain oil produced in 78 observations, and frame-cleanliness was the entire
    # reason to start from oil. "Monochrome oil painting" is auction-catalogue
    # vocabulary, where the images genuinely are photographs of framed paintings.
    #
    # It also refuted the mechanism this screen was built on. The claim was that
    # frames come from naming a PAPER-BOUND artifact. charcoal is paper-bound and
    # clean; monochrome_oil is canvas-bound and framed. Both directions fail, so
    # frame behaviour is a property of the SPECIFIC PHRASE and is not derivable
    # from the medium. Screen candidate strings with an 8-minute smoke; do not
    # reason about them.
    "monochrome_oil": "a monochrome oil painting of {}",
    # The live candidate, and it was included as the control. Pencil-grade
    # monochrome (9.9/7.1 against reference_sketch's 9.2 median) with none of
    # pencil's frames, which is what the hypothesis wanted from the other one.
    "charcoal": "a detailed charcoal drawing of {}",
}

# Square-root SDS timestep anneal (NOT full HiFA): endpoints and exponent.
SQRT_ANNEAL_T_HIGH = 0.98
SQRT_ANNEAL_T_LOW = 0.02
SQRT_ANNEAL_EXPONENT = 0.5


# ---------------------------------------------------------------- primes


class FourierFeatureNetwork(nn.Module):
    """Implicit image: pixel coordinates -> RGB through fixed Fourier
    features and a small MLP. High-frequency-capable but smooth enough to
    survive printing, per the paper's Sec. 4.3."""

    frequencies: Tensor
    _coord_cache: dict[tuple[int, torch.device], Tensor]

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
        self._coord_cache = {}

    def forward(self, coords: Tensor) -> Tensor:
        projected = 2 * math.pi * coords @ self.frequencies
        encoded = torch.cat([torch.sin(projected), torch.cos(projected)], dim=-1)
        return torch.sigmoid(self.mlp(encoded))

    def image(self, resolution: int = RESOLUTION) -> Tensor:
        """Render to a (1, 3, H, W) image in [0, 1]."""
        device = self.frequencies.device
        key = (resolution, device)
        grid = self._coord_cache.get(key)
        if grid is None:
            axis = torch.linspace(-1.0, 1.0, resolution, device=device)
            grid = torch.stack(torch.meshgrid(axis, axis, indexing="ij"), dim=-1)
            self._coord_cache[key] = grid
        rgb = self(grid.reshape(-1, 2)).reshape(resolution, resolution, 3)
        return rgb.permute(2, 0, 1).unsqueeze(0)


class ReferenceFourierFeatureNetwork(nn.Module):
    """The 256px Fourier network used by the authors' public notebook.

    This is experiment-only capacity. The legacy network remains the default.
    """

    frequencies: Tensor
    _coord_cache: dict[tuple[int, torch.device], Tensor]

    def __init__(self, features: int = 128, hidden: int = 256, scale: float = 10.0) -> None:
        super().__init__()
        self.register_buffer("frequencies", torch.randn(2, features) * scale)
        encoded = 2 * features
        self.model = nn.Sequential(
            nn.Conv2d(encoded, hidden, kernel_size=1),
            nn.ReLU(),
            nn.BatchNorm2d(hidden),
            nn.Conv2d(hidden, hidden, kernel_size=1),
            nn.ReLU(),
            nn.BatchNorm2d(hidden),
            nn.Conv2d(hidden, hidden, kernel_size=1),
            nn.ReLU(),
            nn.BatchNorm2d(hidden),
            nn.Conv2d(hidden, 3, kernel_size=1),
            nn.Sigmoid(),
        )
        self._coord_cache = {}

    def forward(self, coords: Tensor) -> Tensor:
        """Map a (B, 2, H, W) UV grid to a (B, 3, H, W) image."""
        batch, channels, height, width = coords.shape
        if channels != 2:
            raise ValueError("reference Fourier coordinates need two channels")
        flat = coords.permute(0, 2, 3, 1).reshape(-1, 2)
        projected = 2 * math.pi * flat @ self.frequencies
        encoded = torch.cat([torch.sin(projected), torch.cos(projected)], dim=-1)
        encoded = encoded.reshape(batch, height, width, -1).permute(0, 3, 1, 2)
        return self.model(encoded)

    def image(self, resolution: int = 256) -> Tensor:
        """Render the native author canvas, with coordinates in [0, 1)."""
        device = self.frequencies.device
        key = (resolution, device)
        grid = self._coord_cache.get(key)
        if grid is None:
            axis = torch.arange(resolution, device=device, dtype=torch.float32) / resolution
            grid = torch.stack(torch.meshgrid(axis, axis, indexing="ij"), dim=0).unsqueeze(0)
            self._coord_cache[key] = grid
        return self(grid)


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


_SSIM_KERNEL_CACHE: dict[tuple[int, str, torch.dtype, int], tuple[Tensor, Tensor]] = {}


def _ssim_kernels(
    channels: int, window: int, device: torch.device, dtype: torch.dtype
) -> tuple[Tensor, Tensor]:
    key = (window, str(device), dtype, channels)
    cached = _SSIM_KERNEL_CACHE.get(key)
    if cached is not None:
        return cached
    sigma = 1.5
    half = window // 2
    coords = torch.arange(window, device=device, dtype=dtype) - half
    gauss = torch.exp(-(coords**2) / (2 * sigma**2))
    kernel_1d = (gauss / gauss.sum()).reshape(1, 1, 1, window)
    k_row = kernel_1d.expand(channels, 1, 1, window).contiguous()
    k_col = kernel_1d.reshape(1, 1, window, 1).expand(channels, 1, window, 1).contiguous()
    _SSIM_KERNEL_CACHE[key] = (k_row, k_col)
    return k_row, k_col


def ssim(a: Tensor, b: Tensor, window: int = 11) -> Tensor:
    """Mean SSIM over an image batch, differentiable, inputs in [0, 1]."""
    half = window // 2
    channels = a.shape[1]
    k_row, k_col = _ssim_kernels(channels, window, a.device, a.dtype)

    def blur(x: Tensor) -> Tensor:
        x = tf.conv2d(x, k_row, padding=(0, half), groups=channels)
        return tf.conv2d(x, k_col, padding=(half, 0), groups=channels)

    mu_a, mu_b = blur(a), blur(b)
    var_a = (blur(a * a) - mu_a**2).clamp(min=0)
    var_b = (blur(b * b) - mu_b**2).clamp(min=0)
    cov = blur(a * b) - mu_a * mu_b
    c1, c2 = 0.01**2, 0.03**2
    score = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / (
        (mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2)
    )
    return score.mean()


def image_similarity_loss(derived: Tensor, target: Tensor) -> Tensor:
    """Dream Target regression: SSIM structure term plus pixel MSE (Eq. 5)."""
    return (1.0 - ssim(derived, target)) + tf.mse_loss(derived, target)


def reconcile_flip(predictions: list[Tensor]) -> list[Tensor]:
    """Average the two flip views' predicted images in the canonical
    (view 1) frame and hand back per-view orientations of the consensus,
    so both views are exact 180-degree rotations of ONE image."""
    canonical = (predictions[0] + rot90(predictions[1], 2)) / 2
    return [canonical, rot90(canonical, 2)]


def sdedit_steps(base_steps: int, strength: float) -> int:
    """Inference steps for an SDEdit call at `strength`.

    img2img truncates the schedule to int(steps * strength) denoise steps,
    so a fixed few-step schedule rounds the late, low-strength polish
    rounds down to zero steps and returns garbage. Grow the schedule so at
    least two denoise steps always run.
    """
    return max(base_steps, math.ceil(2 / strength))


def apply_style_template(subject: str, style: str | None) -> str:
    """Wrap a semantic subject in a style template. Subject text is preserved."""
    if style is None or style == "none":
        return subject
    if style not in STYLE_TEMPLATES:
        raise ValueError(f"unknown style {style!r}; choose from {sorted(STYLE_TEMPLATES)}")
    return STYLE_TEMPLATES[style].format(subject)


def sqrt_anneal_timestep_fraction(progress: float) -> float:
    """Square-root timestep anneal: high noise -> low as
    t(p)=T_low+(T_high-T_low)*(1-p**exponent). Not the full HiFA method."""
    p = min(max(progress, 0.0), 1.0)
    span = SQRT_ANNEAL_T_HIGH - SQRT_ANNEAL_T_LOW
    return SQRT_ANNEAL_T_LOW + span * (1.0 - (p**SQRT_ANNEAL_EXPONENT))


def hifa_timestep_fraction(progress: float) -> float:
    """Deprecated alias for sqrt_anneal_timestep_fraction."""
    return sqrt_anneal_timestep_fraction(progress)


def sds_timestep_weight(alphas_cumprod: Tensor, timestep: int) -> Tensor:
    """Reference SDS weighting w(t) = 1 - alpha_cumprod[t]."""
    return 1.0 - alphas_cumprod[timestep].float()


def add_training_noise(
    latent: Tensor,
    noise: Tensor,
    timesteps: Tensor,
    alphas_cumprod: Tensor,
) -> Tensor:
    """Forward-process noising independent of an inference scheduler.

    Euler's ``add_noise`` uses its inference sigma parameterization and its
    result must normally pass through ``scale_model_input``. SDS instead
    needs the diffusion training equation directly.
    """
    alpha = alphas_cumprod.to(device=latent.device, dtype=latent.dtype)[timesteps]
    while alpha.ndim < latent.ndim:
        alpha = alpha.unsqueeze(-1)
    return alpha.sqrt() * latent + (1 - alpha).sqrt() * noise


def balanced_view_schedule(steps: int, views: int, seed: int) -> list[int]:
    """Deterministically shuffle equal view exposures for reference SDS."""
    if steps <= 0 or views <= 0 or steps % views:
        raise ValueError("steps must be positive and divisible by views")
    schedule = [view for view in range(views) for _ in range(steps // views)]
    random.Random(seed).shuffle(schedule)
    return schedule


def compute_sds_gradient(
    *,
    objective: str,
    noise: Tensor,
    uncond: Tensor,
    cond: Tensor,
    guidance_scale: float,
    weight_t: Tensor,
    delta_d: Tensor | None = None,
) -> Tensor:
    """Build the detached SDS/CSD/NFSD residual used as the latent gradient.

    Formulas (then applied via (latent * gradient).sum()):
      legacy:       (eps_cfg - noise)
      weighted_sds: w(t) * (eps_cfg - noise)
      csd:          w(t) * (eps_cond - eps_uncond)   # canonical; ignores guidance
      nfsd:         w(t) * (delta_D + guidance * delta_C)
    """
    if objective not in SDS_OBJECTIVES:
        raise ValueError(f"unknown sds_objective {objective!r}")
    delta_c = cond - uncond
    if objective == "csd":
        return (weight_t * delta_c).float()
    if objective == "nfsd":
        if delta_d is None:
            raise ValueError("nfsd requires delta_d")
        return (weight_t * (delta_d + guidance_scale * delta_c)).float()
    guided = uncond + guidance_scale * delta_c
    residual = (guided - noise).float()
    if objective == "weighted_sds":
        return (weight_t * residual).float()
    return residual


def nfsd_delta_d(
    *,
    timestep: int,
    uncond: Tensor,
    neg: Tensor | None,
) -> Tensor:
    """NFSD domain branch: t < 200 uses uncond; else uncond - neg."""
    if timestep < 200:
        return uncond
    if neg is None:
        raise ValueError("nfsd requires neg prediction for t >= 200")
    return uncond - neg


def param_grad_norm(parameters: Sequence[nn.Parameter]) -> float:
    """L2 norm of all parameter gradients without concatenating tensors."""
    total = 0.0
    for param in parameters:
        if param.grad is not None:
            total += float(param.grad.detach().float().norm().item() ** 2)
    return total**0.5


def gradient_conflict_stats(view_grads: list[Tensor]) -> dict[str, float]:
    """Per-view FFN gradient cosine similarity and norm ratio diagnostics."""
    if len(view_grads) < 2:
        return {"cosine": 1.0, "norm_ratio": 1.0, "norm_0": 0.0, "norm_1": 0.0}
    g0, g1 = view_grads[0].float(), view_grads[1].float()
    n0 = g0.norm().item()
    n1 = g1.norm().item()
    denom = max(n0 * n1, 1e-12)
    cosine = float((g0 @ g1).item() / denom)
    ratio = max(n0, n1) / max(min(n0, n1), 1e-12)
    return {"cosine": cosine, "norm_ratio": ratio, "norm_0": n0, "norm_1": n1}


def preserve_rng_state(fn: Callable[[], Any]) -> Any:
    """Run fn while restoring global CPU/CUDA RNG state afterward."""
    cpu_state = torch.get_rng_state()
    cuda_states = None
    if torch.cuda.is_available():
        cuda_states = torch.cuda.get_rng_state_all()
    try:
        return fn()
    finally:
        torch.set_rng_state(cpu_state)
        if cuda_states is not None:
            torch.cuda.set_rng_state_all(cuda_states)


def resolve_learning_rates(config: IllusionConfig) -> tuple[float, float]:
    """Resolve sds_lr/dream_lr from learning_rate; explicit phase values win."""
    base = config.learning_rate
    sds = base if config.sds_lr is None else config.sds_lr
    dream = base if config.dream_lr is None else config.dream_lr
    return sds, dream


def checkpoint_name(sds_step: int) -> str:
    """Phase-qualified SDS checkpoint name (never reused for final)."""
    return f"sds_{sds_step:04d}"


def latent_shape_for(image: Tensor) -> tuple[int, int, int, int]:
    """SD-class VAE latent shape for an (B,3,H,W) image."""
    batch, _, height, width = image.shape
    return batch, 4, height // 8, width // 8


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
        *,
        enable_vae_slicing: bool = False,
        channels_last: bool = False,
        view_batch_size: int | None = None,
        sds_objective: str = "legacy",
        sds_gradient_scale: float = 1.0,
        vae_id: str | None = None,
        model_variant: str | None = None,
    ) -> None:
        from diffusers import (  # imported here: heavy, inference extra only
            AutoencoderKL,
            AutoPipelineForImage2Image,
            AutoPipelineForText2Image,
        )

        dtype = torch.float16 if device != "cpu" else torch.float32
        self.model_id = model_id
        self.dream_model_id = dream_model_id
        self.vae_id = vae_id
        self.model_variant = model_variant
        self.sds_objective = sds_objective
        self.sds_gradient_scale = sds_gradient_scale
        self.view_batch_size = view_batch_size
        local_only = os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes")
        load_kwargs: dict[str, Any] = {
            "torch_dtype": dtype,
            "local_files_only": local_only,
            # Local SD1.5 snapshots often omit CompVis safety-checker weights;
            # HF_HUB_OFFLINE cannot fetch them. Illusion SDS never uses the checker.
            "safety_checker": None,
            "requires_safety_checker": False,
        }
        if model_variant:
            load_kwargs["variant"] = model_variant
        if vae_id:
            vae_kwargs: dict[str, Any] = {"torch_dtype": dtype, "local_files_only": local_only}
            if model_variant:
                # fp16-fix VAE usually has no variant suffix; try plain first.
                try:
                    load_kwargs["vae"] = AutoencoderKL.from_pretrained(vae_id, **vae_kwargs)
                except OSError:
                    vae_kwargs["variant"] = model_variant
                    load_kwargs["vae"] = AutoencoderKL.from_pretrained(vae_id, **vae_kwargs)
            else:
                load_kwargs["vae"] = AutoencoderKL.from_pretrained(vae_id, **vae_kwargs)
        self.pipe = AutoPipelineForText2Image.from_pretrained(model_id, **load_kwargs).to(device)
        self._configure_modules(self.pipe, enable_vae_slicing, channels_last)
        self.img2img = AutoPipelineForImage2Image.from_pipe(self.pipe)
        self.device = device
        self.dtype = dtype
        self.scheduler = self.pipe.scheduler
        # hidden: [uncond, cond]; pooled: optional SDXL [uncond, cond] or None
        self.embeddings: dict[str, tuple[Tensor, Tensor | None]] = {}
        self.dream_embeddings: dict[str, tuple[Tensor, Tensor | None]] = {}
        # Dream Target defaults: keep SDS img2img until begin_dream_phase()
        self.dream_inference_steps = 25
        self.dream_guidance = 7.5
        self._enable_vae_slicing = enable_vae_slicing
        self._channels_last = channels_last
        # Test hook: each encode_latent appends the input batch size.
        self.encode_batch_sizes: list[int] = []
        self.backward_before_next_encode: list[bool] = []
        self._encodes_since_backward = 0

    def _is_sdxl(self) -> bool:
        pipe = self.pipe if self.pipe is not None else self.img2img
        return bool(
            pipe is not None
            and hasattr(pipe, "text_encoder_2")
            and getattr(pipe, "text_encoder_2", None) is not None
        )

    def _sdxl_time_ids(self, batch: int) -> Tensor:
        # Match diffusers' default 512px pipeline conditioning. Claiming a
        # 1024px original for a canvas that was generated at 512 is false
        # micro-conditioning and was one cause of the failed pilot.
        values = [RESOLUTION, RESOLUTION, 0, 0, RESOLUTION, RESOLUTION]
        return torch.tensor([values], device=self.device, dtype=self.dtype).repeat(batch, 1)

    def _configure_modules(self, pipe: Any, enable_vae_slicing: bool, channels_last: bool) -> None:
        pipe.unet.requires_grad_(False)
        pipe.vae.requires_grad_(False)
        if hasattr(pipe, "text_encoder") and pipe.text_encoder is not None:
            pipe.text_encoder.requires_grad_(False)
        if hasattr(pipe, "text_encoder_2") and pipe.text_encoder_2 is not None:
            pipe.text_encoder_2.requires_grad_(False)
        if enable_vae_slicing and hasattr(pipe.vae, "enable_slicing"):
            pipe.vae.enable_slicing()
        if channels_last and self_device_is_cuda(pipe):
            pipe.unet.to(memory_format=torch.channels_last)
            pipe.vae.to(memory_format=torch.channels_last)

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
            self.embeddings.clear()  # cached SDS embeddings are not used after phase 1
            return

        del self.pipe
        del self.img2img
        self.pipe = None
        self.scheduler = None
        self.embeddings.clear()
        if self.device != "cpu" and hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()

        local_only = os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes")
        dream_kwargs: dict[str, Any] = {
            "torch_dtype": self.dtype,
            "local_files_only": local_only,
            "safety_checker": None,
            "requires_safety_checker": False,
        }
        # vae_id/model_variant describe the SDS backbone. Do not leak an
        # SDXL VAE or variant into a different Dream Target checkpoint.
        self.img2img = AutoPipelineForImage2Image.from_pretrained(
            self.dream_model_id,
            **dream_kwargs,
        ).to(self.device)
        # The LCM checkpoints ship a PNDM scheduler config; sampling an
        # LCM-distilled UNet with it gives mush from weakly structured
        # inputs. Swap in the scheduler the model was distilled for.
        self.img2img.scheduler = LCMScheduler.from_config(self.img2img.scheduler.config)
        self.img2img.unet.requires_grad_(False)
        self.img2img.vae.requires_grad_(False)
        if hasattr(self.img2img, "text_encoder") and self.img2img.text_encoder is not None:
            self.img2img.text_encoder.requires_grad_(False)
        if hasattr(self.img2img, "text_encoder_2") and self.img2img.text_encoder_2 is not None:
            self.img2img.text_encoder_2.requires_grad_(False)
        if getattr(self, "_enable_vae_slicing", False) and hasattr(
            self.img2img.vae, "enable_slicing"
        ):
            self.img2img.vae.enable_slicing()
        if getattr(self, "_channels_last", False) and self.device != "cpu":
            self.img2img.unet.to(memory_format=torch.channels_last)
            self.img2img.vae.to(memory_format=torch.channels_last)
        self.dream_inference_steps = 4
        self.dream_guidance = 2.0

    def embed(self, prompt: str) -> tuple[Tensor, Tensor | None]:
        """Cached ([uncond, cond] hidden, optional [uncond, cond] pooled for SDXL)."""
        if prompt not in self.embeddings:
            with torch.no_grad():
                encoded = self.pipe.encode_prompt(
                    prompt,
                    device=self.device,
                    num_images_per_prompt=1,
                    do_classifier_free_guidance=True,
                )
                cond, uncond = encoded[0], encoded[1]
                pooled = None
                if len(encoded) >= 4 and encoded[2] is not None and encoded[3] is not None:
                    pooled = torch.cat([encoded[3], encoded[2]])
                self.embeddings[prompt] = (torch.cat([uncond, cond]), pooled)
        return self.embeddings[prompt]

    def encode_latent(
        self,
        image: Tensor,
        *,
        generator=None,
        use_mean: bool = False,
        posterior_eps: Tensor | None = None,
    ) -> Tensor:
        """VAE-encode an image to latents.

        Legacy default (PR #118 / 5f30fdd): ``posterior.sample()`` draws from
        the globally seeded RNG (``torch.manual_seed`` in optimize_illusion).
        Do not pass the SDS ``generator`` into ``sample()`` on that path.

        use_mean: deterministic posterior mean (diagnostics only).
        posterior_eps: explicit N(0,1) sample for opt-in microbatch comparison.
        ``generator`` is accepted for API compatibility but ignored unless
        posterior_eps is used to pre-sample noise elsewhere.
        """
        del generator  # legacy path must not consume the SDS generator here
        self.encode_batch_sizes.append(int(image.shape[0]))
        if self._encodes_since_backward > 0:
            self.backward_before_next_encode.append(False)
        self._encodes_since_backward += 1
        scaled = (image * 2 - 1).to(self.dtype)
        posterior = self.pipe.vae.encode(scaled).latent_dist
        if use_mean:
            latent = posterior.mean
        elif posterior_eps is not None:
            latent = posterior.mean + posterior.std * posterior_eps.to(
                device=posterior.mean.device, dtype=posterior.mean.dtype
            )
        else:
            # Exact legacy: global RNG, no SDS generator threading.
            latent = posterior.sample()
        return latent * self.pipe.vae.config.scaling_factor

    def note_backward(self) -> None:
        """Record that a backward completed before the next encode (tests)."""
        if self._encodes_since_backward > 0:
            self.backward_before_next_encode.append(True)
        self._encodes_since_backward = 0

    def sds_loss(self, derived: Tensor, prompt: str, guidance_scale: float, generator) -> Tensor:
        """Score Distillation Loss for one derived image (paper 3.3.1)."""
        return self.sds_loss_batch(
            derived,
            [prompt],
            [1.0],
            guidance_scale,
            generator,
            objective=self.sds_objective,
        )

    def _sample_sds_noise_and_t(
        self,
        latent: Tensor,
        generator,
        *,
        progress: float | None,
        use_hifa_schedule: bool,
    ) -> tuple[int, Tensor, Tensor]:
        train_steps = self.scheduler.config.num_train_timesteps
        if use_hifa_schedule and progress is not None:
            frac = hifa_timestep_fraction(progress)
            timestep = int(frac * (train_steps - 1))
            timestep = max(0, min(train_steps - 1, timestep))
        else:
            timestep = int(
                torch.randint(
                    int(0.02 * train_steps),
                    int(0.98 * train_steps),
                    (1,),
                    generator=generator,
                    device=self.device,
                ).item()
            )
        noise = torch.randn(
            latent.shape,
            generator=generator,
            device=self.device,
            dtype=latent.dtype,
        )
        return timestep, noise, self.scheduler.alphas_cumprod

    def _unet_cfg(
        self,
        noised: Tensor,
        timesteps: Tensor,
        prompts: list[str],
        *,
        negative_prompts: list[str] | None = None,
    ) -> tuple[Tensor, Tensor, Tensor | None]:
        """Run UNet with CFG layout; optionally a third neg chunk for NFSD."""
        batch = noised.shape[0]
        hidden_unconds = []
        hidden_conds = []
        pooled_unconds = []
        pooled_conds = []
        for prompt in prompts:
            embedded = self.embed(prompt)
            if isinstance(embedded, tuple):
                hidden, pooled = embedded
            else:  # compatibility with SD1.5-only adapter callers
                hidden, pooled = embedded, None
            hidden_unconds.append(hidden[:1])
            hidden_conds.append(hidden[1:])
            if pooled is not None:
                pooled_unconds.append(pooled[:1])
                pooled_conds.append(pooled[1:])

        def _added(pooled_list: list[Tensor], chunk_batch: int) -> dict[str, Tensor] | None:
            if not pooled_list:
                return None
            return {
                "text_embeds": torch.cat(pooled_list, dim=0),
                "time_ids": self._sdxl_time_ids(chunk_batch),
            }

        if negative_prompts is None:
            model_in = torch.cat([noised, noised], dim=0)
            timesteps_cfg = torch.cat([timesteps, timesteps], dim=0)
            encoder_hidden_states = torch.cat(hidden_unconds + hidden_conds, dim=0)
            added = _added(pooled_unconds + pooled_conds, batch * 2)
            kwargs: dict[str, Any] = {"encoder_hidden_states": encoder_hidden_states}
            if added is not None:
                kwargs["added_cond_kwargs"] = added
            predicted = self.pipe.unet(model_in, timesteps_cfg, **kwargs).sample
            uncond, cond = predicted.chunk(2)
            return uncond, cond, None

        hidden_negs = []
        pooled_negs = []
        for prompt in negative_prompts:
            embedded = self.embed(prompt)
            if isinstance(embedded, tuple):
                hidden, pooled = embedded
            else:
                hidden, pooled = embedded, None
            hidden_negs.append(hidden[1:])
            if pooled is not None:
                pooled_negs.append(pooled[1:])
        model_in = torch.cat([noised, noised, noised], dim=0)
        timesteps_cfg = torch.cat([timesteps, timesteps, timesteps], dim=0)
        encoder_hidden_states = torch.cat(hidden_unconds + hidden_conds + hidden_negs, dim=0)
        added = _added(pooled_unconds + pooled_conds + pooled_negs, batch * 3)
        kwargs = {"encoder_hidden_states": encoder_hidden_states}
        if added is not None:
            kwargs["added_cond_kwargs"] = added
        predicted = self.pipe.unet(model_in, timesteps_cfg, **kwargs).sample
        uncond, cond, neg = predicted.split(batch, dim=0)
        return uncond, cond, neg

    def sds_loss_batch(
        self,
        derived: Tensor,
        prompts: list[str],
        weights: list[float],
        guidance_scale: float,
        generator,
        *,
        objective: str | None = None,
        progress: float | None = None,
        use_hifa_schedule: bool = False,
        view_batch_size: int | None = None,
        use_mean: bool = False,
        posterior_eps: Tensor | None = None,
        shared_timestep: int | None = None,
        shared_noise: Tensor | None = None,
    ) -> Tensor:
        """Full-batch SDS loss (single VAE encode of the whole derived batch).

        For memory-saving microbatching that releases VAE graphs between
        views, use sds_microbatch_backward instead.
        """
        objective_name = str(objective or getattr(self, "sds_objective", "legacy"))
        if len(prompts) != derived.shape[0] or len(weights) != derived.shape[0]:
            raise ValueError("prompts, weights, and derived batch size must match")
        if derived.shape[0] == 0:
            return torch.zeros((), device=self.device)

        latent = self.encode_latent(
            derived, generator=generator, use_mean=use_mean, posterior_eps=posterior_eps
        )
        if shared_timestep is not None and shared_noise is not None:
            timestep, noise, alphas = (
                shared_timestep,
                shared_noise,
                self.scheduler.alphas_cumprod,
            )
        else:
            timestep, noise, alphas = self._sample_sds_noise_and_t(
                latent, generator, progress=progress, use_hifa_schedule=use_hifa_schedule
            )
        return self._sds_loss_from_latent(
            latent,
            prompts,
            weights,
            guidance_scale,
            objective=objective_name,
            timestep=timestep,
            noise=noise,
            alphas=alphas,
        )

    def sds_microbatch_backward(
        self,
        derived: Tensor,
        prompts: list[str],
        weights: list[float],
        guidance_scale: float,
        generator,
        *,
        objective: str | None = None,
        progress: float | None = None,
        use_hifa_schedule: bool = False,
        view_batch_size: int = 1,
        posterior_eps: Tensor | None = None,
    ) -> float:
        """Chunk before VAE encode; backward each chunk before the next encode.

        Pre-samples shared timestep and per-view diffusion noise (and optional
        posterior_eps) for the full batch so the summed objective matches a
        full-batch reference. Returns the detached scalar loss for logging.
        """
        objective_name = str(objective or getattr(self, "sds_objective", "legacy"))
        if len(prompts) != derived.shape[0] or len(weights) != derived.shape[0]:
            raise ValueError("prompts, weights, and derived batch size must match")
        batch = derived.shape[0]
        if batch == 0:
            return 0.0
        chunk = max(1, int(view_batch_size))
        # Shape-only noise sample (no encode yet).
        _, channels, lat_h, lat_w = latent_shape_for(derived)
        # Legacy parity: VAE epsilon from global RNG BEFORE any SDS-generator draws.
        if posterior_eps is None:
            posterior_eps = torch.randn(
                batch,
                channels,
                lat_h,
                lat_w,
                device=self.device,
                dtype=self.dtype,
            )
        fake_latent = torch.empty(
            batch, channels, lat_h, lat_w, device=self.device, dtype=self.dtype
        )
        timestep, noise, alphas = self._sample_sds_noise_and_t(
            fake_latent, generator, progress=progress, use_hifa_schedule=use_hifa_schedule
        )
        total = 0.0
        for start in range(0, batch, chunk):
            end = min(start + chunk, batch)
            is_last = end >= batch
            chunk_eps = posterior_eps[start:end]
            latent = self.encode_latent(
                derived[start:end],
                generator=generator,
                posterior_eps=chunk_eps,
            )
            loss = self._sds_loss_from_latent(
                latent,
                prompts[start:end],
                weights[start:end],
                guidance_scale,
                objective=objective_name,
                timestep=timestep,
                noise=noise[start:end],
                alphas=alphas,
            )
            loss.backward(retain_graph=not is_last)
            self.note_backward()
            total += float(loss.detach().item())
            del latent, loss
        return total

    def _sds_loss_from_latent(
        self,
        latent: Tensor,
        prompts: list[str],
        weights: list[float],
        guidance_scale: float,
        *,
        objective: str,
        timestep: int,
        noise: Tensor,
        alphas: Tensor,
    ) -> Tensor:
        batch = latent.shape[0]
        timesteps = torch.full((batch,), timestep, device=self.device, dtype=torch.long)
        weight_t = sds_timestep_weight(alphas.to(self.device), timestep)
        with torch.no_grad():
            noised = self._add_sds_noise(latent.detach(), noise, timesteps, alphas)
            need_neg = objective == "nfsd" and timestep >= 200
            neg_prompts = [NFSD_NEGATIVE_PROMPT] * batch if need_neg else None
            uncond, cond, neg = self._unet_cfg(
                noised, timesteps, prompts, negative_prompts=neg_prompts
            )
            delta_d = None
            if objective == "nfsd":
                delta_d = nfsd_delta_d(timestep=timestep, uncond=uncond, neg=neg)
            gradient = compute_sds_gradient(
                objective=objective,
                noise=noise,
                uncond=uncond,
                cond=cond,
                guidance_scale=guidance_scale,
                weight_t=weight_t,
                delta_d=delta_d,
            )
            gradient = gradient * float(getattr(self, "sds_gradient_scale", 1.0))
        per_item = (latent.float() * gradient).reshape(batch, -1).sum(dim=1)
        weight_arr = torch.tensor(weights, device=per_item.device, dtype=per_item.dtype)
        return (per_item * weight_arr).sum()

    def _add_sds_noise(
        self,
        latent: Tensor,
        noise: Tensor,
        timesteps: Tensor,
        alphas: Tensor,
    ) -> Tensor:
        """Use training noising for Euler; preserve the SD1.5 legacy path."""
        if type(self.scheduler).__name__.startswith("Euler"):
            return add_training_noise(latent, noise, timesteps, alphas)
        return self.scheduler.add_noise(latent, noise, timesteps)

    def sdedit(
        self,
        image: Tensor,
        prompt: str,
        strength: float,
        generator,
        *,
        negative_prompt: str | None = None,
    ) -> Tensor:
        """SDEdit img2img: noise the derived image and denoise it toward
        the prompt, producing a Dream Target (paper 3.3.2).

        negative_prompt is ordinary sampling CFG at the Dream guidance of 2.0.
        None is the pipeline's own default, so an unset negative prompt is the
        pre-window-2 call.
        """
        strength = max(strength, 0.05)
        with torch.no_grad():
            result = self.img2img(
                prompt=prompt,
                negative_prompt=negative_prompt,
                image=image.to(self.dtype),
                strength=strength,
                num_inference_steps=sdedit_steps(self.dream_inference_steps, strength),
                guidance_scale=self.dream_guidance,
                generator=generator,
                output_type="pt",
            ).images
        return result.float().clamp(0, 1)

    def _dream_embed(self, prompt: str) -> tuple[Tensor, Tensor | None]:
        """Cached ([uncond, cond] hidden, optional pooled) from the Dream pipeline."""
        if prompt not in self.dream_embeddings:
            with torch.no_grad():
                encoded = self.img2img.encode_prompt(
                    prompt,
                    device=self.device,
                    num_images_per_prompt=1,
                    do_classifier_free_guidance=True,
                )
                cond, uncond = encoded[0], encoded[1]
                pooled = None
                if len(encoded) >= 4 and encoded[2] is not None and encoded[3] is not None:
                    pooled = torch.cat([encoded[3], encoded[2]])
                self.dream_embeddings[prompt] = (torch.cat([uncond, cond]), pooled)
        return self.dream_embeddings[prompt]

    def _dream_embed_cfg(
        self, prompt: str, negative_prompt: str | None
    ) -> tuple[Tensor, Tensor | None]:
        """[uncond, cond] for the Dream UNet, with the negative prompt taking
        the unconditional slot when one is set. That is what a diffusers
        pipeline does for `negative_prompt`, and it keeps the two-chunk batch
        `sdedit_joint` already runs, so a negative arm costs no extra UNet time."""
        hidden, pooled = self._dream_embed(prompt)
        if negative_prompt is None:
            return hidden, pooled
        neg_hidden, neg_pooled = self._dream_embed(negative_prompt)
        combined = torch.cat([neg_hidden[1:], hidden[1:]])
        if pooled is None or neg_pooled is None:
            return combined, None
        return combined, torch.cat([neg_pooled[1:], pooled[1:]])

    def sdedit_joint(
        self,
        views: list[Tensor],
        prompts: list[str],
        strength: float,
        generator,
        *,
        negative_prompt: str | None = None,
    ) -> list[Tensor]:
        """Joint SDEdit for the flip illusion (issue #134): denoise both
        views together, reconciling their predicted images at every step so
        the returned targets are two orientations of ONE image satisfying
        both prompts - unlike independent per-view SDEdit, whose targets
        disagree about the shared pixels by construction.

        The reconciliation runs in pixel space on the predicted x0: the SD
        1.5 VAE does not commute with rot180 in latent space (measured
        0.78-0.97 relative latent error on gallery images), so rotating
        latents directly would corrupt the views.
        """
        strength = max(strength, 0.05)
        steps = sdedit_steps(self.dream_inference_steps, strength)
        vae = self.img2img.vae
        unet = self.img2img.unet
        scaling = vae.config.scaling_factor

        def encode(image: Tensor) -> Tensor:
            posterior = vae.encode((image * 2 - 1).to(self.dtype)).latent_dist
            return posterior.mean * scaling

        def decode(latent: Tensor) -> Tensor:
            image = vae.decode(latent / scaling).sample
            return ((image.float() + 1) / 2).clamp(0, 1)

        # one scheduler per view: PNDM keeps multistep history, so the two
        # trajectories must not share internal state
        schedulers = []
        for _ in views:
            scheduler = type(self.img2img.scheduler).from_config(self.img2img.scheduler.config)
            scheduler.set_timesteps(steps, device=self.device)
            schedulers.append(scheduler)
        order = getattr(schedulers[0], "order", 1)
        t_start = max(steps - int(steps * strength), 0)
        timesteps = schedulers[0].timesteps[t_start * order :]
        if len(timesteps) == 0:
            return [view.float().clamp(0, 1) for view in views]

        with torch.no_grad():
            latents = []
            for view, scheduler in zip(views, schedulers, strict=True):
                latent = encode(view)
                noise = torch.randn(
                    latent.shape, generator=generator, device=self.device, dtype=latent.dtype
                )
                latents.append(scheduler.add_noise(latent, noise, timesteps[:1]))
            for timestep in timesteps:
                alpha = (
                    schedulers[0].alphas_cumprod[timestep].to(device=self.device, dtype=self.dtype)
                )
                sqrt_alpha = alpha.sqrt()
                sqrt_one_minus = (1 - alpha).sqrt()
                predictions = []
                for latent, prompt, scheduler in zip(latents, prompts, schedulers, strict=True):
                    model_in = scheduler.scale_model_input(torch.cat([latent, latent]), timestep)
                    hidden, pooled = self._dream_embed_cfg(prompt, negative_prompt)
                    added = None
                    if pooled is not None:
                        added = {
                            "text_embeds": pooled,
                            "time_ids": self._sdxl_time_ids(model_in.shape[0]),
                        }
                    kwargs: dict[str, Any] = {"encoder_hidden_states": hidden}
                    if added is not None:
                        kwargs["added_cond_kwargs"] = added
                    predicted = unet(model_in, timestep, **kwargs).sample
                    uncond, cond = predicted.chunk(2)
                    epsilon = uncond + self.dream_guidance * (cond - uncond)
                    predictions.append(decode((latent - sqrt_one_minus * epsilon) / sqrt_alpha))
                consensus = reconcile_flip(predictions)
                for index, (latent, scheduler) in enumerate(zip(latents, schedulers, strict=True)):
                    z0 = encode(consensus[index])
                    epsilon = (latent - sqrt_alpha * z0) / sqrt_one_minus
                    latents[index] = scheduler.step(epsilon, timestep, latent).prev_sample
            target = decode(latents[0]).clamp(0, 1)
        return [target, rot90(target, 2)]


def self_device_is_cuda(pipe: Any) -> bool:
    try:
        device = next(pipe.unet.parameters()).device
        return device.type == "cuda"
    except StopIteration:
        return False


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
    sds_objective: str = "legacy"
    sds_low_res: int = 256
    # 0 = ladder off. SDS at 256px runs on 32x32 latents, off-distribution
    # for the SD 1.5 UNet: it stalls subject formation and the Dream Target
    # phase cannot recover the loss. Opt in only for quick throwaway runs.
    sds_low_res_fraction: float = 0.0
    use_hifa_schedule: bool = False
    round_robin: bool = False
    view_batch_size: int | None = None
    # Capacity opts stay off until measured; experiment harness opts in.
    enable_vae_slicing: bool = False
    channels_last: bool = False
    # Optional VAE override (e.g. madebyollin/sdxl-vae-fp16-fix for SDXL).
    vae_id: str | None = None
    # Diffusers weight variant (e.g. "fp16" for SDXL Hub snapshots).
    model_variant: str | None = None
    dream_rounds: int = 8
    # joint targets (issue #134): flip only - rotate/hidden views are
    # overlays of several primes, not orthogonal transforms of one image
    dream_joint: bool = False
    # Dream/SDEdit only, where guidance is 2.0. Deliberately NOT routed into
    # SDS: replacing the unconditional branch of a guidance-60 weighted-SDS
    # gradient gives the negative term coefficient 5.9 against the positive
    # 6.0, which needs its own scaled perpendicular pilot rather than a flag.
    negative_prompt: str | None = None
    dream_steps: int = 300
    # None means "use learning_rate"; explicit values override per phase.
    sds_lr: float | None = None
    dream_lr: float | None = None
    learning_rate: float = 1e-3
    seed: int = 0
    device: str = field(default_factory=lambda: "cuda" if torch.cuda.is_available() else "cpu")
    strengths: list[float] = field(default_factory=list)
    # Empty by default so legacy runs are behaviorally equivalent to PR #118.
    checkpoint_steps: tuple[int, ...] = ()
    collect_diagnostics: bool = False
    style: str | None = None
    # Experiment harness only. Never change this default without acceptance.
    experimental_recipe: str = "legacy"
    sds_gradient_scale: float = 1.0
    # Native resolution the primes are rendered at. None means "the arrangement
    # resolution" for legacy and 256 for author_reference (the paper renders a
    # 256px network and upsamples for diffusion, paper Sec. 4.3). The prime is
    # the printable artifact, so this bounds print quality.
    prime_resolution: int | None = None

    def strength_schedule(self) -> list[float]:
        if self.strengths:
            return self.strengths
        # 0.95 down to 0.05 (the paper's 3.3.2 schedule walks 0.90 to 0.01;
        # ours is shifted by the SDEdit floor); rounds are configurable so
        # the smoke config can run in minutes
        return [
            0.9 * (1 - index / max(self.dream_rounds - 1, 1)) + 0.05
            for index in range(self.dream_rounds)
        ]


@dataclass(frozen=True)
class DreamArm:
    """One Dream phase to run from the shared SDS state.

    Only settings that act inside phase 2 belong here: anything earlier would
    make the arms of a base incomparable, which is the whole point of forking.
    """

    name: str
    dream_joint: bool = False
    negative_prompt: str | None = None


@dataclass
class IllusionResult:
    primes: list[Tensor]
    derived: list[Tensor]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Per-arm results when the Dream phase was forked; empty otherwise. The
    # top-level primes/derived are the first arm's, so single-arm callers see
    # exactly what they saw before forking existed.
    arm_results: dict[str, "IllusionResult"] = field(default_factory=dict)


def targets_for(config: IllusionConfig, spec: IllusionSpec) -> list[str | Tensor]:
    """Per-derived-image targets: prompts, with the optional image target
    taking the final (hidden) slot."""
    prompts = [
        apply_style_template(p, config.style) if isinstance(p, str) else p for p in config.prompts
    ]
    targets: list[str | Tensor] = list(prompts)
    if config.target_image is not None:
        targets.append(config.target_image)
    if len(targets) != len(spec.weights):
        raise ValueError(f"{config.illusion} needs {len(spec.weights)} targets, got {len(targets)}")
    return targets


def warn_low_clip_margins(margins: Sequence[float]) -> None:
    """Non-blocking warning when either view's CLIP margin is non-positive.

    Does not claim anatomy or aesthetic defect detection.
    """
    if any(m <= 0 for m in margins):
        warnings.warn(
            "low-confidence CLIP margins (non-positive for at least one view); "
            "diagnostic only - not an anatomy or aesthetic verdict",
            UserWarning,
            stacklevel=2,
        )


def optimize_illusion(
    config: IllusionConfig,
    progress: ProgressFn = lambda fraction: None,
    *,
    on_phase: PhaseFn | None = None,
    on_checkpoint: CheckpointFn | None = None,
    arms: Sequence[DreamArm] | None = None,
) -> IllusionResult:
    """Run both optimization phases and return primes and derived images.

    arms forks the Dream phase: SDS runs once, and each arm restores the
    SDS-end state before running its own Dream phase, so arms differ in exactly
    the settings under test. None keeps the single unforked Dream phase.
    """
    import time as _time

    if config.sds_objective not in SDS_OBJECTIVES:
        raise ValueError(f"unknown sds_objective {config.sds_objective!r}")
    if config.experimental_recipe not in ("legacy", "author_reference"):
        raise ValueError(f"unknown experimental_recipe {config.experimental_recipe!r}")
    reference_recipe = config.experimental_recipe == "author_reference"
    if reference_recipe and config.illusion != "flip":
        raise ValueError("author_reference currently supports only flip illusions")
    # Hidden: prefer view_batch_size=1 when the caller opts into microbatching.
    # Do not force it by default (keeps legacy path unchanged).
    view_batch = config.view_batch_size

    spec = ILLUSIONS[config.illusion]
    targets = targets_for(config, spec)
    torch.manual_seed(config.seed)
    generator = torch.Generator(device=config.device).manual_seed(config.seed)

    network_type = ReferenceFourierFeatureNetwork if reference_recipe else FourierFeatureNetwork
    networks = [network_type().to(config.device) for _ in range(spec.n_primes)]
    parameters = [p for network in networks for p in network.parameters()]
    sds_lr, dream_lr = resolve_learning_rates(config)

    optimizer = (
        torch.optim.SGD(parameters, lr=sds_lr)
        if reference_recipe
        else torch.optim.Adam(parameters, lr=sds_lr)
    )
    adapter = DiffusionAdapter(
        config.model_id,
        config.device,
        config.dream_model_id,
        enable_vae_slicing=config.enable_vae_slicing,
        channels_last=config.channels_last,
        view_batch_size=view_batch,
        sds_objective=config.sds_objective,
        sds_gradient_scale=config.sds_gradient_scale,
        vae_id=config.vae_id,
        model_variant=config.model_variant,
    )

    # None means "render at the arrangement resolution", which is the legacy
    # path. author_reference defaults to the paper's 256px network.
    prime_res = config.prime_resolution or (256 if reference_recipe else None)

    def render_primes(resolution: int = RESOLUTION) -> list[Tensor]:
        return [network.image(prime_res or resolution) for network in networks]

    def render_derived(resolution: int = RESOLUTION) -> list[Tensor]:
        derived = spec.arrange(render_primes(resolution))
        if prime_res is not None and prime_res != resolution:
            derived = [
                tf.interpolate(
                    image,
                    size=(resolution, resolution),
                    mode="bilinear",
                    align_corners=False,
                )
                for image in derived
            ]
        return derived

    def emit(event: PhaseEvent) -> None:
        if on_phase is not None:
            on_phase(event)
        if on_checkpoint is not None and event.primes is not None and event.derived is not None:
            on_checkpoint(
                event.phase,
                event.primes,
                event.derived,
                event.diagnostics if event.diagnostics is not None else {},
            )

    def snapshot_images() -> tuple[list[Tensor], list[Tensor]]:
        with torch.no_grad():
            primes_ck = [image.clamp(0, 1) for image in render_primes()]
            derived_ck = [image.clamp(0, 1) for image in render_derived()]
        return primes_ck, derived_ck

    sds_iterations = config.sds_steps
    if config.round_robin and config.illusion == "flip":
        sds_iterations = config.sds_steps * 2
    reference_views = (
        balanced_view_schedule(config.sds_steps, len(spec.weights), config.seed)
        if reference_recipe
        else None
    )

    arm_list = (
        [DreamArm(name="", dream_joint=config.dream_joint, negative_prompt=config.negative_prompt)]
        if arms is None
        else list(arms)
    )
    if not arm_list:
        raise ValueError("arms must not be empty")
    if len({arm.name for arm in arm_list}) != len(arm_list):
        raise ValueError("dream arm names must be unique")

    dream_rounds = len(config.strength_schedule())
    total = sds_iterations + len(arm_list) * dream_rounds * config.dream_steps
    done = 0
    low_res_steps = int(config.sds_steps * config.sds_low_res_fraction)
    diagnostics: dict[str, Any] = {
        "losses": [],
        "grad_norms": [],
        "conflict": [],
        "dream_rounds": [],
        "round_robin_exposures": [0, 0],
        "sds_objective": config.sds_objective,
        "sds_lr": sds_lr,
        "dream_lr": dream_lr,
        "experimental_recipe": config.experimental_recipe,
        "sds_gradient_scale": config.sds_gradient_scale,
        "effective_prompts": [t if isinstance(t, str) else "<image>" for t in targets],
    }
    checkpoint_set = set(config.checkpoint_steps)
    instrument = bool(
        config.collect_diagnostics or checkpoint_set or on_phase is not None or on_checkpoint
    )
    t_wall0 = _time.perf_counter()
    observe = on_phase is not None or on_checkpoint is not None

    def maybe_record_conflict(
        rendered: list[Tensor],
        prompt_images: list[Tensor],
        logical_step: int,
        progress_frac: float,
    ) -> None:
        if not config.collect_diagnostics:
            return
        if config.illusion != "flip" or config.round_robin:
            return
        if (logical_step + 1) not in checkpoint_set:
            return
        if len(prompt_images) < 2 or not all(isinstance(targets[i], str) for i in (0, 1)):
            return

        def _compute() -> dict[str, float]:
            view0 = rendered[0]
            _, c, lh, lw = latent_shape_for(view0)
            diag_noise = torch.randn(1, c, lh, lw, device=config.device, dtype=adapter.dtype)
            train_steps = adapter.scheduler.config.num_train_timesteps
            if config.use_hifa_schedule:
                frac = hifa_timestep_fraction(progress_frac)
                diag_t = max(0, min(train_steps - 1, int(frac * (train_steps - 1))))
            else:
                diag_t = int(0.5 * train_steps)
            view_grads: list[Tensor] = []
            for view_i in range(2):
                v_loss = adapter.sds_loss_batch(
                    rendered[view_i],
                    [str(targets[view_i])],
                    [spec.weights[view_i]],
                    config.sds_guidance,
                    generator,
                    objective=config.sds_objective,
                    progress=progress_frac,
                    use_hifa_schedule=config.use_hifa_schedule,
                    use_mean=True,
                    shared_timestep=diag_t,
                    shared_noise=diag_noise,
                )
                grads = torch.autograd.grad(
                    v_loss, parameters, retain_graph=True, allow_unused=True
                )
                pieces = [
                    (
                        g.detach().reshape(-1)
                        if g is not None
                        else torch.zeros(p.numel(), device=p.device)
                    )
                    for g, p in zip(grads, parameters, strict=True)
                ]
                view_grads.append(torch.cat(pieces) if pieces else torch.zeros(0))
            stats = gradient_conflict_stats(view_grads)
            stats["step"] = logical_step + 1
            return stats

        diagnostics["conflict"].append(preserve_rng_state(_compute))

    emit(PhaseEvent(phase="sds_begin", step=0, wall_s=0.0, diagnostics=diagnostics))

    for step in range(sds_iterations):
        logical_step = step // 2 if config.round_robin and config.illusion == "flip" else step
        resolution = config.sds_low_res if logical_step < low_res_steps else RESOLUTION
        optimizer.zero_grad()
        loss = torch.zeros((), device=config.device)
        rendered = render_derived(resolution)
        prompt_images: list[Tensor] = []
        prompt_texts: list[str] = []
        prompt_weights: list[float] = []
        for derived, target, weight in zip(rendered, targets, spec.weights, strict=True):
            if isinstance(target, str):
                prompt_images.append(derived)
                prompt_texts.append(target)
                prompt_weights.append(weight)
            else:
                target_image = target.to(config.device)
                if target_image.shape[-1] != resolution or target_image.shape[-2] != resolution:
                    target_image = tf.interpolate(
                        target_image,
                        size=(resolution, resolution),
                        mode="bilinear",
                        align_corners=False,
                    )
                loss = loss + weight * image_similarity_loss(derived, target_image)

        if reference_views is not None and prompt_images:
            active = reference_views[step]
            diagnostics["round_robin_exposures"][active] += 1
            prompt_images = [prompt_images[active]]
            prompt_texts = [prompt_texts[active]]
            prompt_weights = [prompt_weights[active]]
        elif config.round_robin and config.illusion == "flip" and prompt_images:
            active = step % 2
            diagnostics["round_robin_exposures"][active] += 1
            prompt_images = [prompt_images[active]]
            prompt_texts = [prompt_texts[active]]
            prompt_weights = [prompt_weights[active]]

        progress_frac = logical_step / max(config.sds_steps - 1, 1)
        use_micro = view_batch is not None and view_batch > 0 and len(prompt_images) > view_batch
        maybe_record_conflict(rendered, prompt_images, logical_step, progress_frac)

        if prompt_images and use_micro:
            assert view_batch is not None
            if float(loss.detach().item()) != 0.0 or loss.requires_grad:
                loss.backward(retain_graph=True)
            sds_loss_value = adapter.sds_microbatch_backward(
                torch.cat(prompt_images, dim=0),
                prompt_texts,
                prompt_weights,
                config.sds_guidance,
                generator,
                objective=config.sds_objective,
                progress=progress_frac,
                use_hifa_schedule=config.use_hifa_schedule,
                view_batch_size=view_batch,
            )
            loss_value = float(loss.detach().item()) + float(sds_loss_value)
        else:
            if prompt_images:
                loss = loss + adapter.sds_loss_batch(
                    torch.cat(prompt_images, dim=0),
                    prompt_texts,
                    prompt_weights,
                    config.sds_guidance,
                    generator,
                    objective=config.sds_objective,
                    progress=progress_frac,
                    use_hifa_schedule=config.use_hifa_schedule,
                )
            loss.backward()
            adapter.note_backward()
            loss_value = float(loss.detach().item())

        if instrument and (logical_step + 1) in checkpoint_set:
            diagnostics["grad_norms"].append(
                {"step": logical_step + 1, "norm": param_grad_norm(parameters)}
            )
            diagnostics["losses"].append(
                {"step": logical_step + 1, "phase": "sds", "loss": loss_value}
            )

        optimizer.step()
        done += 1
        progress(done / total)

        if (
            observe
            and (logical_step + 1) in checkpoint_set
            and (not config.round_robin or step % 2 == 1)
        ):
            primes_ck, derived_ck = snapshot_images()
            emit(
                PhaseEvent(
                    phase=checkpoint_name(logical_step + 1),
                    step=logical_step + 1,
                    primes=primes_ck,
                    derived=derived_ck,
                    loss=loss_value,
                    grad_norm=param_grad_norm(parameters) if instrument else None,
                    wall_s=_time.perf_counter() - t_wall0,
                    diagnostics=diagnostics,
                )
            )

    emit(
        PhaseEvent(
            phase="sds_end",
            step=config.sds_steps,
            wall_s=_time.perf_counter() - t_wall0,
            diagnostics=diagnostics,
        )
    )

    # The img2img swap is a one-time transition, not per-arm state: repeating it
    # would reload the Dream checkpoint and invalidate its embedding cache.
    adapter.begin_dream_phase()

    # Everything phase 1 mutates that a Dream arm reads. The prime networks ARE
    # the optimization state; the SDS optimizer is not snapshotted because the
    # phase boundary already discards it in favour of a fresh Adam.
    sds_weights = [
        {name: value.detach().clone() for name, value in network.state_dict().items()}
        for network in networks
    ]
    sds_cpu_rng = torch.get_rng_state()
    sds_cuda_rng = torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None
    sds_generator_rng = generator.get_state()

    dream_save_rounds = {1, 4, 8}

    def run_dream_arm(arm: DreamArm) -> IllusionResult:
        nonlocal done
        tag = None if arms is None else arm.name
        # Forked arms cannot share the round log; unforked runs keep the exact
        # dict they always returned.
        arm_diagnostics = (
            diagnostics
            if arms is None
            else {**diagnostics, "dream_rounds": [], "dream_arm": arm.name}
        )
        emit(
            PhaseEvent(
                phase="dream_begin",
                arm=tag,
                wall_s=_time.perf_counter() - t_wall0,
                diagnostics=arm_diagnostics,
            )
        )
        joint = (
            arm.dream_joint
            and config.illusion == "flip"
            and all(isinstance(target, str) for target in targets)
        )
        for round_index, strength in enumerate(config.strength_schedule(), start=1):
            dream_targets: list[Tensor] = []
            with torch.no_grad():
                current = render_derived(RESOLUTION)
            if joint:
                prompts = [target for target in targets if isinstance(target, str)]
                dream_targets = adapter.sdedit_joint(
                    current,
                    prompts,
                    strength,
                    generator,
                    negative_prompt=arm.negative_prompt,
                )
            else:
                for derived, target in zip(current, targets, strict=True):
                    if isinstance(target, str):
                        dream_targets.append(
                            adapter.sdedit(
                                derived,
                                target,
                                strength,
                                generator,
                                negative_prompt=arm.negative_prompt,
                            )
                        )
                    else:
                        dream_targets.append(target.to(config.device))

            loss_start_t = torch.zeros((), device=config.device)
            with torch.no_grad():
                for derived, dream, weight in zip(
                    render_derived(RESOLUTION), dream_targets, spec.weights, strict=True
                ):
                    loss_start_t = loss_start_t + weight * image_similarity_loss(derived, dream)
            loss_start = float(loss_start_t.item())
            loss_end = loss_start
            for _ in range(config.dream_steps):
                optimizer.zero_grad()
                loss = torch.zeros((), device=config.device)
                for derived, dream, weight in zip(
                    render_derived(RESOLUTION), dream_targets, spec.weights, strict=True
                ):
                    loss = loss + weight * image_similarity_loss(derived, dream)
                loss.backward()
                optimizer.step()
                loss_end = float(loss.detach().item())
                done += 1
                progress(done / total)

            arm_diagnostics["dream_rounds"].append(
                {
                    "round": round_index,
                    "strength": strength,
                    "loss_start": loss_start,
                    "loss_end": loss_end,
                    "loss_reduction": loss_start - loss_end,
                }
            )
            if observe and round_index in dream_save_rounds:
                primes_ck, derived_ck = snapshot_images()
                targets_det = [t.detach().clamp(0, 1) for t in dream_targets]
                emit(
                    PhaseEvent(
                        phase=f"dream_round_{round_index:02d}",
                        arm=tag,
                        round=round_index,
                        strength=strength,
                        primes=primes_ck,
                        derived=derived_ck,
                        targets=targets_det,
                        loss_start=loss_start,
                        loss_end=loss_end,
                        wall_s=_time.perf_counter() - t_wall0,
                        diagnostics=arm_diagnostics,
                    )
                )

        emit(
            PhaseEvent(
                phase="dream_end",
                arm=tag,
                wall_s=_time.perf_counter() - t_wall0,
                diagnostics=arm_diagnostics,
            )
        )

        with torch.no_grad():
            arm_primes = [image.clamp(0, 1) for image in render_primes()]
            arm_final = [image.clamp(0, 1) for image in render_derived()]
        emit(
            PhaseEvent(
                phase="final",
                arm=tag,
                primes=arm_primes,
                derived=arm_final,
                wall_s=_time.perf_counter() - t_wall0,
                diagnostics=arm_diagnostics,
            )
        )
        return IllusionResult(primes=arm_primes, derived=arm_final, diagnostics=arm_diagnostics)

    arm_results: dict[str, IllusionResult] = {}
    for arm in arm_list:
        for network, weights in zip(networks, sds_weights, strict=True):
            network.load_state_dict(weights)
        torch.set_rng_state(sds_cpu_rng)
        if sds_cuda_rng is not None:
            torch.cuda.set_rng_state_all(sds_cuda_rng)
        # One shared Dream RNG state for every arm: the arms of a base then
        # differ in exactly the setting under test, and the 36 bases supply the
        # independent draws that keep a result off one lucky sample.
        generator.set_state(sds_generator_rng)
        optimizer = torch.optim.Adam(parameters, lr=dream_lr)
        arm_results[arm.name] = run_dream_arm(arm)

    lead = arm_results[arm_list[0].name]
    return IllusionResult(
        primes=lead.primes,
        derived=lead.derived,
        diagnostics=lead.diagnostics,
        arm_results={} if arms is None else arm_results,
    )


# ------------------------------------------------------------------- cli


def save_image(tensor: Tensor, path: Path) -> None:
    from PIL import Image

    array = (tensor.squeeze(0).permute(1, 2, 0).cpu().numpy() * 255).clip(0, 255).astype("uint8")
    Image.fromarray(array).save(path)


def load_image(path: Path, resolution: int = RESOLUTION) -> Tensor:
    from PIL import Image

    with Image.open(path) as image:
        rgb = image.convert("RGB").resize((resolution, resolution), Image.Resampling.LANCZOS)
    data = torch.frombuffer(bytearray(rgb.tobytes()), dtype=torch.uint8).clone()
    pixels = data.reshape(resolution, resolution, 3).float() / 255.0
    return pixels.permute(2, 0, 1).unsqueeze(0)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=sorted(ILLUSIONS), required=True)
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        help="one per derived image, in arrangement order (semantic subject)",
    )
    parser.add_argument(
        "--style",
        choices=[*STYLE_TEMPLATES, "none"],
        default="none",
        help="optional style template wrapping each --prompt subject",
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
            "Pass 'none' to reuse --model with the classic 25-step CFG-7.5 schedule."
        ),
    )
    parser.add_argument("--sds-steps", type=int, default=500)
    parser.add_argument(
        "--sds-guidance",
        type=float,
        default=None,
        help="CFG scale for legacy/weighted_sds/nfsd (default 100). Rejected for csd.",
    )
    parser.add_argument(
        "--sds-objective",
        choices=SDS_OBJECTIVES,
        default="legacy",
        help="SDS residual formula: legacy | weighted_sds | csd | nfsd",
    )
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
    parser.add_argument(
        "--sqrt-timestep-anneal",
        action="store_true",
        help=(
            "square-root SDS timestep anneal "
            f"(t: {SQRT_ANNEAL_T_HIGH}->{SQRT_ANNEAL_T_LOW}, "
            f"exponent={SQRT_ANNEAL_EXPONENT}); not full HiFA; off by default"
        ),
    )
    parser.add_argument(
        "--hifa-schedule",
        action="store_true",
        help="deprecated alias for --sqrt-timestep-anneal",
    )
    parser.add_argument(
        "--round-robin",
        action="store_true",
        help="flip: alternate single-view SDS updates (equal exposure)",
    )
    parser.add_argument(
        "--view-batch-size",
        type=int,
        default=None,
        help="SDS microbatch size (default: full batch; hidden forces 1)",
    )
    parser.add_argument("--dream-rounds", type=int, default=8)
    parser.add_argument(
        "--dream-joint",
        action="store_true",
        help=(
            "flip only: build each round's Dream Targets jointly, reconciling "
            "both views into one image per step (issue #134)"
        ),
    )
    parser.add_argument("--dream-steps", type=int, default=300)
    parser.add_argument(
        "--negative-prompt",
        default=None,
        help="negative prompt for the Dream/SDEdit phase only; SDS is untouched",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=None,
        help="legacy alias: sets both --sds-lr and --dream-lr when those are absent",
    )
    parser.add_argument("--sds-lr", type=float, default=None)
    parser.add_argument("--dream-lr", type=float, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--vae-slicing", action="store_true", help="opt-in VAE slicing")
    parser.add_argument("--channels-last", action="store_true", help="opt-in channels_last layout")
    return parser


def config_from_args(args: argparse.Namespace) -> IllusionConfig:
    dream_model = None if args.dream_model.lower() == "none" else args.dream_model
    legacy_lr = 1e-3 if args.learning_rate is None else args.learning_rate
    # Explicit phase flags override; otherwise leave None so resolve_learning_rates
    # uses learning_rate. If --learning-rate alone is passed, set both via alias.
    sds_lr = args.sds_lr
    dream_lr = args.dream_lr
    if args.learning_rate is not None and sds_lr is None and dream_lr is None:
        sds_lr = dream_lr = args.learning_rate
    style = None if args.style == "none" else args.style
    if args.sds_objective == "csd":
        if args.sds_guidance is not None:
            raise SystemExit(
                "csd rejects --sds-guidance; canonical CSD is w(t)*(cond-uncond) "
                "(see CSD paper Eq. 7)"
            )
        sds_guidance = 1.0  # unused by compute_sds_gradient for csd
    else:
        sds_guidance = 100.0 if args.sds_guidance is None else float(args.sds_guidance)
    use_sqrt = bool(getattr(args, "sqrt_timestep_anneal", False) or args.hifa_schedule)
    return IllusionConfig(
        illusion=args.type,
        prompts=args.prompt,
        target_image=(load_image(args.target_image) if args.target_image else None),
        model_id=args.model,
        dream_model_id=dream_model,
        sds_steps=args.sds_steps,
        sds_guidance=sds_guidance,
        sds_objective=args.sds_objective,
        sds_low_res=args.sds_low_res,
        sds_low_res_fraction=args.sds_low_res_fraction,
        use_hifa_schedule=use_sqrt,
        round_robin=args.round_robin,
        view_batch_size=args.view_batch_size,
        enable_vae_slicing=args.vae_slicing,
        channels_last=args.channels_last,
        dream_rounds=args.dream_rounds,
        dream_joint=args.dream_joint,
        negative_prompt=args.negative_prompt,
        dream_steps=args.dream_steps,
        sds_lr=sds_lr,
        dream_lr=dream_lr,
        learning_rate=legacy_lr,
        seed=args.seed,
        device=args.device,
        style=style,
    )


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = config_from_args(args)
    try:
        result = optimize_illusion(config, progress=lambda f: print(f"\rprogress {f:6.1%}", end=""))
    except ValueError as error:  # e.g. wrong number of --prompt for --type
        raise SystemExit(f"error: {error}") from error
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
