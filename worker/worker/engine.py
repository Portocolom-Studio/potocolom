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
        self, manifest: Manifest, params: dict, progress: ProgressFn
    ) -> GeneratedImage: ...

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes: ...


def encode_webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=80)
    return buffer.getvalue()


class SimulatedEngine:
    """Sleeps instead of denoising; frames echo back, jobs produce a flat
    image colored from the prompt so results are distinguishable."""

    def __init__(self, inference_seconds: float):
        self.inference_seconds = inference_seconds

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn
    ) -> GeneratedImage:
        steps = 4
        start = time.monotonic()
        for step in range(steps):
            await asyncio.sleep(self.inference_seconds / steps)
            progress((step + 1) / steps)
        color = sha256(str(params.get("prompt", "")).encode()).digest()
        image = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE),
                          (color[0], color[1], color[2]))
        gpu_ms = int((time.monotonic() - start) * 1000)
        return GeneratedImage(encode_webp(image), REALTIME_SIZE, REALTIME_SIZE, gpu_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        await asyncio.sleep(self.inference_seconds)
        return payload


class DiffusersEngine:
    """Hugging Face diffusers pipelines, one GPU, all inference serialized."""

    def __init__(self, device: str):
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
            from diffusers import AutoPipelineForImage2Image, AutoPipelineForText2Image

            cls = AutoPipelineForText2Image if mode == "t2i" else AutoPipelineForImage2Image
            loaded = self._pipelines.get((manifest.id, "i2i" if mode == "t2i" else "t2i"))
            if loaded is not None:
                pipeline = cls.from_pipe(loaded)  # shares weights already on the device
            else:
                pipeline = self._from_pretrained(cls, manifest.source or manifest.id)
                pipeline = pipeline.to(self.device)
            pipeline.set_progress_bar_config(disable=True)
            self._pipelines[key] = pipeline
        return self._pipelines[key]

    def _from_pretrained(self, cls: Any, source: str) -> Any:
        if self.dtype is self.torch.float16:
            try:
                # fp16 variants halve the download and the disk footprint;
                # not every repository ships one.
                return cls.from_pretrained(source, torch_dtype=self.dtype, variant="fp16")
            except Exception:
                pass
        return cls.from_pretrained(source, torch_dtype=self.dtype)

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn
    ) -> GeneratedImage:
        if "text_to_image" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support text_to_image jobs")
        loop = asyncio.get_running_loop()
        async with self._gpu:
            return await asyncio.to_thread(self._generate, manifest, dict(params), progress, loop)

    def _generate(self, manifest: Manifest, params: dict, progress: ProgressFn,
                  loop: asyncio.AbstractEventLoop) -> GeneratedImage:
        pipeline = self._pipeline(manifest, "t2i")
        steps = int(params.get("steps", 2))
        generator = None
        if params.get("seed") is not None:
            generator = self.torch.Generator(self.device).manual_seed(int(params["seed"]))

        def on_step(pipe: Any, step: int, timestep: Any, kwargs: dict) -> dict:
            loop.call_soon_threadsafe(progress, (step + 1) / steps)
            return kwargs

        start = time.monotonic()
        image = pipeline(
            prompt=str(params.get("prompt", "")),
            num_inference_steps=steps,
            guidance_scale=float(params.get("guidance", 0.0)),
            width=int(params.get("width", REALTIME_SIZE)),
            height=int(params.get("height", REALTIME_SIZE)),
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        async with self._gpu:
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
