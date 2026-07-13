"""Inference engines behind one interface (issue #15).

SimulatedEngine keeps the wire protocol runnable anywhere; DiffusersEngine is
the real thing and imports torch lazily, so the package installs and imports
without the inference extra.

Engines call the progress callback on the event loop, never from the
inference thread directly.
"""

import asyncio
import io
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from PIL import Image

from worker.manifests import Manifest

ProgressFn = Callable[[float], None]

REALTIME_SIZE = 512  # the realtime bar is 512 px (docs/decisions.md)


@dataclass
class GeneratedImage:
    data: bytes  # always WebP; storage keys and asset rows assume it
    width: int
    height: int
    gpu_ms: int


class Engine(Protocol):
    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
    ) -> GeneratedImage: ...

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes: ...

    def loaded_models(self) -> list[str]: ...

    async def load_model(self, manifest: Manifest) -> int: ...

    async def unload_model(self, model_id: str) -> None: ...

    async def unload_all(self) -> None: ...


def encode_webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=80)
    return buffer.getvalue()


def make_thumbnail_webp(data: bytes, max_edge: int = 320) -> bytes:
    with Image.open(io.BytesIO(data)) as opened:
        rgb = opened if opened.mode == "RGB" else opened.convert("RGB")
        rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        return encode_webp(rgb)


class SimulatedEngine:
    """Sleeps instead of denoising; frames echo back, jobs produce a flat
    image colored from the prompt so results are distinguishable."""

    def __init__(self, inference_seconds: float):
        self.inference_seconds = inference_seconds
        self._loaded: set[str] = set()

    def loaded_models(self) -> list[str]:
        return sorted(self._loaded)

    async def load_model(self, manifest: Manifest) -> int:
        start = time.monotonic()
        await asyncio.sleep(self.inference_seconds / 4)
        self._loaded = {manifest.id}
        return int((time.monotonic() - start) * 1000)

    async def unload_model(self, model_id: str) -> None:
        self._loaded.discard(model_id)

    async def unload_all(self) -> None:
        self._loaded.clear()

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
    ) -> GeneratedImage:
        if input_image is not None and "image_to_image" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support image_to_image jobs")
        steps = 4
        start = time.monotonic()
        for step in range(steps):
            await asyncio.sleep(self.inference_seconds / steps)
            progress((step + 1) / steps)
        color = sha256(str(params.get("prompt", "")).encode()).digest()
        if input_image is not None:
            source = Image.open(io.BytesIO(input_image)).convert("RGB")
            width = params.get("width")
            height = params.get("height")
            if width and height:
                width, height = int(width), int(height)
                source = source.resize((width, height), Image.Resampling.LANCZOS)
            else:
                width, height = source.size
            color = sha256((str(params.get("prompt", "")) + ":i2i").encode()).digest()
            rgb = (color[0], color[1], color[2])
            image = Image.new("RGB", (width, height), rgb)
        else:
            width = height = REALTIME_SIZE
            rgb = (color[0], color[1], color[2])
            image = Image.new("RGB", (width, height), rgb)
        gpu_ms = int((time.monotonic() - start) * 1000)
        return GeneratedImage(encode_webp(image), width, height, gpu_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        await asyncio.sleep(self.inference_seconds)
        return payload


class DiffusersEngine:
    """Hugging Face diffusers pipelines, one GPU, all inference serialized."""

    def __init__(self, device: str):
        if device == "rocm":
            # RDNA3 consumer cards gate their fused attention kernels behind
            # this flag; the fallback is math attention, several times slower.
            # Read at first SDPA dispatch, so it must precede any inference.
            os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
        import torch

        self.torch = torch
        # ROCm builds of torch expose the cuda API; DEVICE=rocm differs only
        # in which image variant and driver stack surrounds this process.
        self.device = "cuda" if device in ("cuda", "rocm") else "cpu"
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self._pipelines: dict[tuple[str, str], Any] = {}
        self._gpu = asyncio.Lock()

    def _pipeline(self, manifest: Manifest, mode: str) -> Any:
        key = (manifest.id, mode)
        if key not in self._pipelines:
            try:
                self._pipelines[key] = self._load(manifest, mode)
            except self.torch.OutOfMemoryError:
                # Retry OUTSIDE this block: while the except frame is alive,
                # its traceback pins the half-moved weights of the failed
                # attempt and the eviction below could not reclaim them.
                pass
            if key not in self._pipelines:
                # A 16 GB card fits roughly one XL model plus headroom: evict
                # every other model and retry once. A real eviction policy
                # (LRU with a min-warm TTL) lands with issue #15.
                self._evict_except(manifest.id)
                self._pipelines[key] = self._load(manifest, mode)
        return self._pipelines[key]

    def _load(self, manifest: Manifest, mode: str) -> Any:
        from diffusers import AutoPipelineForImage2Image, AutoPipelineForText2Image

        cls = AutoPipelineForText2Image if mode == "t2i" else AutoPipelineForImage2Image
        loaded = self._pipelines.get((manifest.id, "i2i" if mode == "t2i" else "t2i"))
        if loaded is not None:
            pipeline = cls.from_pipe(loaded)  # shares weights already on the device
        else:
            pipeline = self._from_pretrained(cls, manifest)
            if manifest.lora:
                # "org/repo/file.safetensors": a distillation LoRA (Lightning,
                # Hyper-SD class) fused into the weights while still on the
                # CPU, so the device move carries the final tensors.
                repo, _, weight = manifest.lora.rpartition("/")
                pipeline.load_lora_weights(repo, weight_name=weight)
                pipeline.fuse_lora()
            if self.device == "cuda":
                # Conv-heavy UNets run measurably faster in NHWC; converted
                # while still on the CPU so the move needs no VRAM transient.
                for name in ("unet", "vae"):
                    module = getattr(pipeline, name, None)
                    if module is not None:
                        module.to(memory_format=self.torch.channels_last)
            pipeline = pipeline.to(self.device)
        if manifest.scheduler:
            pipeline.scheduler = self._scheduler(manifest.scheduler, pipeline.scheduler.config)
        pipeline.set_progress_bar_config(disable=True)
        return pipeline

    def _scheduler(self, name: str, config: Any) -> Any:
        if name == "dpmsolver":
            from diffusers import DPMSolverMultistepScheduler

            # DPM++ 2M Karras, the robust workhorse for SDXL class quality;
            # the stock Euler config trips a sigma indexing bug at some step
            # counts (25 fails, 20 passes) in diffusers 0.39.
            return DPMSolverMultistepScheduler.from_config(
                config, algorithm_type="dpmsolver++", use_karras_sigmas=True
            )
        if name == "euler-trailing":
            from diffusers import EulerDiscreteScheduler

            # The documented recipe for Lightning class distillation LoRAs.
            return EulerDiscreteScheduler.from_config(config, timestep_spacing="trailing")
        raise ValueError(f"unknown scheduler override: {name}")

    def loaded_models(self) -> list[str]:
        return sorted({key[0] for key in self._pipelines})

    def _free_gpu_cache(self) -> None:
        import gc

        gc.collect()
        if self.device == "cuda":
            self.torch.cuda.empty_cache()

    def _evict_except(self, model_id: str) -> None:
        evicted = [key for key in self._pipelines if key[0] != model_id]
        for key in evicted:
            del self._pipelines[key]
        self._free_gpu_cache()

    def _evict_model(self, model_id: str) -> None:
        for key in [key for key in self._pipelines if key[0] == model_id]:
            del self._pipelines[key]
        self._free_gpu_cache()

    def _evict_all(self) -> None:
        self._pipelines.clear()
        self._free_gpu_cache()

    async def load_model(self, manifest: Manifest) -> int:
        async with self._gpu:
            return await asyncio.to_thread(self._load_model, manifest)

    def _load_model(self, manifest: Manifest) -> int:
        self._evict_all()
        start = time.monotonic()
        try:
            self._pipelines[(manifest.id, "t2i")] = self._load(manifest, "t2i")
        except self.torch.OutOfMemoryError:
            self._evict_all()
            self._pipelines[(manifest.id, "t2i")] = self._load(manifest, "t2i")
        return int((time.monotonic() - start) * 1000)

    async def unload_model(self, model_id: str) -> None:
        async with self._gpu:
            await asyncio.to_thread(self._evict_model, model_id)

    async def unload_all(self) -> None:
        async with self._gpu:
            await asyncio.to_thread(self._evict_all)

    def _from_pretrained(self, cls: Any, manifest: Manifest) -> Any:
        source = manifest.source or manifest.id
        kwargs: dict[str, Any] = {"torch_dtype": self.dtype}
        if manifest.vae:
            from diffusers import AutoencoderKL

            # SDXL's stock VAE upcasts itself to fp32 at decode time (fp16
            # overflows), which spikes VRAM past a 16 GB card; manifests name
            # an fp16-safe replacement instead.
            kwargs["vae"] = AutoencoderKL.from_pretrained(manifest.vae, torch_dtype=self.dtype)
        if self.dtype is self.torch.float16:
            try:
                # fp16 variants halve the download and the disk footprint;
                # not every repository ships one.
                return cls.from_pretrained(source, variant="fp16", **kwargs)
            except Exception:
                pass
        return cls.from_pretrained(source, **kwargs)

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
    ) -> GeneratedImage:
        if input_image is not None:
            if "image_to_image" not in manifest.capabilities:
                raise ValueError(f"model {manifest.id} does not support image_to_image jobs")
            runner = self._generate_i2i
        else:
            if "text_to_image" not in manifest.capabilities:
                raise ValueError(f"model {manifest.id} does not support text_to_image jobs")
            runner = self._generate
        loop = asyncio.get_running_loop()
        async with self._gpu:
            try:
                return await asyncio.to_thread(runner, manifest, dict(params),
                                               progress, loop, input_image)
            except self.torch.OutOfMemoryError:
                pass  # retry outside: the live traceback pins failed tensors
            # Two resident models plus activations can exceed a 16 GB card
            # mid-denoise; free the others and run once more.
            self._evict_except(manifest.id)
            return await asyncio.to_thread(runner, manifest, dict(params),
                                           progress, loop, input_image)

    def _generate(self, manifest: Manifest, params: dict, progress: ProgressFn,
                  loop: asyncio.AbstractEventLoop,
                  input_image: bytes | None = None) -> GeneratedImage:
        pipeline = self._pipeline(manifest, "t2i")
        steps = max(1, int(params.get("steps", 2)))
        generator = None
        if params.get("seed") is not None:
            generator = self.torch.Generator(self.device).manual_seed(int(params["seed"]))

        def on_step(pipe: Any, step: int, timestep: Any, kwargs: dict) -> dict:
            loop.call_soon_threadsafe(progress, (step + 1) / steps)
            return kwargs

        # Absent dimensions fall through as None: the pipeline renders at the
        # model's native size (512 for SD class, 1024 for SDXL base class).
        width = params.get("width")
        height = params.get("height")
        start = time.monotonic()
        image = pipeline(
            prompt=str(params.get("prompt", "")),
            num_inference_steps=steps,
            guidance_scale=float(params.get("guidance", 0.0)),
            width=int(width) if width else None,
            height=int(height) if height else None,
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms)

    def _generate_i2i(self, manifest: Manifest, params: dict, progress: ProgressFn,
                      loop: asyncio.AbstractEventLoop,
                      input_image: bytes | None) -> GeneratedImage:
        if input_image is None:
            raise ValueError("image_to_image job requires input_image")
        pipeline = self._pipeline(manifest, "i2i")
        source = Image.open(io.BytesIO(input_image)).convert("RGB")
        width = params.get("width")
        height = params.get("height")
        if width and height:
            source = source.resize((int(width), int(height)), Image.Resampling.LANCZOS)
        steps = max(1, int(params.get("steps", 2)))
        strength = min(max(float(params.get("strength", 0.75)), 0.05), 1.0)
        actual_steps = max(1, math.ceil(steps * strength))
        generator = None
        if params.get("seed") is not None:
            generator = self.torch.Generator(self.device).manual_seed(int(params["seed"]))

        def on_step(pipe: Any, step: int, timestep: Any, kwargs: dict) -> dict:
            loop.call_soon_threadsafe(progress, (step + 1) / actual_steps)
            return kwargs

        start = time.monotonic()
        image = pipeline(
            prompt=str(params.get("prompt", "")),
            image=source,
            num_inference_steps=steps,
            strength=strength,
            guidance_scale=float(params.get("guidance", 0.0)),
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        loop.call_soon_threadsafe(progress, 1.0)
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        async with self._gpu:
            try:
                return await asyncio.to_thread(self._frame, manifest, dict(params), payload)
            except self.torch.OutOfMemoryError:
                pass  # retry outside: the live traceback pins failed tensors
            self._evict_except(manifest.id)
            return await asyncio.to_thread(self._frame, manifest, dict(params), payload)

    def _frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        canvas = Image.open(io.BytesIO(payload)).convert("RGB")
        canvas = canvas.resize((REALTIME_SIZE, REALTIME_SIZE))
        strength = min(max(float(params.get("strength", 0.7)), 0.05), 1.0)
        pipeline = self._pipeline(manifest, "i2i")
        image = pipeline(
            prompt=str(params.get("prompt", "")),
            image=canvas,
            # Few-step img2img: diffusers runs ceil(steps * strength) steps,
            # so keep the product at one or above.
            num_inference_steps=max(2, math.ceil(1 / strength)),
            strength=strength,
            guidance_scale=0.0,
        ).images[0]
        return encode_webp(image)
