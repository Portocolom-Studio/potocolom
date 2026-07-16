import asyncio
import io
from unittest.mock import MagicMock, patch

from PIL import Image

from worker.engine import DiffusersEngine, SimulatedEngine
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
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=3 * 1024**3):
        wire = engine.measured_manifests([manifest])
        assert engine.effective_realtime_slots(wire, 2) == 0


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
