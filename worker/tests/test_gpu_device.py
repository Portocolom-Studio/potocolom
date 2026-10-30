import asyncio
import logging
import sys
from types import SimpleNamespace

import pytest

from worker.engine import DiffusersEngine
from worker.gpu_device import select_device
from worker.manifests import Manifest
from engine_test_support import make_engine


class _FakeCuda:
    def __init__(self, devices, arch_list, *, count=None):
        self.devices = devices
        self.arch_list = arch_list
        self.count = len(devices) if count is None else count
        self.queried = []

    def device_count(self):
        return self.count

    def get_device_properties(self, index):
        self.queried.append(index)
        return self.devices[index]

    def get_arch_list(self):
        return self.arch_list


def _torch(devices, arch_list, *, hip="6.3", count=None):
    cuda = _FakeCuda(devices, arch_list, count=count)
    return SimpleNamespace(
        cuda=cuda,
        version=SimpleNamespace(hip=hip),
    )


def _gpu(name, arch):
    return SimpleNamespace(name=name, gcnArchName=arch)


def test_selects_first_visible_rocm_gpu_with_supported_base_target(caplog):
    caplog.set_level(logging.INFO)
    torch = _torch(
        [_gpu("old card", "gfx900"), _gpu("supported card", "gfx1102:sramecc+"),
         _gpu("unsupported after selection", "gfx906")],
        ["gfx1102", "gfx1100"],
    )

    assert select_device(torch, "rocm", environ={}) == "cuda:1"
    assert torch.cuda.queried == [0, 1, 2]
    assert "supported card" in caplog.text
    assert "gfx1102" in caplog.text
    assert "old card" in caplog.text
    assert "unsupported after selection" in caplog.text


def test_all_unsupported_rocm_devices_explain_available_targets():
    torch = _torch([_gpu("first", "gfx900"), _gpu("second", "gfx906")], ["gfx1102"])

    with pytest.raises(RuntimeError, match="no visible ROCm GPU supports") as error:
        select_device(torch, "rocm", environ={})

    assert "gfx900" in str(error.value)
    assert "gfx1102" in str(error.value)


def test_rocm_without_visible_devices_refuses_startup():
    with pytest.raises(RuntimeError, match="no visible ROCm GPUs"):
        select_device(_torch([], ["gfx1102"]), "rocm", environ={})


@pytest.mark.parametrize("mask", ["HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"])
@pytest.mark.parametrize("value", ["2,1"])
def test_explicit_visibility_masks_keep_operator_mapping(mask, value, caplog):
    caplog.set_level(logging.INFO)
    torch = _torch([_gpu("logical first", "gfx900")], ["gfx1102"])

    assert select_device(torch, "rocm", environ={mask: value}) == "cuda:0"
    assert torch.cuda.queried == [0]
    assert "logical first" in caplog.text
    assert "gfx900" in caplog.text


@pytest.mark.parametrize("mask", ["HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"])
def test_empty_visibility_masks_refuse_startup(mask):
    torch = _torch([], ["gfx1102"])

    with pytest.raises(RuntimeError, match="no visible ROCm GPUs"):
        select_device(torch, "rocm", environ={mask: ""})


def test_cpu_selection_does_not_inspect_cuda():
    torch = _torch([], [], hip=None)

    assert select_device(torch, "cpu", environ={}) == "cpu"


def test_unknown_device_type_is_refused():
    with pytest.raises(ValueError, match="unknown device type"):
        select_device(_torch([], []), "unknown", environ={})


def test_rocm_configuration_requires_a_rocm_build():
    torch = _torch([_gpu("supported", "gfx1102")], ["gfx1102"], hip=None)
    with pytest.raises(RuntimeError, match="requires a ROCm build"):
        select_device(torch, "rocm", environ={})


def test_nvidia_selection_does_not_compare_gcn_architecture():
    torch = _torch([SimpleNamespace(name="NVIDIA", gcnArchName=None)], ["sm_89"], hip=None)

    assert select_device(torch, "cuda", environ={}) == "cuda:0"
    assert torch.cuda.queried == []


@pytest.mark.parametrize("supported_index", [0, 1])
def test_engine_uses_selected_device_for_threaded_pipeline_placement(monkeypatch, supported_index):
    devices = [_gpu("unsupported", "gfx1036"), _gpu("unsupported", "gfx1036")]
    devices[supported_index] = _gpu("supported", "gfx1102")
    torch = _torch(devices, ["gfx1102"])
    torch.float16 = "float16"
    torch.float32 = "float32"
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.delenv("HIP_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    engine = DiffusersEngine("rocm")
    manifest = Manifest(id="model", name="Model", capabilities=["text_to_image"])
    pipeline = SimpleNamespace(to=lambda device: device)

    async def exercise():
        return await engine._run_to_completion(engine._apply_rung, pipeline, manifest, "full")

    assert asyncio.run(exercise()) == f"cuda:{supported_index}"
    assert engine.dtype == "float16"


class _IndexedCuda:
    def __init__(self):
        self.calls = []

    def mem_get_info(self, *, device):
        self.calls.append(("mem_get_info", device))
        return (2048, 4096)

    def memory_reserved(self, *, device):
        self.calls.append(("memory_reserved", device))
        return 1024

    def synchronize(self, *, device):
        self.calls.append(("synchronize", device))

    def empty_cache(self):
        self.calls.append(("empty_cache", None))

    def device(self, device):
        self.calls.append(("device", device))
        return _DeviceContext()


class _DeviceContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _PlacementTorch:
    OutOfMemoryError = RuntimeError

    def __init__(self):
        self.cuda = _IndexedCuda()

    @staticmethod
    def device(device):
        return device


def _engine_on_second_device():
    engine = make_engine()
    engine.device = "cuda:2"
    engine.torch = _PlacementTorch()
    engine.vram_reserve_mb = 0
    return engine


def test_memory_sync_and_cache_calls_bind_device_from_inference_thread():
    engine = _engine_on_second_device()

    async def exercise():
        return await engine._run_to_completion(
            lambda: (
                engine._raw_free_vram_bytes(),
                engine._available_vram_bytes(),
                engine._sync_cuda(),
                engine._free_gpu_cache(),
            ),
        )

    asyncio.run(exercise())
    assert engine.torch.cuda.calls == [
        ("mem_get_info", "cuda:2"),
        ("memory_reserved", "cuda:2"),
        ("mem_get_info", "cuda:2"),
        ("synchronize", "cuda:2"),
        ("device", "cuda:2"),
        ("empty_cache", None),
    ]


def test_pipeline_placement_and_offload_keep_selected_index():
    engine = _engine_on_second_device()
    offload = []
    pipeline = SimpleNamespace(
        enable_model_cpu_offload=lambda **kwargs: offload.append(kwargs),
        to=lambda device: device,
    )
    manifest = Manifest(id="model", name="Model", capabilities=["text_to_image"])

    async def exercise():
        return await engine._run_to_completion(
            lambda: (
                engine._apply_rung(pipeline, manifest, "full"),
                engine._apply_rung(pipeline, manifest, "model_offload"),
            ),
        )

    placed, offloaded = asyncio.run(exercise())
    assert placed == "cuda:2"
    assert offloaded is pipeline
    assert offload == [{"gpu_id": 2}]
