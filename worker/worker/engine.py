"""Inference engines behind one interface (issue #15).

SimulatedEngine keeps the wire protocol runnable anywhere; DiffusersEngine is
the real thing and imports torch lazily, so the package installs and imports
without the inference extra.

Engines call the progress callback on the event loop, never from the
inference thread directly.
"""

import asyncio
import contextlib
import io
import math
import os
import time
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, TypeVar

from PIL import Image, ImageDraw, ImageOps

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
T = TypeVar("T")

REALTIME_SIZE = 512  # the realtime bar is 512 px (docs/decisions.md)
# After a non-OOM generation error, drop the resident model at most this often
# so a permanently broken weight set cannot thrash load/unload (issue #103).
POISON_EVICT_COOLDOWN_S = 30.0
# Timing samples after the discarded warmup pass; nearest-rank p95 over
# these tolerates one outlier (19th of 20) instead of becoming the max.
CALIBRATION_SAMPLES = 20
# Live frame observations supersede the calibration estimate once a session
# has produced enough to mean anything: a handful of frames is not a
# distribution, and the first frames of a session are the least
# representative ones. The window is bounded so a slow early session cannot
# keep dragging a model's advertised p95 after it speeds up.
OBSERVED_FRAME_SAMPLES = 20
OBSERVED_FRAME_WINDOW = 120
# Pillow work was implicitly bounded to one operation by the GPU lock before
# this change moved it outside that lock. Use half the logical CPUs so codec
# work leaves room in the shared default executor, with a floor of one for
# small hosts and a ceiling of four to limit CPU and memory pressure.
CODEC_CONCURRENCY_LIMIT = max(1, min(4, (os.cpu_count() or 1) // 2))


@dataclass
class GeneratedImage:
    data: bytes  # PNG generation master
    width: int
    height: int
    gpu_ms: int
    load_ms: int = 0


@dataclass
class GeneratedFrame:
    data: bytes
    gpu_ms: int


class Engine(Protocol):
    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
    ) -> GeneratedImage: ...

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> GeneratedFrame: ...

    async def prepare_realtime(self, manifest: Manifest) -> bool: ...

    def loaded_models(self) -> list[str]: ...

    def measured_manifests(self, manifests: list[Manifest]) -> list[dict]: ...

    def effective_realtime_slots(self, wire_manifests: list[dict], configured: int) -> int: ...

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int: ...

    def observe_frame_ms(self, model_id: str, gpu_ms: float) -> None: ...

    def realtime_p95_ms(self, model_id: str) -> int | None: ...

    def p95_model_ids(self) -> list[str]: ...

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


def _canvas_to_sketch_map(canvas: Image.Image, threshold: int = 128) -> Image.Image:
    """Canvas (dark strokes on white) to the adapter's conditioning-map
    convention (light strokes on black), with no learned preprocessor. The
    threshold variant (the prototype's stream default) binarizes at the
    midpoint, which kills WebP ring halos while the antialiased stroke cores
    stay (scripts/prototype-canvas-conditioning.py)."""
    gray = canvas.convert("L")
    inverted = ImageOps.invert(gray)
    sketch = inverted.point(lambda value: 255 if value >= threshold else 0)
    return sketch.convert("RGB")


def _sparse_sketch_map(size: int = REALTIME_SIZE) -> Image.Image:
    """A representative sparse sketch map (light strokes on black) for
    realtime calibration. Flat gray is wrong here: a uniform map gives the
    T2I-Adapter nothing to condition on and would time an unrealistically
    easy frame, sizing realtime_slots against a workload no session runs."""
    sketch = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(sketch)
    draw.line((0, 0, size, size), fill=(255, 255, 255), width=8)
    draw.line((size, 0, 0, size), fill=(255, 255, 255), width=8)
    return sketch


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
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

    def observe_frame_ms(self, model_id: str, gpu_ms: float) -> None:
        # Simulated engine has no meaningful timings to learn from; a no-op
        # keeps its realtime_p95_ms advertising nothing forever.
        return None

    def realtime_p95_ms(self, model_id: str) -> int | None:
        # Simulated engine never measured a frame; nothing to advertise.
        return None

    def p95_model_ids(self) -> list[str]:
        # Simulated engine never measured a frame; nothing to advertise.
        return []

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
            return GeneratedImage(encode_png(image), width, height, gpu_ms, load_ms)
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
        return GeneratedImage(encode_png(image), width, height, gpu_ms, load_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> GeneratedFrame:
        started = time.monotonic()
        await asyncio.sleep(self.inference_seconds)
        return GeneratedFrame(payload, int((time.monotonic() - started) * 1000))

    async def prepare_realtime(self, manifest: Manifest) -> bool:
        # Nothing to load: the simulated engine has no residency, so every
        # session it opens can be served.
        return True


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
        self._realtime_p95_ms: dict[str, int] = {}
        self._observed_frame_ms: dict[str, deque[float]] = {}
        self._gpu = asyncio.Lock()
        self._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)

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

    def _pipeline(
        self, manifest: Manifest, mode: str, *, allow_demotion: bool = True,
    ) -> Any:
        key = (manifest.id, mode)
        while key not in self._pipelines:
            self._ensure_vram(manifest)
            try:
                self._pipelines[key] = self._load(manifest, mode)
            except self.torch.OutOfMemoryError as error:
                # Retry OUTSIDE this block: while the except frame is alive,
                # its traceback pins the half-moved weights of the failed
                # attempt and the eviction below could not reclaim them.
                load_error = error.with_traceback(None)
            if key in self._pipelines:
                break
            if allow_demotion and self._demote_rung(manifest, phase="load"):
                continue
            if not allow_demotion:
                raise load_error
            self._evict_cold(except_model_id=manifest.id)
            try:
                self._pipelines[key] = self._load(manifest, mode)
            except self.torch.OutOfMemoryError as error:
                load_error = error.with_traceback(None)
            if key not in self._pipelines:
                raise load_error
        self._touch(manifest.id)
        return self._pipelines[key]

    def _pick_rung(self, manifest: Manifest) -> MemoryRung:
        if manifest.id in self._rungs:
            return self._rungs[manifest.id]
        rung = self._select_rung(manifest)
        self._rungs[manifest.id] = rung
        return rung

    def _demote_rung(self, manifest: Manifest, *, phase: str) -> bool:
        if self.memory_mode != "auto":
            return False
        rung = self._pick_rung(manifest)
        lower: dict[MemoryRung, MemoryRung] = {
            "full": "model_offload",
            "model_offload": "group_offload",
        }
        demoted = lower.get(rung)
        if demoted is None:
            return False
        self._evict_model(manifest.id)
        self._rungs[manifest.id] = demoted
        logger.error(
            "out of memory during %s for %s; demoting from %s to %s",
            phase, manifest.id, rung, demoted,
        )
        return True

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
        use_group_offload_fast_path = not manifest.quantize
        offload_dir = None
        # Quantized torchao subclass tensors cannot be serialized by safetensors
        # or handle aten.is_pinned for stream prefetch. They must remain in host
        # RAM without streaming, so this rung is slower and needs enough host RAM.
        if self.models_dir and use_group_offload_fast_path:
            safe_id = "".join(c if c.isalnum() or c in "._-" else "-" for c in manifest.id)
            offload_dir = str(Path(self.models_dir) / ".offload" / safe_id.lstrip("."))
            Path(offload_dir).mkdir(parents=True, exist_ok=True)
        pipeline.enable_group_offload(
            onload_device=self.torch.device(self.device),
            # leaf_level streams layer by layer and needs no block sizing;
            # block_level raises when num_blocks_per_group is unset.
            offload_type="leaf_level",
            use_stream=use_group_offload_fast_path,
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

    def _optimize_resident(
        self, pipeline: Any, mode: str, *, force_compile: bool = False,
    ) -> None:
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
        if self.torch_compile or force_compile:
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
        if mode == "realtime":
            if not manifest.t2i_adapter:
                raise ValueError(
                    f"manifest {manifest.id} has no t2i_adapter for realtime conditioning"
                )
            return self._load_realtime(manifest)
        from diffusers import AutoPipelineForImage2Image, AutoPipelineForText2Image

        cls = AutoPipelineForText2Image if mode == "t2i" else AutoPipelineForImage2Image
        loaded = self._pipelines.get((manifest.id, "i2i" if mode == "t2i" else "t2i"))
        if loaded is not None:
            pipeline = cls.from_pipe(loaded)  # shares weights already on the device
        else:
            pipeline = self._from_pretrained(cls, manifest)
            if manifest.quantize:
                component_name, scheme = manifest.quantize.split(":", 1)
                if scheme != "int8":
                    raise ValueError(
                        f"unknown quantization scheme for {manifest.id}: {scheme}"
                    )
                component = getattr(pipeline, component_name, None)
                if component is None:
                    raise ValueError(
                        f"unknown quantization component for {manifest.id}: {component_name}"
                    )
                from torchao.quantization import Int8WeightOnlyConfig, quantize_

                quantize_(component, Int8WeightOnlyConfig())
                logger.info("quantized %s on %s with int8", component_name, manifest.id)
            if manifest.lora:
                # "org/repo/file.safetensors": a distillation LoRA (Lightning,
                # Hyper-SD class) fused into the weights while still on the
                # CPU, so the device move carries the final tensors.
                repo, _, weight = manifest.lora.rpartition("/")
                pipeline.load_lora_weights(repo, weight_name=weight)
                pipeline.fuse_lora()
            rung = self._pick_rung(manifest)
            # NHWC helps full-resident GPU UNets. self.device is "cuda" for both
            # NVIDIA and ROCm (mapped in __init__); channels_last makes tensors
            # non-contiguous in the NCHW sense. Group-offload may write them
            # through safetensors, which refuses non-contiguous params
            # ("You are trying to save a non contiguous tensor").
            if self.device == "cuda" and rung == "full":
                for name in ("unet", "vae"):
                    module = getattr(pipeline, name, None)
                    if module is not None:
                        module.to(memory_format=self.torch.channels_last)
            pipeline = self._apply_rung(pipeline, manifest, rung)
            if rung == "full":
                self._optimize_resident(
                    pipeline, mode, force_compile=bool(manifest.quantize),
                )
        if manifest.scheduler:
            pipeline.scheduler = self._scheduler(manifest.scheduler, pipeline.scheduler.config)
        pipeline.set_progress_bar_config(disable=True)
        return pipeline

    async def _run_to_completion(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """`to_thread` that cannot be abandoned while its thread still runs.

        Cancellation cannot stop a thread. Awaiting `to_thread` directly means a
        cancelled await unwinds the enclosing `async with self._gpu` and frees
        the lock while the thread is still on the device, so the next entrant
        runs concurrently on a GPU the rest of the system treats as serialized
        (issue #202). Shielding alone does not help: the outer await still
        raises and still unwinds, so the thread has to be awaited before the
        cancellation propagates.
        """
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio.wait is itself an await point: a second cancellation
            # arriving here would skip the raise, unwind the lock, and leak it
            # exactly as before. Shutdown gathers can deliver one.
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait([task])
            if not task.cancelled() and (failure := task.exception()) is not None:
                # Retrieving it without logging replaces a bare "Task exception
                # was never retrieved" at GC with total silence. Report it here,
                # where the model and phase are still known.
                logger.warning("GPU work failed while cancelled: %s", failure,
                               exc_info=failure)
            raise

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        async with self._gpu:
            try:
                return await self._run_to_completion(self._calibrate_realtime,
                                                     manifest, configured)
            except Exception:
                # Could not measure; advertise nothing rather than a guess,
                # and never let a boot-time inference error kill the worker.
                logger.exception("realtime calibration failed for %s", manifest.id)
                self._calibrated_slots = 0
                self._realtime_p95_ms.pop(manifest.id, None)
                return 0

    def observe_frame_ms(self, model_id: str, gpu_ms: float) -> None:
        """Record a completed realtime frame's worker-side inference time.

        The window is bounded, so a slow early session stops affecting the
        advertised p95 once newer frames fill it (OBSERVED_FRAME_WINDOW).
        """
        window = self._observed_frame_ms.setdefault(
            model_id, deque(maxlen=OBSERVED_FRAME_WINDOW))
        window.append(gpu_ms)

    def realtime_p95_ms(self, model_id: str) -> int | None:
        window = self._observed_frame_ms.get(model_id)
        if window is not None and len(window) >= OBSERVED_FRAME_SAMPLES:
            # Real frames supersede the calibration estimate: a handful of
            # frames is not a distribution, and the first frames of a
            # session are the least representative ones.
            return round(_percentile_nearest(list(window), 95.0))
        return self._realtime_p95_ms.get(model_id)

    def p95_model_ids(self) -> list[str]:
        """Model ids this engine can report a frame p95 for, resident or not.

        A model evicted from VRAM still holds its measurements, and residency
        has nothing to do with whether a past measurement is valid: the
        heartbeat must keep advertising it until the window fills with
        newer frames.
        """
        return sorted(
            model_id for model_id in set(self._realtime_p95_ms) | set(self._observed_frame_ms)
            if self.realtime_p95_ms(model_id) is not None
        )

    def _calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        """Measure single-frame p95 and advertise slots that still meet the bar.

        Multi-image batch calibration waits on deferred cross-session batching;
        until then N sessions share the GPU lock, so capacity is bar_ms / p95.
        """
        if self.device != "cuda":
            # CPU diffusion cannot hold the bar; skip the frames, advertise nothing.
            self._calibrated_slots = 0
            self._realtime_p95_ms.pop(manifest.id, None)
            return 0
        if configured <= 0 or "realtime" not in manifest.capabilities:
            self._calibrated_slots = 0
            self._realtime_p95_ms.pop(manifest.id, None)
            return 0
        if self._select_rung(manifest) != "full":
            self._calibrated_slots = 0
            self._realtime_p95_ms.pop(manifest.id, None)
            logger.info(
                "realtime calibration skipped for %s (not full-resident)", manifest.id,
            )
            return 0
        if manifest.t2i_adapter:
            # A manifest with a sketch adapter never runs img2img frames, so
            # timing that path would size realtime_slots against a mode no
            # session uses. Calibrate the conditioned path on the manifest's
            # declared defaults, with a sparse sketch map rather than flat
            # gray: a uniform map gives the adapter nothing to condition on
            # and would time an unrealistically easy frame.
            canvas = _sparse_sketch_map()
            properties = manifest.parameters.get("properties", {})
            strength = 0.0
            params = {
                "prompt": "calibration",
                "structure_strength": float(
                    properties.get("structure_strength", {}).get("default", 1.0)
                ),
                "steps": int(properties.get("steps", {}).get("default", 2)),
            }
        else:
            canvas = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (128, 128, 128))
            strength = 0.7
            params = {"prompt": "calibration", "strength": strength}
        samples: list[float] = []
        # One discarded pass absorbs remaining compile/warmup cost.
        for index in range(CALIBRATION_SAMPLES + 1):
            # Time exactly the region serialized by the GPU lock. That occupancy,
            # rather than CPU work that can overlap it, is what the scheduler uses.
            start = time.monotonic()
            self._frame(manifest, params, canvas, strength)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            if index > 0:
                samples.append(elapsed_ms)
        p95 = _percentile_nearest(samples, 95.0)
        slots = slots_from_frame_ms(p95, configured, bar_ms=REALTIME_BAR_MS)
        self._calibrated_slots = slots
        self._realtime_p95_ms[manifest.id] = round(p95)
        logger.info(
            "realtime calibration model=%s p95_ms=%.1f slots=%d (cap=%d)",
            manifest.id, p95, slots, configured,
        )
        return slots

    def _load_realtime(self, manifest: Manifest) -> Any:
        """The sketch-conditioned realtime pipeline: the ordinary text-to-image
        pipeline composed with the manifest's T2I-Adapter, so the UNet, text
        encoders and VAE are the same objects, never re-loaded."""
        from diffusers import StableDiffusionXLAdapterPipeline, T2IAdapter

        base = self._pipelines.get((manifest.id, "t2i"))
        if base is None:
            base = self._load(manifest, "t2i")
            # Registered directly rather than through _pipeline, which would
            # re-enter the loader's own eviction and demotion logic from
            # inside a load; the rung for this model has already been chosen
            # by the caller. This does not make eviction free: the two cache
            # entries share their UNet, text encoders and VAE, so evicting
            # one of them frees nothing while the other is alive. Eviction
            # accounting does not model shared components; this at least
            # makes the base visible to the cache rather than invisible to
            # it, so a later text-to-image job reuses it instead of loading
            # a second copy.
            self._pipelines[(manifest.id, "t2i")] = base
        # T2IAdapter computes its conditioning features once before the
        # denoising loop, so the conditioned path's cost is roughly constant
        # in step count (scripts/prototype-canvas-conditioning.py).
        adapter = T2IAdapter.from_pretrained(
            manifest.t2i_adapter, torch_dtype=self.dtype,
        )
        pipeline = StableDiffusionXLAdapterPipeline.from_pipe(
            base,
            adapter=adapter,
            # from_pipe defaults to float32 and would upcast the shared UNet.
            torch_dtype=self.dtype,
        )
        if pipeline.unet is not base.unet:
            # Loading a second pipeline from the base repository would
            # duplicate the UNet and both text encoders.
            raise RuntimeError(
                f"from_pipe duplicated the UNet for {manifest.id}: the "
                "realtime conditioning pipeline must share weights with the "
                "base text-to-image pipeline"
            )
        pipeline.to(self.device)
        pipeline.set_progress_bar_config(disable=True)
        return pipeline

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

    def _forget_rung_if_unloaded(self, model_id: str) -> None:
        """Forget a cached rung once the model holds no resident pipeline.

        The rung describes the model, not one mode: while any of its
        pipelines is loaded (the realtime and t2i entries share every
        weight) the rung it was loaded at is still the truth. Only the
        removal that empties the model may clear it, or the cached answer
        outlives its conditions and the next decision is made against VRAM
        that once existed (issue #270).
        """
        if not any(key[0] == model_id for key in self._pipelines):
            self._rungs.pop(model_id, None)

    def _drop_pipeline(self, key: tuple[str, str]) -> None:
        del self._pipelines[key]
        self._forget_rung_if_unloaded(key[0])

    def _evict_model(self, model_id: str) -> None:
        for key in [key for key in self._pipelines if key[0] == model_id]:
            self._drop_pipeline(key)
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
            return await self._run_to_completion(self._load_model, manifest)

    def _load_model(self, manifest: Manifest) -> int:
        self._evict_all()
        self._rungs[manifest.id] = self._select_rung(manifest)
        start = time.monotonic()
        if "upscale" in manifest.capabilities:
            factor_spec = manifest.parameters.get("properties", {}).get("factor", {})
            mode = f"upscale-{int(factor_spec.get('default', 2))}"
        else:
            mode = "t2i"
        self._pipeline(manifest, mode)
        return int((time.monotonic() - start) * 1000)

    async def unload_model(self, model_id: str) -> None:
        async with self._gpu:
            await self._run_to_completion(self._evict_model, model_id)

    async def unload_all(self) -> None:
        async with self._gpu:
            await self._run_to_completion(self._evict_all)

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

    def _encode_clip_chunks(
        self,
        tokenizer: Any,
        text_encoder: Any,
        token_ids: list[int],
        window: int,
        chunk_count: int,
        *,
        pooled: bool,
    ) -> tuple[Any, Any | None]:
        special_tokens = tokenizer.num_special_tokens_to_add(pair=False)
        content_size = window - special_tokens
        pad_token_id = tokenizer.pad_token_id
        if pad_token_id is None:
            pad_token_id = tokenizer.eos_token_id
        chunk_embeddings = []
        first_pooled = None
        for chunk_index in range(chunk_count):
            start = chunk_index * content_size
            content = token_ids[start:start + content_size]
            # Built explicitly rather than through the tokenizer: transformers 5
            # dropped build_inputs_with_special_tokens from the CLIP tokenizers,
            # and CLIP's framing is fixed anyway at one begin and one end token.
            input_ids = [tokenizer.bos_token_id, *content, tokenizer.eos_token_id]
            padding = window - len(input_ids)
            attention_mask = [1] * len(input_ids) + [0] * padding
            input_ids += [pad_token_id] * padding
            input_tensor = self.torch.tensor([input_ids], device=self.device)
            encoder_kwargs: dict[str, Any] = {}
            if getattr(text_encoder.config, "use_attention_mask", False):
                encoder_kwargs["attention_mask"] = self.torch.tensor(
                    [attention_mask], device=self.device,
                )
            with self.torch.no_grad():
                if pooled:
                    encoder_output = text_encoder(
                        input_tensor, output_hidden_states=True, **encoder_kwargs,
                    )
                    chunk_embeddings.append(encoder_output.hidden_states[-2])
                    if chunk_index == 0:
                        first_pooled = encoder_output[0]
                else:
                    encoder_output = text_encoder(input_tensor, **encoder_kwargs)
                    chunk_embeddings.append(encoder_output[0])
        return self.torch.cat(chunk_embeddings, dim=1), first_pooled

    def _prompt_kwargs(
        self,
        pipeline: Any,
        manifest: Manifest,
        prompt: str,
        negative_prompt: str | None,
    ) -> dict[str, Any]:
        # A realtime session calls this for every frame with the same prompt, and
        # chunked encoding is a full pass through each text encoder: measured at
        # 322 ms on CPU for a two chunk prompt, against a frame budget of 250 ms.
        # Only the encoded result is worth keeping, so the cache holds one entry
        # and lives on the pipeline, which means a model switch drops it with the
        # pipeline instead of pinning embeddings for weights no longer loaded.
        cache_key = (manifest.id, prompt, negative_prompt)
        cached = getattr(pipeline, "_potocolom_prompt_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        prompt_kwargs: dict[str, Any] = {"prompt": prompt}
        if negative_prompt is not None:
            prompt_kwargs["negative_prompt"] = negative_prompt
        declared_window = manifest.prompt_token_limit
        if declared_window <= 0:
            return prompt_kwargs

        if (
            getattr(pipeline, "tokenizer_3", None) is not None
            and getattr(pipeline, "text_encoder_3", None) is not None
        ):
            # SD3 requires joint CLIP+T5 prompt embeddings. This CLIP-only
            # chunker cannot satisfy that contract, so let diffusers encode it.
            return prompt_kwargs

        tokenizer_2 = getattr(pipeline, "tokenizer_2", None)
        text_encoder_2 = getattr(pipeline, "text_encoder_2", None)
        dual_encoder = tokenizer_2 is not None and text_encoder_2 is not None
        tokenizers = [pipeline.tokenizer]
        text_encoders = [pipeline.text_encoder]
        if dual_encoder:
            tokenizers.append(tokenizer_2)
            text_encoders.append(text_encoder_2)
        window = min(
            [
                declared_window,
                *[
                    int(getattr(tokenizer, "model_max_length", declared_window))
                    for tokenizer in tokenizers
                ],
            ]
        )

        positive_ids = [
            list(tokenizer(
                prompt, add_special_tokens=False, truncation=False,
            )["input_ids"])
            for tokenizer in tokenizers
        ]
        negative_text = negative_prompt or ""
        negative_ids = [
            list(tokenizer(
                negative_text, add_special_tokens=False, truncation=False,
            )["input_ids"])
            for tokenizer in tokenizers
        ]
        chunk_count = 1
        for tokenizer, token_lists in zip(
            tokenizers, zip(positive_ids, negative_ids), strict=True,
        ):
            content_size = window - tokenizer.num_special_tokens_to_add(pair=False)
            if content_size <= 0:
                raise ValueError(f"prompt token limit {window} leaves no content tokens")
            for token_ids in token_lists:
                chunk_count = max(chunk_count, math.ceil(len(token_ids) / content_size))
        if chunk_count == 1:
            return prompt_kwargs

        positive_embeddings = []
        positive_pooled = None
        for index, (tokenizer, text_encoder, token_ids) in enumerate(zip(
            tokenizers, text_encoders, positive_ids, strict=True,
        )):
            embeddings, pooled_embedding = self._encode_clip_chunks(
                tokenizer,
                text_encoder,
                token_ids,
                window,
                chunk_count,
                pooled=dual_encoder,
            )
            positive_embeddings.append(embeddings)
            if index == 1:
                positive_pooled = pooled_embedding
        prompt_embeds = (
            self.torch.cat(positive_embeddings, dim=-1)
            if dual_encoder else positive_embeddings[0]
        )

        force_zero_negative = (
            dual_encoder
            and negative_prompt is None
            and bool(getattr(pipeline.config, "force_zeros_for_empty_prompt", False))
        )
        if force_zero_negative:
            # It implies dual_encoder, so the index-1 pass above ran with
            # pooled=True and positive_pooled holds that encoder's embedding.
            assert positive_pooled is not None
            negative_prompt_embeds = self.torch.zeros_like(prompt_embeds)
            negative_pooled = self.torch.zeros_like(positive_pooled)
        else:
            negative_embeddings = []
            negative_pooled = None
            for index, (tokenizer, text_encoder, token_ids) in enumerate(zip(
                tokenizers, text_encoders, negative_ids, strict=True,
            )):
                embeddings, pooled_embedding = self._encode_clip_chunks(
                    tokenizer,
                    text_encoder,
                    token_ids,
                    window,
                    chunk_count,
                    pooled=dual_encoder,
                )
                negative_embeddings.append(embeddings)
                if index == 1:
                    negative_pooled = pooled_embedding
            negative_prompt_embeds = (
                self.torch.cat(negative_embeddings, dim=-1)
                if dual_encoder else negative_embeddings[0]
            )

        result = {
            "prompt_embeds": prompt_embeds,
            "negative_prompt_embeds": negative_prompt_embeds,
        }
        if dual_encoder:
            result["pooled_prompt_embeds"] = positive_pooled
            result["negative_pooled_prompt_embeds"] = negative_pooled
        pipeline._potocolom_prompt_cache = (cache_key, result)
        return result

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
                return await self._run_to_completion(runner, manifest, dict(params),
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
            try:
                return await self._run_to_completion(runner, manifest, dict(params),
                                                     progress, loop, input_image)
            except self.torch.OutOfMemoryError as error:
                retry_error = error.with_traceback(None)
            if not self._demote_rung(manifest, phase="generation retry"):
                raise retry_error
            if "upscale" in manifest.capabilities:
                mode = f"upscale-{int(params.get('factor', 0))}"
            else:
                mode = "i2i" if input_image is not None else "t2i"
            await self._run_to_completion(
                self._pipeline, manifest, mode, allow_demotion=False,
            )
            return await self._run_to_completion(runner, manifest, dict(params),
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
        negative_prompt = params.get("negative_prompt")
        prompt_kwargs = self._prompt_kwargs(
            pipeline,
            manifest,
            str(params.get("prompt", "")),
            str(negative_prompt) if negative_prompt is not None else None,
        )
        start = time.monotonic()
        image = pipeline(
            **prompt_kwargs,
            num_inference_steps=steps,
            guidance_scale=float(params.get("guidance", 0.0)),
            width=int(width) if width else None,
            height=int(height) if height else None,
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

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

        negative_prompt = params.get("negative_prompt")
        prompt_kwargs = self._prompt_kwargs(
            pipeline,
            manifest,
            str(params.get("prompt", "")),
            str(negative_prompt) if negative_prompt is not None else None,
        )
        start = time.monotonic()
        image = pipeline(
            **prompt_kwargs,
            image=source,
            num_inference_steps=steps,
            strength=strength,
            guidance_scale=float(params.get("guidance", 0.0)),
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        loop.call_soon_threadsafe(progress, 1.0)
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

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
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

    async def frame(self, manifest: Manifest, params: dict, payload: bytes) -> GeneratedFrame:
        if "realtime" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support realtime frames")
        if self._pick_rung(manifest) != "full":
            raise ValueError(f"model {manifest.id} is not fully resident for realtime")
        frame_params = dict(params)

        def prepare_canvas() -> Image.Image:
            canvas = Image.open(io.BytesIO(payload)).convert("RGB")
            canvas = canvas.resize((REALTIME_SIZE, REALTIME_SIZE))
            if manifest.t2i_adapter:
                # Sketch-map conversion is Pillow work: keep it outside the
                # GPU lock, where canvas decoding already happens.
                return _canvas_to_sketch_map(canvas)
            return canvas

        async with self._codec:
            canvas = await asyncio.to_thread(prepare_canvas)
        strength = min(max(float(frame_params.get("strength", 0.7)), 0.05), 1.0)
        async with self._gpu:
            # _pipeline can load or evict GPU weights, _prompt_kwargs can run
            # GPU text encoders for long prompts, and diffusion uses the GPU.
            frame_result: tuple[Image.Image, int] | None = None
            try:
                frame_result = await self._run_to_completion(
                    self._frame, manifest, frame_params, canvas, strength)
            except self.torch.OutOfMemoryError:
                pass  # retry outside: the live traceback pins failed tensors
            except (ValueError, TypeError):
                raise
            except Exception:
                self._evict_poisoned(manifest.id)
                raise
            if frame_result is None:
                # The eviction mutates GPU residency, so it remains serialized.
                self._evict_except(manifest.id)
                frame_result = await self._run_to_completion(
                    self._frame, manifest, frame_params, canvas, strength)
            image, gpu_ms = frame_result
        async with self._codec:
            data = await asyncio.to_thread(encode_webp, image)
        return GeneratedFrame(data, gpu_ms)

    async def prepare_realtime(self, manifest: Manifest) -> bool:
        """Bring the model to full residency for realtime frames, or say no.

        Loads the realtime pipeline through the ordinary path so eviction
        gets its chance to make room, but with demotion refused: a demoted
        rung is exactly the state that makes every frame raise, so a load
        that could only succeed demoted is a failure for this purpose. The
        rung is confirmed afterwards rather than assumed: a model whose
        cached rung is below full loads successfully and still cannot
        serve frames.
        """
        async with self._gpu:
            try:
                await self._run_to_completion(self._prepare_realtime, manifest)
            except self.torch.OutOfMemoryError:
                logger.warning(
                    "realtime session refused for %s: could not load fully resident",
                    manifest.id,
                )
                return False
            except Exception:
                logger.exception("realtime session refused for %s: prepare failed",
                                 manifest.id)
                return False
        return self._pick_rung(manifest) == "full"

    def _prepare_realtime(self, manifest: Manifest) -> None:
        self._pipeline(manifest, "realtime", allow_demotion=False)

    def _frame(
        self,
        manifest: Manifest,
        params: dict,
        canvas: Image.Image,
        strength: float,
    ) -> tuple[Image.Image, int]:
        if manifest.t2i_adapter:
            # Conditioned text-to-image: the canvas (already converted to the
            # adapter's sketch map, outside the GPU lock in frame())
            # conditions a fresh latent instead of an init image. img2img has
            # no useful middle strength here: sweeps return the line drawing
            # until 1.0, where the scene ignores it.
            pipeline = self._pipeline(manifest, "realtime")
            negative_prompt = params.get("negative_prompt")
            prompt_kwargs = self._prompt_kwargs(
                pipeline,
                manifest,
                str(params.get("prompt", "")),
                str(negative_prompt) if negative_prompt is not None else None,
            )
            properties = manifest.parameters.get("properties", {})
            structure_strength = float(params.get(
                "structure_strength",
                properties.get("structure_strength", {}).get("default", 1.0),
            ))
            steps = int(params.get(
                "steps", properties.get("steps", {}).get("default", 2),
            ))
            generator = None
            if isinstance(params.get("seed"), int):
                # A fresh latent per frame makes an unchanged canvas re-roll
                # the whole image (measured at 85.9 percent of pixels); the
                # session's seed keeps it stable. Seed policy lives in the
                # worker's session layer (client.ensure_seed), so a missing
                # or non-integer seed means no generator, not one invented
                # here.
                generator = self.torch.Generator(self.device).manual_seed(
                    int(params["seed"])
                )
            started = time.monotonic()
            image = pipeline(
                **prompt_kwargs,
                image=canvas,
                num_inference_steps=steps,
                adapter_conditioning_scale=structure_strength,
                guidance_scale=0.0,
                generator=generator,
            ).images[0]
            gpu_ms = int((time.monotonic() - started) * 1000)
            return image, gpu_ms
        pipeline = self._pipeline(manifest, "i2i")
        negative_prompt = params.get("negative_prompt")
        prompt_kwargs = self._prompt_kwargs(
            pipeline,
            manifest,
            str(params.get("prompt", "")),
            str(negative_prompt) if negative_prompt is not None else None,
        )
        started = time.monotonic()
        image = pipeline(
            **prompt_kwargs,
            image=canvas,
            # Few-step img2img: diffusers runs ceil(steps * strength) steps,
            # so keep the product at one or above.
            num_inference_steps=max(2, math.ceil(1 / strength)),
            strength=strength,
            guidance_scale=0.0,
        ).images[0]
        gpu_ms = int((time.monotonic() - started) * 1000)
        return image, gpu_ms
