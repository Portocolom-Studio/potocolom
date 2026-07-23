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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from worker.manifests import Manifest
from worker.memory_ladder import (
    MemoryMode,
    MemoryRung,
    REALTIME_BAR_MS,
    measured_wire_manifest,
    measured_wire_manifests,
    rung_vram_bytes,
    select_rung,
    slots_from_frame_ms,
)

logger = logging.getLogger("potocolom.worker")

ProgressFn = Callable[[float], None]

REALTIME_SIZE = 512  # the realtime bar is 512 px (docs/decisions.md)
# After a non-OOM generation error, drop the resident model at most this often
# so a permanently broken weight set cannot thrash load/unload (issue #103).
POISON_EVICT_COOLDOWN_S = 30.0
# Timing samples after the discarded warmup pass; nearest-rank p95 over
# these tolerates one outlier (19th of 20) instead of becoming the max.
CALIBRATION_SAMPLES = 20


@dataclass
class GeneratedImage:
    data: bytes  # always WebP; storage keys and asset rows assume it
    width: int
    height: int
    gpu_ms: int
    load_ms: int = 0


class Engine(Protocol):
    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
    ) -> GeneratedImage: ...

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes: ...

    def loaded_models(self) -> list[str]: ...

    def measured_manifests(self, manifests: list[Manifest]) -> list[dict]: ...

    def effective_realtime_slots(self, wire_manifests: list[dict], configured: int) -> int: ...

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int: ...

    async def load_model(self, manifest: Manifest) -> int: ...

    async def unload_model(self, model_id: str) -> None: ...

    async def unload_all(self) -> None: ...


def decode_input_image(data: bytes) -> Image.Image:
    """Decode a job's source image; a clear ValueError beats a Pillow OSError."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            return opened.convert("RGB")
    except OSError as error:
        raise ValueError("source image could not be decoded") from error


def encode_webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=80)
    return buffer.getvalue()


def _percentile_nearest(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def make_thumbnail_webp(data: bytes, max_edge: int = 384) -> bytes:
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

    def measured_manifests(self, manifests: list[Manifest]) -> list[dict]:
        return measured_wire_manifests(manifests, 1 << 60, "full", on_cpu=True)

    def effective_realtime_slots(self, wire_manifests: list[dict], configured: int) -> int:
        from worker.memory_ladder import effective_realtime_slots

        return effective_realtime_slots(wire_manifests, configured)

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        # Simulated engine has no GPU timings; keep the configured upper bound.
        return self.effective_realtime_slots(
            self.measured_manifests([manifest]), configured,
        )

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
        if "upscale" in manifest.capabilities:
            if input_image is None:
                raise ValueError("upscale job requires input_image")
            factor = int(params.get("factor", 0))
            if factor not in (2, 4):
                raise ValueError(f"unsupported upscale factor: {factor}")
            load_ms = 0
            if manifest.id not in self._loaded:
                load_start = time.monotonic()
                await asyncio.sleep(self.inference_seconds / 4)
                self._loaded = {manifest.id}
                load_ms = int((time.monotonic() - load_start) * 1000)
            source = decode_input_image(input_image)
            width, height = source.size[0] * factor, source.size[1] * factor
            start = time.monotonic()
            progress(0.5)
            image = source.resize((width, height), Image.Resampling.LANCZOS)
            progress(1.0)
            gpu_ms = int((time.monotonic() - start) * 1000)
            return GeneratedImage(encode_webp(image), width, height, gpu_ms, load_ms)
        if input_image is not None and "image_to_image" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support image_to_image jobs")
        load_ms = 0
        if manifest.id not in self._loaded:
            load_start = time.monotonic()
            await asyncio.sleep(self.inference_seconds / 4)
            self._loaded = {manifest.id}
            load_ms = int((time.monotonic() - load_start) * 1000)
        steps = 4
        start = time.monotonic()
        for step in range(steps):
            await asyncio.sleep(self.inference_seconds / steps)
            progress((step + 1) / steps)
        color = sha256(str(params.get("prompt", "")).encode()).digest()
        if input_image is not None:
            source = decode_input_image(input_image)
            width_param = params.get("width")
            height_param = params.get("height")
            if width_param and height_param:
                width, height = int(width_param), int(height_param)
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
        return GeneratedImage(encode_webp(image), width, height, gpu_ms, load_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        await asyncio.sleep(self.inference_seconds)
        return payload


class DiffusersEngine:
    """Hugging Face diffusers pipelines, one GPU, all inference serialized."""

    def __init__(self, device: str, *, memory_mode: MemoryMode = "auto",
                 models_dir: str = "", torch_compile: bool = False,
                 attention_backend: str = ""):
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
        self.memory_mode = memory_mode
        self.models_dir = models_dir
        self.torch_compile = torch_compile
        self.attention_backend = attention_backend
        self._pipelines: dict[tuple[str, str], Any] = {}
        self._rungs: dict[str, MemoryRung] = {}
        self._last_used: dict[str, float] = {}
        self._poison_evicted_at: dict[str, float] = {}
        self._poison_evict_count: dict[str, int] = {}
        self._calibrated_slots: int | None = None
        self._gpu = asyncio.Lock()

    def _free_vram_bytes(self) -> int:
        if self.device != "cuda":
            return 1 << 60
        free, _total = self.torch.cuda.mem_get_info()
        return int(free)

    def measured_manifests(self, manifests: list[Manifest]) -> list[dict]:
        # Loaded models keep their pinned rung: an offloaded pipeline leaves
        # VRAM looking free, and a reconnect must not re-advertise realtime
        # for a model that _frame would refuse.
        free = self._free_vram_bytes()
        on_cpu = self.device != "cuda"
        return [
            measured_wire_manifest(
                manifest,
                self._rungs.get(manifest.id)
                or select_rung(manifest.min_vram_gb, free, self.memory_mode,
                               on_cpu=on_cpu),
            )
            for manifest in manifests
        ]

    def effective_realtime_slots(self, wire_manifests: list[dict], configured: int) -> int:
        from worker.memory_ladder import effective_realtime_slots

        base = effective_realtime_slots(wire_manifests, configured)
        if base == 0:
            return 0
        if self._calibrated_slots is not None:
            return self._calibrated_slots
        return base

    def model_rung(self, model_id: str) -> MemoryRung | None:
        return self._rungs.get(model_id)

    def _touch(self, model_id: str) -> None:
        self._last_used[model_id] = time.monotonic()

    def _ensure_vram(self, manifest: Manifest) -> None:
        if self.memory_mode == "auto" and manifest.id not in self._rungs:
            # Prefer evicting cold residents over degrading the new model to
            # an offload rung: make room for full residency before picking.
            wanted = rung_vram_bytes(manifest.min_vram_gb, "full")
            if self._free_vram_bytes() < wanted:
                self._evict_cold(except_model_id=manifest.id, required_bytes=wanted)
        rung = self._pick_rung(manifest)
        required = rung_vram_bytes(manifest.min_vram_gb, rung)
        if self._free_vram_bytes() < required:
            self._evict_cold(except_model_id=manifest.id, required_bytes=required)

    def _pipeline(self, manifest: Manifest, mode: str) -> Any:
        key = (manifest.id, mode)
        if key not in self._pipelines:
            self._ensure_vram(manifest)
            try:
                self._pipelines[key] = self._load(manifest, mode)
            except self.torch.OutOfMemoryError:
                # Retry OUTSIDE this block: while the except frame is alive,
                # its traceback pins the half-moved weights of the failed
                # attempt and the eviction below could not reclaim them.
                pass
            if key not in self._pipelines:
                self._evict_cold(except_model_id=manifest.id)
                self._pipelines[key] = self._load(manifest, mode)
        self._touch(manifest.id)
        return self._pipelines[key]

    def _pick_rung(self, manifest: Manifest) -> MemoryRung:
        if manifest.id in self._rungs:
            return self._rungs[manifest.id]
        rung = self._select_rung(manifest)
        self._rungs[manifest.id] = rung
        return rung

    def _select_rung(self, manifest: Manifest) -> MemoryRung:
        # Upscalers are plain nn.Modules run tiled; the offload rungs are
        # diffusers pipeline mechanics, so they always load fully resident.
        if "upscale" in manifest.capabilities:
            return "full"
        return select_rung(
            manifest.min_vram_gb, self._free_vram_bytes(), self.memory_mode,
            on_cpu=self.device != "cuda",
        )

    def _apply_rung(self, pipeline: Any, manifest: Manifest, rung: MemoryRung) -> Any:
        if rung == "full":
            return pipeline.to(self.device)
        if rung == "model_offload":
            if self.device == "cuda":
                pipeline.enable_model_cpu_offload(gpu_id=0)
            else:
                pipeline.enable_model_cpu_offload()
            return pipeline
        offload_dir = None
        if self.models_dir:
            safe_id = "".join(c if c.isalnum() or c in "._-" else "-" for c in manifest.id)
            offload_dir = str(Path(self.models_dir) / ".offload" / safe_id.lstrip("."))
            Path(offload_dir).mkdir(parents=True, exist_ok=True)
        pipeline.enable_group_offload(
            onload_device=self.torch.device(self.device),
            # leaf_level streams layer by layer and needs no block sizing;
            # block_level raises when num_blocks_per_group is unset.
            offload_type="leaf_level",
            use_stream=True,
            offload_to_disk_path=offload_dir,
        )
        return pipeline

    def _denoise_modules(self, pipeline: Any) -> list[tuple[str, Any]]:
        modules: list[tuple[str, Any]] = []
        for name in ("unet", "transformer"):
            module = getattr(pipeline, name, None)
            if module is not None:
                modules.append((name, module))
        return modules

    def _set_attention_backend(self, pipeline: Any) -> list[Any]:
        """Apply the configured attention backend; return modules that took it."""
        backend = self.attention_backend.strip()
        if not backend:
            return []
        applied: list[Any] = []
        for name, module in self._denoise_modules(pipeline):
            setter = getattr(module, "set_attention_backend", None)
            if setter is None:
                continue
            try:
                setter(backend)
                applied.append(module)
                logger.info("attention backend %s on %s", backend, name)
            except Exception as error:
                logger.warning(
                    "set_attention_backend(%s) failed on %s: %s", backend, name, error,
                )
        return applied

    def _reset_attention_backend(self, module: Any) -> None:
        """Undo a set_attention_backend without raising out of the revert path."""
        try:
            reset = getattr(module, "reset_attention_backend", None)
            if reset is not None:
                reset()
                return
            setter = getattr(module, "set_attention_backend", None)
            if setter is not None:
                setter("native")
        except Exception as error:
            logger.warning("reset_attention_backend failed: %s", error)

    def _compile_module(self, pipeline: Any, name: str, module: Any) -> Any | None:
        """Compile one denoise module; return the original module on success.

        Always wraps with torch.compile (not in-place regional compile) so a
        failed warmup can restore the eager module.
        """
        try:
            # reduce-overhead enables CUDAGraphs, which overwrite intermediate
            # activations on this ROCm + Diffusers UNet path. default still
            # fuses kernels without graph capture.
            compiled = self.torch.compile(
                module, mode="default", fullgraph=False, dynamic=True,
            )
            setattr(pipeline, name, compiled)
            logger.info("torch.compile applied to %s", name)
            return module
        except Exception as error:
            logger.warning("torch.compile failed for %s: %s", name, error)
            return None

    def _optimize_resident(self, pipeline: Any, mode: str) -> None:
        """Attention backend + torch.compile for full-resident GPU pipelines.

        Offload rungs skip compile: accelerate hooks fight Inductor. Failures
        keep the uncompiled module so load still succeeds (ROCm Inductor is
        not guaranteed for every UNet shape). Warmup runs when either compile
        or an attention backend was applied, so a bad backend is not first
        discovered by a user frame.
        """
        if self.device != "cuda":
            return
        applied = self._set_attention_backend(pipeline)
        originals: list[tuple[str, Any]] = []
        if self.torch_compile:
            for name, module in self._denoise_modules(pipeline):
                original = self._compile_module(pipeline, name, module)
                if original is not None:
                    originals.append((name, original))
        if not originals and not applied:
            return
        try:
            self._warmup_pipeline(pipeline, mode)
        except Exception:
            for name, original in originals:
                setattr(pipeline, name, original)
            for module in applied:
                self._reset_attention_backend(module)
            logger.warning("reverted warmup optimizations after failure")
            return

    def _warmup_pipeline(self, pipeline: Any, mode: str) -> None:
        """One cheap forward so the first user job does not pay compile cost.

        Raises on failure so the caller can revert a broken compiled module.
        """
        if mode == "t2i":
            pipeline(
                prompt="",
                num_inference_steps=1,
                guidance_scale=0.0,
                width=REALTIME_SIZE,
                height=REALTIME_SIZE,
            )
            return
        if mode == "i2i":
            canvas = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (128, 128, 128))
            pipeline(
                prompt="",
                image=canvas,
                num_inference_steps=2,
                strength=0.5,
                guidance_scale=0.0,
            )
            return

    def _load(self, manifest: Manifest, mode: str) -> Any:
        if mode.startswith("upscale-"):
            return self._load_upscale(manifest, int(mode.split("-", 1)[1]))
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
            rung = self._pick_rung(manifest)
            pipeline = self._apply_rung(pipeline, manifest, rung)
            if rung == "full":
                self._optimize_resident(pipeline, mode)
        if manifest.scheduler:
            pipeline.scheduler = self._scheduler(manifest.scheduler, pipeline.scheduler.config)
        pipeline.set_progress_bar_config(disable=True)
        return pipeline

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        async with self._gpu:
            try:
                return await asyncio.to_thread(self._calibrate_realtime,
                                               manifest, configured)
            except Exception:
                # Could not measure; advertise nothing rather than a guess,
                # and never let a boot-time inference error kill the worker.
                logger.exception("realtime calibration failed for %s", manifest.id)
                self._calibrated_slots = 0
                return 0

    def _calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        """Measure single-frame p95 and advertise slots that still meet the bar.

        Multi-image batch calibration waits on deferred cross-session batching;
        until then N sessions share the GPU lock, so capacity is bar_ms / p95.
        """
        if configured <= 0 or "realtime" not in manifest.capabilities:
            self._calibrated_slots = 0
            return 0
        if self._select_rung(manifest) != "full":
            self._calibrated_slots = 0
            logger.info(
                "realtime calibration skipped for %s (not full-resident)", manifest.id,
            )
            return 0
        canvas = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (128, 128, 128))
        payload = encode_webp(canvas)
        params = {"prompt": "calibration", "strength": 0.7}
        samples: list[float] = []
        # One discarded pass absorbs remaining compile/warmup cost.
        for index in range(CALIBRATION_SAMPLES + 1):
            start = time.monotonic()
            self._frame(manifest, params, payload)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            if index > 0:
                samples.append(elapsed_ms)
        p95 = _percentile_nearest(samples, 95.0)
        slots = slots_from_frame_ms(p95, configured, bar_ms=REALTIME_BAR_MS)
        self._calibrated_slots = slots
        logger.info(
            "realtime calibration model=%s p95_ms=%.1f slots=%d (cap=%d)",
            manifest.id, p95, slots, configured,
        )
        return slots

    def _load_upscale(self, manifest: Manifest, factor: int) -> Any:
        from worker.upscale import ensure_weights, load_upscale_model

        source = manifest.source or (
            "https://github.com/xinntao/Real-ESRGAN/releases/download"
        )
        path = ensure_weights(source, self.models_dir, manifest.id, factor)
        return load_upscale_model(path, self.device, self.dtype)

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
        if name == "lcm":
            from diffusers import LCMScheduler

            # LCM-distilled adapters (VegaRT class) sample with the
            # consistency scheduler.
            return LCMScheduler.from_config(config)
        raise ValueError(f"unknown scheduler override: {name}")

    def loaded_models(self) -> list[str]:
        return sorted({key[0] for key in self._pipelines})

    def _free_gpu_cache(self) -> None:
        import gc

        gc.collect()
        if self.device == "cuda":
            self.torch.cuda.empty_cache()

    def _evict_cold(self, except_model_id: str, *, required_bytes: int = 0) -> None:
        loaded = sorted({key[0] for key in self._pipelines})
        candidates = [model_id for model_id in loaded if model_id != except_model_id]
        candidates.sort(key=lambda model_id: self._last_used.get(model_id, 0.0))
        for model_id in candidates:
            if required_bytes and self._free_vram_bytes() >= required_bytes:
                break
            self._evict_model(model_id)

    def _evict_except(self, model_id: str) -> None:
        self._evict_cold(except_model_id=model_id)

    def _evict_model(self, model_id: str) -> None:
        for key in [key for key in self._pipelines if key[0] == model_id]:
            del self._pipelines[key]
        self._rungs.pop(model_id, None)
        self._last_used.pop(model_id, None)
        self._free_gpu_cache()

    def _evict_poisoned(self, model_id: str) -> bool:
        """Drop a model left corrupt by a non-OOM generation error (issue #103).

        Returns True when an eviction ran. Cooldown prevents load/unload thrash
        when every subsequent job keeps failing the same way.
        """
        if not any(key[0] == model_id for key in self._pipelines):
            return False
        now = time.monotonic()
        last = self._poison_evicted_at.get(model_id, 0.0)
        if now - last < POISON_EVICT_COOLDOWN_S:
            logger.warning(
                "poisoned pipeline for %s; cooldown %.0fs active (evictions=%d), not reloading",
                model_id, POISON_EVICT_COOLDOWN_S, self._poison_evict_count.get(model_id, 0),
            )
            return False
        self._evict_model(model_id)
        self._poison_evicted_at[model_id] = now
        count = self._poison_evict_count.get(model_id, 0) + 1
        self._poison_evict_count[model_id] = count
        logger.warning("evicted poisoned pipeline for %s (count=%d)", model_id, count)
        return True

    def _evict_all(self) -> None:
        self._pipelines.clear()
        self._rungs.clear()
        self._last_used.clear()
        self._free_gpu_cache()

    async def load_model(self, manifest: Manifest) -> int:
        async with self._gpu:
            return await asyncio.to_thread(self._load_model, manifest)

    def _load_model(self, manifest: Manifest) -> int:
        self._evict_all()
        self._rungs[manifest.id] = self._select_rung(manifest)
        start = time.monotonic()
        if "upscale" in manifest.capabilities:
            factor_spec = manifest.parameters.get("properties", {}).get("factor", {})
            mode = f"upscale-{int(factor_spec.get('default', 2))}"
            try:
                self._pipelines[(manifest.id, mode)] = self._load(manifest, mode)
            except self.torch.OutOfMemoryError:
                self._evict_all()
                self._pipelines[(manifest.id, mode)] = self._load(manifest, mode)
        else:
            try:
                self._pipelines[(manifest.id, "t2i")] = self._load(manifest, "t2i")
            except self.torch.OutOfMemoryError:
                self._evict_all()
                self._pipelines[(manifest.id, "t2i")] = self._load(manifest, "t2i")
        self._touch(manifest.id)
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
        if "upscale" in manifest.capabilities:
            if input_image is None:
                raise ValueError("upscale job requires input_image")
            runner = self._generate_upscale
        elif input_image is not None:
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
            except (ValueError, TypeError):
                raise  # request/validation errors, not a corrupt resident
            except Exception:
                # Dtype mismatches and similar leave mixed-precision state in
                # the resident pipeline; drop it so the next job reloads clean.
                self._evict_poisoned(manifest.id)
                raise
            # Two resident models plus activations can exceed a 16 GB card
            # mid-denoise; free the others and run once more.
            self._evict_except(manifest.id)
            return await asyncio.to_thread(runner, manifest, dict(params),
                                           progress, loop, input_image)

    def _generate(self, manifest: Manifest, params: dict, progress: ProgressFn,
                  loop: asyncio.AbstractEventLoop,
                  input_image: bytes | None = None) -> GeneratedImage:
        load_start = time.monotonic()
        key = (manifest.id, "t2i")
        cold = key not in self._pipelines
        pipeline = self._pipeline(manifest, "t2i")
        load_ms = int((time.monotonic() - load_start) * 1000) if cold else 0
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
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms, load_ms)

    def _generate_i2i(self, manifest: Manifest, params: dict, progress: ProgressFn,
                      loop: asyncio.AbstractEventLoop,
                      input_image: bytes | None) -> GeneratedImage:
        if input_image is None:
            raise ValueError("image_to_image job requires input_image")
        load_start = time.monotonic()
        key = (manifest.id, "i2i")
        cold = key not in self._pipelines
        pipeline = self._pipeline(manifest, "i2i")
        load_ms = int((time.monotonic() - load_start) * 1000) if cold else 0
        source = decode_input_image(input_image)
        width = params.get("width")
        height = params.get("height")
        if width and height:
            source = source.resize((int(width), int(height)), Image.Resampling.LANCZOS)
        steps = max(1, int(params.get("steps", 2)))
        strength = min(max(float(params.get("strength", 0.75)), 0.05), 1.0)
        # diffusers img2img floors the step count: int(steps * strength).
        actual_steps = max(1, int(steps * strength))
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
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms, load_ms)

    def _generate_upscale(self, manifest: Manifest, params: dict, progress: ProgressFn,
                          loop: asyncio.AbstractEventLoop,
                          input_image: bytes | None) -> GeneratedImage:
        if input_image is None:
            raise ValueError("upscale job requires input_image")
        factor = int(params.get("factor", 0))
        if factor not in (2, 4):
            raise ValueError(f"unsupported upscale factor: {factor}")
        from worker.upscale import UpscaleRuntime, upscale_tiled

        load_start = time.monotonic()
        mode = f"upscale-{factor}"
        key = (manifest.id, mode)
        cold = key not in self._pipelines
        runtime = self._pipeline(manifest, mode)
        if not isinstance(runtime, UpscaleRuntime):
            raise TypeError(f"upscale pipeline for {manifest.id} is not an UpscaleRuntime")
        load_ms = int((time.monotonic() - load_start) * 1000) if cold else 0
        source = decode_input_image(input_image)

        def on_tile(fraction: float) -> None:
            loop.call_soon_threadsafe(progress, fraction)

        start = time.monotonic()
        image = upscale_tiled(
            runtime.model, source, factor,
            device=self.device, dtype=self.dtype,
            native_scale=runtime.native_scale, progress=on_tile,
        )
        gpu_ms = int((time.monotonic() - start) * 1000)
        loop.call_soon_threadsafe(progress, 1.0)
        return GeneratedImage(encode_webp(image), image.width, image.height, gpu_ms, load_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        async with self._gpu:
            try:
                return await asyncio.to_thread(self._frame, manifest, dict(params), payload)
            except self.torch.OutOfMemoryError:
                pass  # retry outside: the live traceback pins failed tensors
            except (ValueError, TypeError):
                raise
            except Exception:
                self._evict_poisoned(manifest.id)
                raise
            self._evict_except(manifest.id)
            return await asyncio.to_thread(self._frame, manifest, dict(params), payload)

    def _frame(self, manifest: Manifest, params: dict, payload: bytes) -> bytes:
        if "realtime" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support realtime frames")
        if self._pick_rung(manifest) != "full":
            raise ValueError(f"model {manifest.id} is not fully resident for realtime")
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
