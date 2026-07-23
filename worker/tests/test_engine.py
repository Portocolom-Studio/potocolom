import asyncio
import io
from unittest.mock import MagicMock, patch

from PIL import Image

from worker.engine import CALIBRATION_SAMPLES, DiffusersEngine, SimulatedEngine
from worker.manifests import Manifest, SIMULATED_MANIFEST


def test_simulated_gpu_lifecycle():
    engine = SimulatedEngine(0.04)

    async def scenario():
        assert engine.loaded_models() == []
        load_ms = await engine.load_model(SIMULATED_MANIFEST)
        assert load_ms >= 0
        assert engine.loaded_models() == ["sd-sim"]
        await engine.unload_all()
        assert engine.loaded_models() == []

    asyncio.run(scenario())


def test_simulated_generate_with_input_image():
    engine = SimulatedEngine(0.01)
    buffer = io.BytesIO()
    Image.new("RGB", (256, 128), (40, 80, 120)).save(buffer, "WEBP")
    input_image = buffer.getvalue()
    progress_values: list[float] = []

    async def scenario():
        result = await engine.generate(
            SIMULATED_MANIFEST, {"prompt": "blend"}, progress_values.append,
            input_image=input_image,
        )
        return result

    result = asyncio.run(scenario())
    assert progress_values[-1] == 1.0
    assert result.width == 256
    assert result.height == 128
    assert result.load_ms >= 0
    assert engine.loaded_models() == ["sd-sim"]
    assert result.data[:4] == b"RIFF"


def test_simulated_upscale_resizes_by_factor():
    engine = SimulatedEngine(0.01)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buffer, "WEBP")
    input_image = buffer.getvalue()
    manifest = Manifest(
        id="realesrgan",
        name="Real-ESRGAN",
        capabilities=["upscale"],
        parameters={
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
    )
    progress_values: list[float] = []

    async def scenario():
        return await engine.generate(
            manifest, {"factor": 4}, progress_values.append, input_image=input_image,
        )

    result = asyncio.run(scenario())
    assert result.width == 256
    assert result.height == 192
    assert progress_values[-1] == 1.0
    assert result.data[:4] == b"RIFF"


def test_simulated_upscale_requires_input():
    engine = SimulatedEngine(0.01)
    manifest = Manifest(id="realesrgan", name="Real-ESRGAN", capabilities=["upscale"])

    async def scenario():
        await engine.generate(manifest, {"factor": 2}, lambda _: None)

    try:
        asyncio.run(scenario())
    except ValueError as error:
        assert "requires input_image" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_diffusers_measured_manifests_use_free_vram():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {}
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=3 * 1024**3):
        wires = engine.measured_manifests([manifest])
    assert wires[0]["id"] == "xl"
    assert "realtime" not in wires[0]["capabilities"]


def test_measured_manifests_respect_pinned_rungs():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {"xl": "model_offload"}
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    # Plenty of free VRAM, but the loaded pipeline is pinned to an offload
    # rung: the hello must not re-advertise realtime for it.
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=64 * 1024**3):
        wire = engine.measured_manifests([manifest])
    assert "realtime" not in wire[0]["capabilities"]


def test_diffusers_effective_realtime_slots_zero_without_realtime():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {}
    engine._calibrated_slots = None
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=3 * 1024**3):
        wire = engine.measured_manifests([manifest])
        assert engine.effective_realtime_slots(wire, 2) == 0


def test_diffusers_effective_realtime_slots_uses_calibration():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {"rt": "full"}
    engine._calibrated_slots = 1
    manifest = Manifest(
        id="rt",
        name="RT",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=4,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=64 * 1024**3):
        wire = engine.measured_manifests([manifest])
        assert engine.effective_realtime_slots(wire, 2) == 1


def test_optimize_resident_skips_offload_and_survives_compile_failure():
    torch_stub = MagicMock()
    torch_stub.compile.side_effect = RuntimeError("inductor blew up")

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = True
    engine.attention_backend = "_native_efficient"

    unet = MagicMock()
    unet.set_attention_backend = MagicMock(side_effect=ValueError("no backend"))
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None

    engine._warmup_pipeline = MagicMock()
    engine._optimize_resident(pipeline, "t2i")

    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    torch_stub.compile.assert_called_once()
    engine._warmup_pipeline.assert_not_called()


def test_optimize_resident_reverts_after_warmup_failure():
    torch_stub = MagicMock()
    compiled = MagicMock(name="compiled")
    torch_stub.compile.return_value = compiled

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = True
    engine.attention_backend = "_native_efficient"

    unet = MagicMock(name="eager_unet")
    unet.set_attention_backend = MagicMock()
    unet.reset_attention_backend = MagicMock()
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock(side_effect=RuntimeError("cudagraph boom"))

    engine._optimize_resident(pipeline, "t2i")

    assert pipeline.unet is unet
    engine._warmup_pipeline.assert_called_once_with(pipeline, "t2i")
    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    unet.reset_attention_backend.assert_called_once_with()


def test_optimize_resident_warms_attention_backend_without_compile():
    torch_stub = MagicMock()
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = False
    engine.attention_backend = "_native_efficient"

    unet = MagicMock()
    unet.set_attention_backend = MagicMock()
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock()

    engine._optimize_resident(pipeline, "t2i")

    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    torch_stub.compile.assert_not_called()
    engine._warmup_pipeline.assert_called_once_with(pipeline, "t2i")


def test_optimize_resident_skipped_when_compile_disabled():
    torch_stub = MagicMock()
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = False
    engine.attention_backend = ""
    pipeline = MagicMock()
    pipeline.unet = MagicMock()
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock()

    engine._optimize_resident(pipeline, "t2i")

    torch_stub.compile.assert_not_called()
    engine._warmup_pipeline.assert_not_called()


def test_calibrate_realtime_sets_slots_from_p95():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(return_value=b"webp")

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    # Discarded pass + CALIBRATION_SAMPLES at 200 ms -> 2 slots under 500 ms.
    times = iter([0.0, 0.2] * (CALIBRATION_SAMPLES + 1))

    def fake_monotonic():
        return next(times)

    with patch("worker.engine.time.monotonic", side_effect=fake_monotonic):
        slots = engine._calibrate_realtime(manifest, configured=4)

    assert slots == 2
    assert engine._calibrated_slots == 2
    assert engine._frame.call_count == CALIBRATION_SAMPLES + 1


def test_calibrate_realtime_p95_tolerates_one_outlier():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(return_value=b"webp")

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    # Discarded pass, then 19x 200 ms and one 900 ms outlier. Nearest-rank
    # p95 of 20 is the 19th ordered value (200 ms), so slots stay 2.
    pairs = [(0.0, 0.2)]  # discarded
    pairs.extend([(0.0, 0.2)] * (CALIBRATION_SAMPLES - 1))
    pairs.append((0.0, 0.9))
    flat: list[float] = []
    for start, end in pairs:
        flat.extend([start, end])
    times = iter(flat)

    with patch("worker.engine.time.monotonic", side_effect=lambda: next(times)):
        slots = engine._calibrate_realtime(manifest, configured=4)

    assert slots == 2
    assert engine._calibrated_slots == 2


def test_calibrate_realtime_failure_advertises_zero_slots():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(side_effect=RuntimeError("hip boom"))

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    async def scenario():
        return await engine.calibrate_realtime(manifest, 4)

    slots = asyncio.run(scenario())
    assert slots == 0
    assert engine._calibrated_slots == 0


def test_evict_cold_removes_oldest_first():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._pipelines = {("a", "t2i"): object(), ("b", "t2i"): object()}
    engine._rungs = {"a": "full", "b": "full"}
    engine._last_used = {"a": 20.0, "b": 10.0}
    engine._free_gpu_cache = MagicMock()
    engine._free_vram_bytes = MagicMock(return_value=0)

    engine._evict_cold(except_model_id="a")

    assert ("a", "t2i") in engine._pipelines
    assert ("b", "t2i") not in engine._pipelines
    assert "b" not in engine._rungs


def _poison_engine(model_id: str = "m") -> DiffusersEngine:
    from worker.engine import GeneratedImage

    # CI worker venv has no torch; only OutOfMemoryError must be a real type
    # so `except self.torch.OutOfMemoryError` stays valid.
    torch_stub = MagicMock()
    torch_stub.OutOfMemoryError = type("OutOfMemoryError", (Exception,), {})

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cpu"
    engine.dtype = object()
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {(model_id, "t2i"): object()}
    engine._rungs = {model_id: "full"}
    engine._last_used = {model_id: 1.0}
    engine._poison_evicted_at = {}
    engine._poison_evict_count = {}
    engine._gpu = asyncio.Lock()
    engine._free_gpu_cache = MagicMock()
    engine._ok = GeneratedImage(b"webp", 64, 64, 1, 0)
    return engine


def test_evict_poisoned_drops_resident_and_counts():
    engine = _poison_engine()
    assert engine._evict_poisoned("m") is True
    assert engine._pipelines == {}
    assert engine._poison_evict_count["m"] == 1


def test_evict_poisoned_respects_cooldown():
    engine = _poison_engine()
    assert engine._evict_poisoned("m") is True
    engine._pipelines[("m", "t2i")] = object()  # simulate a reload that failed again
    assert engine._evict_poisoned("m") is False
    assert ("m", "t2i") in engine._pipelines
    assert engine._poison_evict_count["m"] == 1


def test_generate_fails_once_then_succeeds_after_poison_evict():
    """Non-OOM error drops the resident; the next job can complete (issue #103)."""
    engine = _poison_engine()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("Input type c10::Half and bias type float")

    async def scenario():
        with patch("asyncio.to_thread", side_effect=boom):
            try:
                await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
            except RuntimeError as error:
                assert "c10::Half" in str(error)
            else:
                raise AssertionError("expected RuntimeError")
        assert engine._pipelines == {}
        assert engine._poison_evict_count["m"] == 1
        # Reload would re-populate; simulate that and a clean second pass.
        engine._pipelines[("m", "t2i")] = object()
        with patch("asyncio.to_thread", return_value=engine._ok):
            result = await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
        assert result is engine._ok

    asyncio.run(scenario())


def test_generate_value_error_does_not_evict():
    engine = _poison_engine()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    def bad_request(*_args, **_kwargs):
        raise ValueError("bad params")

    async def scenario():
        with patch("asyncio.to_thread", side_effect=bad_request):
            try:
                await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError")

    asyncio.run(scenario())
    assert ("m", "t2i") in engine._pipelines
    assert engine._poison_evict_count == {}
