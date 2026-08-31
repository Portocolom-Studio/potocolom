"""Inference engines behind one interface (issue #15).

SimulatedEngine keeps the wire protocol runnable anywhere; DiffusersEngine is
the real thing and imports torch lazily, so the package installs and imports
without the inference extra.

Engines call the progress callback on the event loop, never from the
inference thread directly.
"""

import asyncio
import contextlib
import inspect
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

from PIL import Image, ImageChops, ImageDraw, ImageOps

from worker.manifests import Manifest
from worker.frame_batch import (
    FrameBatchCollector,
    FrameRequest,
    occupancy_share_ms,
)
from worker.memory_ladder import (
    MemoryMode,
    MemoryRung,
    REALTIME_BAR_MS,
    measured_wire_manifest,
    measured_wire_manifests,
    rung_vram_bytes,
    select_rung,
    slots_from_batch_curve,
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
_PREVIEW_DECODER_ATTR = "_potocolom_preview_decoder"
_PREVIEW_DECODER_RETRY_ATTR = "_potocolom_preview_decoder_retry_after"
# A failed preview decoder never disables the fast path permanently. Loads fail
# for transient reasons: an out-of-memory card that later frees, a cold cache
# behind a network blip, a rate-limited registry. Falling back for this frame and
# retrying after a pause keeps frames rendering without reloading every frame.
PREVIEW_DECODER_RETRY_S = 60.0


@dataclass
class GeneratedImage:
    data: bytes  # PNG generation master
    width: int
    height: int
    gpu_ms: int
    load_ms: int = 0


def reject_degenerate_output(image: Image.Image, model_id: str) -> None:
    """Raise when a denoise decoded to one flat colour.

    A saturated or NaN denoise decodes to a single constant colour, and the
    job would otherwise be stored as succeeded with a plausible asset row: that
    is exactly how the group offload streaming defect stayed invisible (see
    _apply_rung). Raising here fails the job and, through generate()'s
    poison-evict branch, drops the resident that produced it so the next job
    reloads clean rather than repeating the fault.

    Every band being exactly constant is the whole test. VAE decode noise means
    real diffusion output is never bit-flat, so there is no tolerance to tune
    and no legitimate generation to misjudge. Upscale is not checked, because a
    flat source legitimately upscales to a flat result, and neither is the
    realtime frame path, which never reaches the offload rung this guards.
    SimulatedEngine emits flat colour by design and is a separate class.
    """
    extrema = image.getextrema()
    # Single-band images return one (low, high) pair rather than one per band.
    bands = extrema if isinstance(extrema[0], tuple) else (extrema,)
    if all(low == high for low, high in bands):
        raise RuntimeError(
            f"{model_id} produced a single flat colour, so the denoise or the "
            "VAE decode failed; the image was discarded rather than stored"
        )


def _empty_frame_stages() -> dict[str, int]:
    return {
        "adapter_ms": 0,
        "text_encode_ms": 0,
        "unet_ms": 0,
        "taesd_ms": 0,
        "overhead_ms": 0,
        "text_encode_cache_hit": 0,
        "unet_forwards": 0,
    }


def _finish_stage_overhead(stages: dict[str, int], gpu_ms: int) -> None:
    stages["overhead_ms"] = max(
        0,
        gpu_ms
        - stages["adapter_ms"]
        - stages["text_encode_ms"]
        - stages["unet_ms"]
        - stages["taesd_ms"],
    )


@dataclass
class GeneratedFrame:
    data: bytes
    gpu_ms: int
    stages: dict[str, int] | None = None


class NotResidentError(ValueError):
    """The model is not on the full realtime rung, so a frame cannot run."""


class Cancelled(Exception):
    """The job was asked to stop, and the work aborted where it stood.

    Raising is how a diffusers pipeline is interrupted: the call runs on the
    GPU thread and no await can reach into it. Nothing was produced, so the
    caller reports what the GPU cost instead of a failure.
    """


@dataclass
class PromptCache:
    """One cached prompt encoding, owned by the realtime session (issue #301).

    The session creates the holder and passes it to every frame; the engine
    never retains it, so there is no release path to forget and the entry is
    dropped with the runner. The key is what was encoded, so an edited prompt
    misses and re-encodes once, which is exactly what update_session carrying
    a new prompt is; seed, canvas and structure strength never reach the text
    encoders, so nothing else invalidates the entry.
    """

    entry: tuple[tuple[Any, Any, Any], dict[str, Any]] | None = None


class Engine(Protocol):
    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GeneratedImage: ...

    async def frame(
        self, manifest: Manifest, params: dict, payload: bytes,
        *, prompt_cache: PromptCache | None = None,
        profile: bool = False,
    ) -> GeneratedFrame: ...

    def loaded_models(self) -> list[str]: ...

    def measured_manifests(self, manifests: list[Manifest]) -> list[dict]: ...

    def effective_realtime_slots(self, wire_manifests: list[dict], configured: int) -> int: ...

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int: ...

    def observe_frame_ms(self, model_id: str, gpu_ms: float) -> None: ...

    def realtime_p95_ms(self, model_id: str) -> int | None: ...

    def realtime_batch_ms(self, model_id: str) -> list[int] | None: ...

    def p95_model_ids(self) -> list[str]: ...

    async def load_model(self, manifest: Manifest) -> int: ...

    async def ensure_realtime_resident(self, manifest: Manifest) -> bool: ...

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
    """Canvas (strokes on white paper) to the adapter's conditioning-map
    convention (light strokes on black), with no learned preprocessor. The
    threshold variant (the prototype's stream default) binarizes at the
    midpoint, which kills WebP ring halos while the antialiased stroke cores
    stay (scripts/prototype-canvas-conditioning.py).

    A stroke is a pixel far from paper white, measured as the darkest of its
    three channels rather than as luminance. Luminance made the map depend on
    hue once the canvas gained a palette: pure green weighs 150 and pure
    yellow 226 against a 128 threshold, so both vanished into the paper while
    red at 76 and blue at 29 drew. The darkest channel is 0 for all four, and
    for a grey pixel it is the grey itself, so black on white is unchanged.
    Which colour a stroke is still carries no meaning to the adapter (that is
    issue #266); this only decides whether the adapter sees the stroke."""
    red, green, blue = canvas.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    inverted = ImageOps.invert(darkest)
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

    def __init__(
        self, inference_seconds: float, *, batch_window_ms: float = 0.0,
    ):
        self.inference_seconds = inference_seconds
        self._loaded: set[str] = set()
        self._gpu = asyncio.Lock()
        # No collection delay by default: simulated inference is for protocol
        # tests, not timing the batch window. Cross-session batching still runs
        # through the same collector path as DiffusersEngine.
        self._batch_collector = FrameBatchCollector(self, window_ms=batch_window_ms)
        self._last_batch_size = 0
        self._batch_sizes: list[int] = []

    async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
        async with self._gpu:
            self._last_batch_size = len(requests)
            self._batch_sizes.append(len(requests))
            started = time.monotonic()
            await asyncio.sleep(self.inference_seconds)
            gpu_ms = occupancy_share_ms(
                int((time.monotonic() - started) * 1000),
                len(requests),
            )
            for request in requests:
                if request.cancelled or request.future.done():
                    continue
                stages = _empty_frame_stages() if request.profile else None
                request.future.set_result(
                    GeneratedFrame(request.payload, gpu_ms, stages),
                )

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

    def realtime_batch_ms(self, model_id: str) -> list[int] | None:
        return None

    def p95_model_ids(self) -> list[str]:
        # Simulated engine never measured a frame; nothing to advertise.
        return []

    async def load_model(self, manifest: Manifest) -> int:
        start = time.monotonic()
        await asyncio.sleep(self.inference_seconds / 4)
        self._loaded = {manifest.id}
        return int((time.monotonic() - start) * 1000)

    async def ensure_realtime_resident(self, manifest: Manifest) -> bool:
        # The simulated engine renders without residency; nothing to make resident.
        return True

    async def unload_model(self, model_id: str) -> None:
        self._loaded.discard(model_id)

    async def unload_all(self) -> None:
        self._loaded.clear()

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
        cancelled: Callable[[], bool] | None = None,
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
            if cancelled is not None and cancelled():
                # The real engine takes this between tiles. The simulated one
                # has one resize, so between its halves is the same place.
                raise Cancelled()
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
            if cancelled is not None and cancelled():
                raise Cancelled()
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

    async def frame(
        self, manifest: Manifest, params: dict, payload: bytes,
        *, prompt_cache: PromptCache | None = None,
        profile: bool = False,
    ) -> GeneratedFrame:
        session_key = (
            id(prompt_cache) if prompt_cache is not None
            else id(asyncio.current_task())
        )
        return await self._batch_collector.submit(
            session_key, manifest, params, payload, 0.0,
            prompt_cache=prompt_cache, resolution=REALTIME_SIZE,
            profile=profile,
        )


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
        self._calibration_cap: int = 0
        self._realtime_p95_ms: dict[str, int] = {}
        self._realtime_batch_ms: dict[str, list[int]] = {}
        self._observed_frame_ms: dict[str, deque[float]] = {}
        self._gpu = asyncio.Lock()
        self._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
        self._batch_collector = FrameBatchCollector(self)

    def _batch_collector_or_create(self) -> FrameBatchCollector:
        collector = getattr(self, "_batch_collector", None)
        if collector is None:
            # Tests build engines with __new__ and skip __init__; a zero window
            # keeps their frame calls from waiting on the production collector.
            collector = FrameBatchCollector(self, window_ms=0.0)
            self._batch_collector = collector
        return collector

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
        can_offload_to_disk = not manifest.quantize
        offload_dir = None
        # Quantized torchao subclass tensors cannot be serialized by safetensors,
        # so they must stay in host RAM: this rung needs enough of it.
        if self.models_dir and can_offload_to_disk:
            safe_id = "".join(c if c.isalnum() or c in "._-" else "-" for c in manifest.id)
            offload_dir = str(Path(self.models_dir) / ".offload" / safe_id.lstrip("."))
            Path(offload_dir).mkdir(parents=True, exist_ok=True)
        pipeline.enable_group_offload(
            onload_device=self.torch.device(self.device),
            # leaf_level streams layer by layer and needs no block sizing;
            # block_level raises when num_blocks_per_group is unset.
            offload_type="leaf_level",
            # use_stream=True silently produces wrong output on this rung:
            # diffusers' lazy prefetch fails to onload every leaf ("some layers
            # were not executed during the forward pass"), latents come back at
            # roughly 20x their normal range (-56..56 against -2.7..2.4) and
            # decode to a solid black image while the job still reports
            # succeeded. Correctness first; this rung is already the slow one.
            use_stream=False,
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
        cls = self._pipeline_class(manifest, mode)
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
                pipeline.load_lora_weights(
                    repo, weight_name=weight,
                    revision=manifest.lora_revision or None,
                )
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

    def _recompute_calibrated_slots(self) -> int:
        """Min of slots_from_frame_ms across models this engine has measured.

        Last-write-wins on a single int was the honesty defect: calibrating
        a slow model after a fast one advertised the slow count for both.
        A failure for one model must not zero the others.
        """
        if not self._realtime_p95_ms:
            self._calibrated_slots = 0
            return 0
        self._calibrated_slots = min(
            slots_from_batch_curve(
                self._realtime_batch_ms[model_id],
                self._calibration_cap,
                bar_ms=REALTIME_BAR_MS,
            )
            if model_id in getattr(self, "_realtime_batch_ms", {})
            else slots_from_frame_ms(
                float(p95), self._calibration_cap, bar_ms=REALTIME_BAR_MS,
            )
            for model_id, p95 in self._realtime_p95_ms.items()
        )
        return self._calibrated_slots

    async def calibrate_realtime(self, manifest: Manifest, configured: int) -> int:
        self._calibration_cap = configured
        try:
            async with self._gpu:
                slots = await self._run_to_completion(
                    self._calibrate_realtime, manifest, configured,
                )
        except Exception:
            logger.exception("realtime calibration failed for %s", manifest.id)
            self._realtime_p95_ms.pop(manifest.id, None)
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            self._recompute_calibrated_slots()
            return 0
        slots = self._recompute_calibrated_slots()
        if slots <= 0:
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            return slots
        try:
            await self._calibrate_batch_curve(manifest, configured)
        except Exception:
            logger.exception("realtime batch calibration failed for %s", manifest.id)
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            self._recompute_calibrated_slots()
        return self._calibrated_slots or 0

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

    def realtime_batch_ms(self, model_id: str) -> list[int] | None:
        curve = getattr(self, "_realtime_batch_ms", {}).get(model_id)
        return list(curve) if curve else None

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

        Batch calibration runs after this single-frame measurement.
        """
        self._calibration_cap = configured
        if self.device != "cuda":
            # CPU diffusion cannot hold the bar; skip the frames, advertise
            # nothing for this model. Sibling measurements stay.
            self._realtime_p95_ms.pop(manifest.id, None)
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            self._recompute_calibrated_slots()
            return 0
        if configured <= 0 or "realtime" not in manifest.capabilities:
            self._realtime_p95_ms.pop(manifest.id, None)
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            self._recompute_calibrated_slots()
            return 0
        if self._select_rung(manifest) != "full":
            self._realtime_p95_ms.pop(manifest.id, None)
            getattr(self, "_realtime_batch_ms", {}).pop(manifest.id, None)
            self._recompute_calibrated_slots()
            logger.info(
                "realtime calibration skipped for %s (not full-resident)", manifest.id,
            )
            return 0
        params, canvas, strength = self._calibration_inputs(manifest)
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
        self._realtime_p95_ms[manifest.id] = round(p95)
        slots = self._recompute_calibrated_slots()
        logger.info(
            "realtime calibration model=%s p95_ms=%.1f slots=%d (cap=%d)",
            manifest.id, p95, slots, configured,
        )
        return slots

    def _calibration_inputs(
        self, manifest: Manifest,
    ) -> tuple[dict[str, Any], Image.Image, float]:
        if manifest.t2i_adapter:
            canvas = _sparse_sketch_map()
            properties = manifest.parameters.get("properties", {})
            return (
                {
                    "prompt": "calibration",
                    "structure_strength": float(
                        properties.get("structure_strength", {}).get("default", 1.0)
                    ),
                    "steps": int(properties.get("steps", {}).get("default", 2)),
                },
                canvas,
                0.0,
            )
        return (
            {"prompt": "calibration", "strength": 0.7},
            Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (128, 128, 128)),
            0.7,
        )

    async def _measure_batch_p95(
        self, count: int, manifest: Manifest, params: dict, payload: bytes,
    ) -> int:
        caches = [PromptCache() for _ in range(count)]
        samples: list[float] = []
        for index in range(CALIBRATION_SAMPLES + 1):
            started = time.monotonic()
            await asyncio.gather(*(
                self.frame(manifest, params, payload, prompt_cache=caches[item])
                for item in range(count)
            ))
            if index > 0:
                samples.append((time.monotonic() - started) * 1000.0)
        return round(_percentile_nearest(samples, 95.0))

    async def _calibrate_batch_curve(
        self, manifest: Manifest, configured: int,
    ) -> None:
        p95 = self._realtime_p95_ms.get(manifest.id)
        if p95 is None or configured < 2:
            self._realtime_batch_ms.pop(manifest.id, None)
            self._recompute_calibrated_slots()
            return
        params, canvas, _strength = self._calibration_inputs(manifest)
        payload = encode_png(canvas)
        curve = [p95]
        for count in range(2, configured + 1):
            wall = await self._measure_batch_p95(count, manifest, params, payload)
            if wall > REALTIME_BAR_MS:
                break
            if wall < curve[-1]:
                wall = curve[-1]
            curve.append(wall)
        self._realtime_batch_ms[manifest.id] = curve
        slots = self._recompute_calibrated_slots()
        logger.info(
            "realtime batch calibration model=%s curve=%s slots=%d (cap=%d)",
            manifest.id, curve, slots, configured,
        )

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
            # by the caller. Registering makes the base visible to the cache
            # rather than invisible to it, so a later text-to-image job reuses
            # it instead of loading a second complete copy.
            #
            # Two entries for one model share their UNet, text encoders and
            # VAE, and eviction handles that because it works per model:
            # _evict_cold collects model ids and _evict_model drops every key
            # for the one it picks, so the shared weights are released
            # together and no path removes a single entry. Measured with a
            # two-entry model beside another: both entries went, the cached
            # rung was forgotten, the other model stayed.
            self._pipelines[(manifest.id, "t2i")] = base
        # T2IAdapter computes its conditioning features once before the
        # denoising loop, so the conditioned path's cost is roughly constant
        # in step count (scripts/prototype-canvas-conditioning.py).
        adapter = T2IAdapter.from_pretrained(
            manifest.t2i_adapter, torch_dtype=self.dtype,
            revision=manifest.t2i_adapter_revision or None,
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

    def _preview_decoder(self, pipeline: Any, manifest: Manifest) -> Any | None:
        """Load and retain a realtime-only decoder on its owning pipeline.

        Returning None means this frame decodes with the model's full VAE. That is
        slower but correct, so no failure here may stop a frame rendering.

        Loading here holds the GPU lock, which frame() took, so a cold Hugging
        Face cache stalls every session on this worker. That is narrower than it
        sounds: warmup_realtime calibrates by rendering frames before hello, so
        the default realtime model's decoder is built before the worker registers
        and before any session exists. Only a realtime model that was not the
        warmed default, or the retry after a deferral, can pay a cold fetch here.

        Prefetching at load time was tried and removed. Building the realtime
        pipeline there to hold the decoder cost 0.15 GiB and an extra pipeline
        that a queued job on the same model never uses; vega-rt serves jobs too.
        A snapshot_download prefetch instead put a network round trip on every
        model load, and could not be made conditional on a warm cache, because a
        repo snapshot is never complete when from_pretrained fetches only the
        files it needs, so a local-only probe always misses and refetches.
        """
        if not manifest.preview_decoder:
            return None
        pipeline_state = vars(pipeline)
        cached = pipeline_state.get(_PREVIEW_DECODER_ATTR)
        if cached is not None:
            return cached
        retry_after = pipeline_state.get(_PREVIEW_DECODER_RETRY_ATTR)
        if retry_after is not None and time.monotonic() < retry_after:
            return None
        try:
            from diffusers import AutoencoderTiny

            decoder = AutoencoderTiny.from_pretrained(
                manifest.preview_decoder, torch_dtype=self.dtype,
                revision=manifest.preview_decoder_revision or None,
            ).to(self.device)
        except Exception as error:
            # Every failure is treated as transient. frame()'s evict-and-retry
            # cannot help here: it evicts other models, and a realtime worker
            # usually holds only this one, so there is nothing to evict.
            self._defer_preview_decoder(pipeline, manifest, "load", error)
            return None
        pipeline_state[_PREVIEW_DECODER_ATTR] = decoder
        pipeline_state.pop(_PREVIEW_DECODER_RETRY_ATTR, None)
        logger.info("loaded preview decoder %s for %s", manifest.preview_decoder, manifest.id)
        return decoder

    def _defer_preview_decoder(
        self, pipeline: Any, manifest: Manifest, phase: str, error: BaseException,
    ) -> None:
        """Fall back to the full VAE and try the decoder again after a pause."""
        try:
            state = vars(pipeline)
        except TypeError:
            state = None
        if state is not None:
            state.pop(_PREVIEW_DECODER_ATTR, None)
            state[_PREVIEW_DECODER_RETRY_ATTR] = time.monotonic() + PREVIEW_DECODER_RETRY_S
        # Slots were measured with the distilled decoder and overstate full-VAE
        # capacity. Clearing them only takes effect at the next registration, when
        # realtime_slots is sent again; until a worker reconnects, the API may
        # keep two sessions on a path that now serves one. Closing that gap needs
        # slot counts on the heartbeat or a worker-side refusal, which is other
        # work. The clear is still worth doing so the next registration is honest.
        self._calibrated_slots = None
        logger.warning(
            "preview decoder %s %s failed for %s; using full VAE, retrying in %.0fs: %s",
            manifest.preview_decoder, phase, manifest.id, PREVIEW_DECODER_RETRY_S, error,
        )

    def _render_with_preview_decoder(
        self,
        pipeline: Any,
        manifest: Manifest,
        pipeline_kwargs: dict[str, Any],
        *,
        preview_decoder: Any | None = None,
        stages: dict[str, int] | None = None,
    ) -> Image.Image:
        if preview_decoder is None:
            preview_decoder = self._preview_decoder(pipeline, manifest)
        image = None
        if preview_decoder is not None:
            latents = pipeline(**pipeline_kwargs, output_type="latent").images
            try:
                decode_started = 0.0
                if stages is not None:
                    self._sync_cuda()
                    decode_started = time.perf_counter()
                # AutoencoderTiny is trained to take the pipeline's SDXL latents
                # directly. Unlike the full VAE path, dividing by
                # pipeline.vae.config.scaling_factor here produces clipped noise.
                with self.torch.inference_mode():
                    decoded = preview_decoder.decode(latents, return_dict=False)[0]
                if stages is not None:
                    self._sync_cuda()
                    stages["taesd_ms"] += int(
                        (time.perf_counter() - decode_started) * 1000
                    )
                image = pipeline.image_processor.postprocess(decoded, output_type="pil")[0]
            except Exception as error:
                # A bad decoder is not a poisoned pipeline. Letting this reach
                # frame()'s handler would evict and reload the whole model on every
                # frame while the decoder failed identically each time.
                self._defer_preview_decoder(pipeline, manifest, "decode", error)
        if image is None:
            # pipeline_kwargs still carries the generator from the latent pass,
            # which already advanced it, so this image is not what a clean
            # full-VAE render at this seed would be; the next frame re-seeds.
            image = pipeline(**pipeline_kwargs).images[0]
        return image

    def _render_with_preview_decoder_batch(
        self,
        pipeline: Any,
        manifest: Manifest,
        pipeline_kwargs: dict[str, Any],
        *,
        preview_decoder: Any | None = None,
    ) -> list[Image.Image]:
        if preview_decoder is None:
            preview_decoder = self._preview_decoder(pipeline, manifest)
        images: list[Image.Image] | None = None
        if preview_decoder is not None:
            latents = pipeline(**pipeline_kwargs, output_type="latent").images
            try:
                with self.torch.inference_mode():
                    decoded = preview_decoder.decode(latents, return_dict=False)[0]
                images = pipeline.image_processor.postprocess(
                    decoded, output_type="pil",
                )
            except Exception as error:
                self._defer_preview_decoder(pipeline, manifest, "decode", error)
        if images is None:
            images = pipeline(**pipeline_kwargs).images
        return images

    @staticmethod
    def _session_frame_key(
        prompt_cache: PromptCache | None,
    ) -> int:
        if prompt_cache is not None:
            return id(prompt_cache)
        return id(asyncio.current_task())

    def _merge_prompt_embeds(
        self, prompt_kwargs_list: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Stack per-request embeds on dim 0, or None when they cannot batch.

        Unlimited-token and SD3 paths return only `prompt` / `negative_prompt`.
        Concatenating those away leaves the pipeline with neither prompt nor
        embeds, which poisons the resident model. Mismatched chunk counts
        raise on cat and would do the same.
        """
        if not prompt_kwargs_list:
            return None
        skip = ("prompt", "negative_prompt")
        embed_keys = [key for key in prompt_kwargs_list[0] if key not in skip]
        if not embed_keys:
            return None
        merged: dict[str, Any] = {}
        for key in embed_keys:
            tensors = []
            for kwargs in prompt_kwargs_list:
                value = kwargs.get(key)
                if value is None:
                    return None
                tensors.append(value)
            try:
                reference = tensors[0].shape[1:]
                if any(tensor.shape[1:] != reference for tensor in tensors):
                    return None
                merged[key] = self.torch.cat(tensors, dim=0)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return None
        return merged

    def _frame_batch_sequential(
        self, requests: list[FrameRequest],
    ) -> list[tuple[Image.Image, int]]:
        results: list[tuple[Image.Image, int]] = []
        for request in requests:
            started = time.monotonic()
            stages = _empty_frame_stages() if request.profile else None
            image, _ = self._frame(
                request.manifest, request.params, request.payload, request.strength,
                prompt_cache=request.prompt_cache, stages=stages,
            )
            gpu_ms = int((time.monotonic() - started) * 1000)
            if stages is not None:
                _finish_stage_overhead(stages, gpu_ms)
                request.stages = stages
            results.append((image, gpu_ms))
        return results

    async def execute_frame_batch(self, requests: list[FrameRequest]) -> None:
        if not requests:
            return
        manifest = requests[0].manifest
        frame_result: list[tuple[Image.Image, int]] | None = None
        try:
            async with self._gpu:
                self._require_realtime_resident(manifest)
                try:
                    frame_result = await self._run_to_completion(
                        self._frame_batch, requests,
                    )
                except self.torch.OutOfMemoryError:
                    pass
                except (ValueError, TypeError) as error:
                    for request in requests:
                        if not request.cancelled and not request.future.done():
                            request.future.set_exception(error)
                    return
                except Exception as error:
                    self._evict_poisoned(manifest.id)
                    for request in requests:
                        if not request.cancelled and not request.future.done():
                            request.future.set_exception(error)
                    return
                if frame_result is None:
                    self._evict_except(manifest.id)
                    self._require_realtime_resident(manifest)
                    try:
                        frame_result = await self._run_to_completion(
                            self._frame_batch, requests,
                        )
                    except self.torch.OutOfMemoryError as error:
                        raise NotResidentError(
                            f"model {manifest.id} is not fully resident for realtime"
                        ) from error
        except NotResidentError as error:
            for request in requests:
                if not request.cancelled and not request.future.done():
                    request.future.set_exception(error)
            return
        except Exception as error:
            for request in requests:
                if not request.cancelled and not request.future.done():
                    request.future.set_exception(error)
            return
        if frame_result is None:
            return
        encode_tasks = []
        for request, (image, gpu_ms) in zip(
            requests, frame_result, strict=True,
        ):
            if request.cancelled or request.future.done():
                continue
            encode_tasks.append(asyncio.create_task(
                self._encode_frame_result(request, image, gpu_ms),
            ))
        if encode_tasks:
            await asyncio.gather(*encode_tasks)

    async def _encode_frame_result(
        self,
        request: FrameRequest,
        image: Image.Image,
        gpu_ms: int,
    ) -> None:
        async with self._codec:
            data = await self._run_to_completion(encode_webp, image)
        if not request.cancelled and not request.future.done():
            request.future.set_result(GeneratedFrame(data, gpu_ms, request.stages))

    def _frame_batch(
        self, requests: list[FrameRequest],
    ) -> list[tuple[Image.Image, int]]:
        started = time.monotonic()
        manifest = requests[0].manifest
        if len(requests) == 1 or any(request.profile for request in requests):
            if len(requests) > 1:
                return self._frame_batch_sequential(requests)
            request = requests[0]
            stages = _empty_frame_stages() if request.profile else None
            image, _ = self._frame(
                manifest, request.params, request.payload, request.strength,
                prompt_cache=request.prompt_cache, stages=stages,
            )
            gpu_ms = int((time.monotonic() - started) * 1000)
            if stages is not None:
                _finish_stage_overhead(stages, gpu_ms)
                request.stages = stages
            return [(image, gpu_ms)]
        if manifest.t2i_adapter:
            pipeline = self._pipeline(
                manifest, "realtime", allow_demotion=False,
            )
            preview_decoder = self._preview_decoder(pipeline, manifest)
            prompt_kwargs_list: list[dict[str, Any]] = []
            images: list[Image.Image] = []
            generators: list[Any] = []
            scales: list[float] = []
            properties = manifest.parameters.get("properties", {})
            steps = requests[0].compat.steps
            seeded = False
            for request in requests:
                negative_prompt = request.params.get("negative_prompt")
                prompt_kwargs_list.append(self._prompt_kwargs(
                    pipeline,
                    manifest,
                    str(request.params.get("prompt", "")),
                    str(negative_prompt) if negative_prompt is not None else None,
                    prompt_cache=request.prompt_cache,
                ))
                images.append(request.payload)
                seed = request.params.get("seed")
                if isinstance(seed, int):
                    seeded = True
                    generators.append(
                        self.torch.Generator(self.device).manual_seed(int(seed)),
                    )
                else:
                    generators.append(self.torch.Generator(self.device))
                scales.append(float(request.params.get(
                    "structure_strength",
                    properties.get("structure_strength", {}).get("default", 1.0),
                )))
            adapter_scale = self.torch.tensor(
                [[[[scale]]] for scale in scales],
                device=self.device, dtype=self.dtype,
            )
            merged = self._merge_prompt_embeds(prompt_kwargs_list)
            if merged is None:
                return self._frame_batch_sequential(requests)
            pipeline_kwargs = {
                **merged,
                "image": images,
                "num_inference_steps": steps,
                "adapter_conditioning_scale": adapter_scale,
                "guidance_scale": 0.0,
            }
            if seeded:
                pipeline_kwargs["generator"] = generators
            rendered = self._render_with_preview_decoder_batch(
                pipeline, manifest, pipeline_kwargs,
                preview_decoder=preview_decoder,
            )
            gpu_ms = occupancy_share_ms(
                int((time.monotonic() - started) * 1000),
                len(requests),
            )
            return [(image, gpu_ms) for image in rendered]
        return self._frame_batch_sequential(requests)

    @staticmethod
    def _detach_preview_decoder(pipeline: Any) -> None:
        try:
            vars(pipeline).pop(_PREVIEW_DECODER_ATTR, None)
        except TypeError:
            # Some test and upscale runtimes are opaque objects with no state.
            pass

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
        # Detach here rather than at each caller: a preview decoder outliving the
        # pipeline it was loaded for would pin VRAM for weights already gone, and
        # every drop path routes through this one.
        self._detach_preview_decoder(self._pipelines[key])
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
        for pipeline in self._pipelines.values():
            self._detach_preview_decoder(pipeline)
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

    async def ensure_realtime_resident(self, manifest: Manifest) -> bool:
        """Make the model fully resident if realtime needs it, and say whether
        it is.

        A realtime frame runs against a 500 ms bar, so a model on an offload
        rung cannot serve one: frame refuses it, and a session would log that
        refusal on every frame it never renders. The rung answer is cached
        per model, and only the load path ever decides it again, because the
        eviction that frees the VRAM lives there and frame never reaches it
        once the cached answer says offload (issue #270). The way to turn a
        stale offload into full is to evict everyone and choose again against
        the freed VRAM, which is exactly what load_model already does under
        the GPU lock, so that is the path reused here rather than a second
        eviction implementation. A pinned memory mode is an operator choice
        and is left alone: pins are never demoted, and nothing here second-
        guesses a pin that placed the model off-GPU; such a session gets the
        per-frame fallback instead of an eviction the operator did not ask
        for.
        """
        if self._pick_rung(manifest) == "full":
            return True
        if self.memory_mode != "auto":
            return False
        await self.load_model(manifest)
        return self._pick_rung(manifest) == "full"

    def _require_realtime_resident(self, manifest: Manifest) -> None:
        """Raise unless the model is on the full realtime rung.

        Callers must hold `_gpu`: `_pick_rung` writes `self._rungs`.
        """
        if self._pick_rung(manifest) != "full":
            raise NotResidentError(
                f"model {manifest.id} is not fully resident for realtime")

    async def unload_model(self, model_id: str) -> None:
        async with self._gpu:
            await self._run_to_completion(self._evict_model, model_id)

    async def unload_all(self) -> None:
        async with self._gpu:
            await self._run_to_completion(self._evict_all)

    def _pipeline_class(self, manifest: Manifest, mode: str) -> Any:
        import diffusers

        if not manifest.pipeline:
            return (
                diffusers.AutoPipelineForText2Image if mode == "t2i"
                else diffusers.AutoPipelineForImage2Image
            )
        suffix = "Pipeline" if mode == "t2i" else "Img2ImgPipeline"
        name = f"{manifest.pipeline}{suffix}"
        cls = getattr(diffusers, name, None)
        if cls is None:
            raise ValueError(
                f"manifest {manifest.id}: diffusers has no pipeline class {name}"
            )
        return cls

    def _pipeline_dtype(self, manifest: Manifest) -> Any:
        declared = manifest.dtype
        if declared:
            return getattr(self.torch, declared)
        return self.dtype

    def _from_pretrained(self, cls: Any, manifest: Manifest) -> Any:
        source = manifest.source
        dtype = self._pipeline_dtype(manifest)
        kwargs: dict[str, Any] = {
            "torch_dtype": dtype, "revision": manifest.source_revision or None,
        }
        if manifest.vae:
            from diffusers import AutoencoderKL

            # SDXL's stock VAE upcasts itself to fp32 at decode time (fp16
            # overflows), which spikes VRAM past a 16 GB card; manifests name
            # an fp16-safe replacement instead.
            kwargs["vae"] = AutoencoderKL.from_pretrained(
                manifest.vae, torch_dtype=dtype,
                revision=manifest.vae_revision or None,
            )
        if dtype is self.torch.float16:
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
        *,
        prompt_cache: PromptCache | None = None,
        stages: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        # A realtime session calls this for every frame with the same prompt, and
        # encoding is a full pass through each text encoder: measured at 322 ms
        # on CPU for a two chunk prompt, against a frame budget of 250 ms. Only
        # the encoded result is worth keeping, so the cache holds one entry and
        # lives on the session's holder, which the caller drops with the session
        # instead of pinning embeddings for weights no longer loaded.
        cache_key = (manifest.id, prompt, negative_prompt)
        if prompt_cache is not None:
            if prompt_cache.entry is not None and prompt_cache.entry[0] == cache_key:
                if stages is not None:
                    stages["text_encode_cache_hit"] = 1
                return prompt_cache.entry[1]

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

        encoder_model_types = {
            str(getattr(getattr(encoder, "config", None), "model_type", ""))
            for encoder in (getattr(pipeline, "text_encoder", None),
                            getattr(pipeline, "text_encoder_2", None))
            if encoder is not None
        }
        if not encoder_model_types <= {"", "clip"}:
            # The declared window still feeds the studio warning, but encoders
            # outside the CLIP family need their own embedding recipe (Qwen3
            # concatenates hidden layers), so manual chunking would corrupt
            # the conditioning. Let diffusers encode it.
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
        if prompt_cache is not None:
            prompt_cache.entry = (cache_key, result)
        return result

    async def generate(
        self, manifest: Manifest, params: dict, progress: ProgressFn,
        *, input_image: bytes | None = None,
        cancelled: Callable[[], bool] | None = None,
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
                                                     progress, loop, input_image, cancelled)
            except self.torch.OutOfMemoryError:
                pass  # retry outside: the live traceback pins failed tensors
            except Cancelled:
                # Stopped on request, not broken: the resident pipeline is
                # exactly as sound as it was before the abandoned call.
                raise
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
                                                     progress, loop, input_image, cancelled)
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
                                                 progress, loop, input_image, cancelled)

    def _generate(self, manifest: Manifest, params: dict, progress: ProgressFn,
                  loop: asyncio.AbstractEventLoop,
                  input_image: bytes | None = None,
                  cancelled: Callable[[], bool] | None = None) -> GeneratedImage:
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
            if cancelled is not None and cancelled():
                raise Cancelled()
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
        reject_degenerate_output(image, manifest.id)
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

    def _generate_i2i(self, manifest: Manifest, params: dict, progress: ProgressFn,
                      loop: asyncio.AbstractEventLoop,
                      input_image: bytes | None,
                      cancelled: Callable[[], bool] | None = None) -> GeneratedImage:
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
        size_kwargs: dict[str, int] = {}
        if width and height:
            source = source.resize((int(width), int(height)), Image.Resampling.LANCZOS)
            # SD and SDXL img2img take the output size from the source image and
            # accept no width/height at all. Newer DiT pipelines default to their
            # training resolution instead (SanaSprint to 1024), so resizing the
            # source is not enough: without these they upsize the request back to
            # 1024 and a 512 canvas frame allocates 4x the activations it should.
            if "width" in inspect.signature(pipeline.__call__).parameters:
                size_kwargs = {"width": int(width), "height": int(height)}
        steps = max(1, int(params.get("steps", 2)))
        strength = min(max(float(params.get("strength", 0.75)), 0.05), 1.0)
        # diffusers img2img floors the step count: int(steps * strength).
        actual_steps = max(1, int(steps * strength))
        generator = None
        if params.get("seed") is not None:
            generator = self.torch.Generator(self.device).manual_seed(int(params["seed"]))

        def on_step(pipe: Any, step: int, timestep: Any, kwargs: dict) -> dict:
            if cancelled is not None and cancelled():
                raise Cancelled()
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
            **size_kwargs,
            image=source,
            num_inference_steps=steps,
            strength=strength,
            guidance_scale=float(params.get("guidance", 0.0)),
            generator=generator,
            callback_on_step_end=on_step,
        ).images[0]
        gpu_ms = int((time.monotonic() - start) * 1000)
        reject_degenerate_output(image, manifest.id)
        loop.call_soon_threadsafe(progress, 1.0)
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

    def _generate_upscale(self, manifest: Manifest, params: dict, progress: ProgressFn,
                          loop: asyncio.AbstractEventLoop,
                          input_image: bytes | None,
                          cancelled: Callable[[], bool] | None = None) -> GeneratedImage:
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
            native_scale=runtime.native_scale, progress=on_tile, cancelled=cancelled,
        )
        gpu_ms = int((time.monotonic() - start) * 1000)
        loop.call_soon_threadsafe(progress, 1.0)
        return GeneratedImage(encode_png(image), image.width, image.height, gpu_ms, load_ms)

    def _sync_cuda(self) -> None:
        if self.device != "cuda":
            return
        cuda = getattr(self.torch, "cuda", None)
        synchronize = getattr(cuda, "synchronize", None)
        if callable(synchronize):
            synchronize()

    def _timed_prompt_kwargs(
        self,
        pipeline: Any,
        manifest: Manifest,
        params: dict,
        prompt_cache: PromptCache | None,
        stages: dict[str, int] | None,
    ) -> dict[str, Any]:
        negative_prompt = params.get("negative_prompt")
        prompt_started = 0.0
        if stages is not None:
            self._sync_cuda()
            prompt_started = time.perf_counter()
        prompt_kwargs = self._prompt_kwargs(
            pipeline,
            manifest,
            str(params.get("prompt", "")),
            str(negative_prompt) if negative_prompt is not None else None,
            prompt_cache=prompt_cache,
            stages=stages,
        )
        if stages is not None:
            self._sync_cuda()
            stages["text_encode_ms"] = int(
                (time.perf_counter() - prompt_started) * 1000
            )
        return prompt_kwargs

    def _attach_stage_hooks(
        self, pipeline: Any, stages: dict[str, int],
    ) -> list[Any]:
        handles: list[Any] = []
        for name in ("adapter", "unet"):
            module = getattr(pipeline, name, None)
            pre_register = getattr(module, "register_forward_pre_hook", None)
            post_register = getattr(module, "register_forward_hook", None)
            if not callable(pre_register) or not callable(post_register):
                continue
            started_at: dict[str, float] = {}

            def before(_module: Any, _inputs: Any, *, module_name: str = name) -> None:
                self._sync_cuda()
                started_at[module_name] = time.perf_counter()

            def after(
                _module: Any, _inputs: Any, _output: Any, *,
                module_name: str = name,
            ) -> None:
                self._sync_cuda()
                elapsed = (
                    time.perf_counter()
                    - started_at.pop(module_name, time.perf_counter())
                ) * 1000
                stages[f"{module_name}_ms"] += int(elapsed)
                if module_name == "unet":
                    stages["unet_forwards"] += 1

            module_handles: list[Any] = []
            try:
                module_handles.append(pre_register(before))
                module_handles.append(post_register(after))
                handles.extend(module_handles)
            except (AttributeError, TypeError):
                for handle in module_handles:
                    remove = getattr(handle, "remove", None)
                    if callable(remove):
                        remove()
        return handles

    async def frame(
        self, manifest: Manifest, params: dict, payload: bytes,
        *, prompt_cache: PromptCache | None = None,
        profile: bool = False,
    ) -> GeneratedFrame:
        if "realtime" not in manifest.capabilities:
            raise ValueError(f"model {manifest.id} does not support realtime frames")
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
            # Same reason the GPU lock uses this: a cancelled await would release
            # the codec slot while its thread still ran, so the bound would be
            # exceeded by exactly the frames being torn down (issue #202).
            canvas = await self._run_to_completion(prepare_canvas)
        strength = min(max(float(frame_params.get("strength", 0.7)), 0.05), 1.0)
        session_key = self._session_frame_key(prompt_cache)
        return await self._batch_collector_or_create().submit(
            session_key, manifest, frame_params, canvas, strength,
            prompt_cache=prompt_cache, resolution=REALTIME_SIZE,
            profile=profile,
        )

    def _frame(
        self,
        manifest: Manifest,
        params: dict,
        canvas: Image.Image,
        strength: float,
        *,
        prompt_cache: PromptCache | None = None,
        stages: dict[str, int] | None = None,
    ) -> tuple[Image.Image, int]:
        # The GPU lock already surrounds this call. Start the clock here so
        # GeneratedFrame.gpu_ms and calibration measure the same occupancy
        # (pipeline lookup, prompt encode, preview-decoder load, diffusion).
        started = time.monotonic()
        if manifest.t2i_adapter:
            # Conditioned text-to-image: the canvas (already converted to the
            # adapter's sketch map, outside the GPU lock in frame())
            # conditions a fresh latent instead of an init image. img2img has
            # no useful middle strength here: sweeps return the line drawing
            # until 1.0, where the scene ignores it.
            pipeline = self._pipeline(
                manifest, "realtime", allow_demotion=False,
            )
            prompt_kwargs = self._timed_prompt_kwargs(
                pipeline, manifest, params, prompt_cache, stages,
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
            preview_decoder = self._preview_decoder(pipeline, manifest)
            pipeline_kwargs = {
                **prompt_kwargs,
                "image": canvas,
                "num_inference_steps": steps,
                "adapter_conditioning_scale": structure_strength,
                "guidance_scale": 0.0,
                "generator": generator,
            }
        else:
            pipeline = self._pipeline(
                manifest, "i2i", allow_demotion=False,
            )
            prompt_kwargs = self._timed_prompt_kwargs(
                pipeline, manifest, params, prompt_cache, stages,
            )
            preview_decoder = self._preview_decoder(pipeline, manifest)
            pipeline_kwargs = {
                **prompt_kwargs,
                "image": canvas,
                # Few-step img2img: diffusers runs ceil(steps * strength) steps,
                # so keep the product at one or above.
                "num_inference_steps": max(2, math.ceil(1 / strength)),
                "strength": strength,
                "guidance_scale": 0.0,
            }
        handles = (
            self._attach_stage_hooks(pipeline, stages) if stages is not None else []
        )
        try:
            image = self._render_with_preview_decoder(
                pipeline, manifest, pipeline_kwargs,
                preview_decoder=preview_decoder,
                stages=stages,
            )
        finally:
            for handle in handles:
                remove = getattr(handle, "remove", None)
                if callable(remove):
                    remove()
        gpu_ms = int((time.monotonic() - started) * 1000)
        return image, gpu_ms
