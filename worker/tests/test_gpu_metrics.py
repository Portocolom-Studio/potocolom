from worker.gpu_metrics import _parse_rocm_metrics, sample_gpu


def test_sample_gpu_cpu_is_unavailable(monkeypatch):
    monkeypatch.setattr("worker.gpu_metrics._torch_vram_bytes", lambda: None)
    snapshot = sample_gpu("cpu")
    assert snapshot["device"] == "cpu"
    assert snapshot["available"] is False


def test_sample_gpu_merges_torch_vram(monkeypatch):
    monkeypatch.setattr("worker.gpu_metrics.shutil.which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr("worker.gpu_metrics._nvidia_metrics", lambda *_args: {"util_pct": 42})
    monkeypatch.setattr("worker.gpu_metrics._torch_vram_bytes", lambda *_args: (4 * 1024**3, 8 * 1024**3))
    snapshot = sample_gpu("cuda")
    assert snapshot["util_pct"] == 42
    assert snapshot["vram_used_bytes"] == 4 * 1024**3
    assert snapshot["vram_total_bytes"] == 8 * 1024**3
    assert snapshot["vram_used_pct"] == 50
    assert snapshot["available"] is True


def test_sample_gpu_never_raises_when_collectors_fail(monkeypatch):
    monkeypatch.setattr("worker.gpu_metrics.shutil.which", lambda name: "/usr/bin/rocm-smi")
    monkeypatch.setattr(
        "worker.gpu_metrics._rocm_metrics",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("collector failed")),
    )
    monkeypatch.setattr(
        "worker.gpu_metrics._torch_vram_bytes",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("torch failed")),
    )
    assert sample_gpu("rocm") == {
        "device": "rocm",
        "available": False,
    }


def test_rocm_metrics_parses_combined_json():
    captured = """{
  "card0": {
    "GPU use (%)": "73",
    "Temperature (Sensor edge) (C)": "61.0",
    "Average Graphics Package Power (W)": "88.0",
    "VRAM Total Memory (B)": "17163091968",
    "VRAM Total Used Memory (B)": "8589934592"
  }
}"""
    metrics = _parse_rocm_metrics(captured)
    assert metrics["util_pct"] == 73
    assert metrics["temperature_c"] == 61.0
    assert metrics["power_w"] == 88.0
    assert metrics["vram_used_bytes"] == 8589934592
    assert metrics["vram_total_bytes"] == 17163091968


def test_rocm_metrics_regex_fallback_parses_combined_text():
    captured = """GPU[0] : GPU use (%): 73
GPU[0] : Temperature (Sensor edge) (C): 61.0
GPU[0] : Average Graphics Package Power (W): 88.0
GPU[0] : VRAM Total Memory (B): 17163091968
GPU[0] : VRAM Total Used Memory (B): 8589934592"""
    metrics = _parse_rocm_metrics(captured)
    assert metrics == {
        "util_pct": 73,
        "temperature_c": 61.0,
        "power_w": 88.0,
        "vram_used_bytes": 8589934592,
        "vram_total_bytes": 17163091968,
    }


def test_rocm_metrics_regex_fallback_parses_existing_per_flag_text():
    captured = """GPU[0] : GPU use (%): 73
Temperature (Sensor edge) (C): 61.0
Average Graphics Package Power (W): 88.0
VRAM Total Memory (B): 17163091968
VRAM Total Used Memory (B): 8589934592"""
    metrics = _parse_rocm_metrics(captured)
    assert metrics["util_pct"] == 73
    assert metrics["temperature_c"] == 61.0
    assert metrics["power_w"] == 88.0
    assert metrics["vram_used_bytes"] == 8589934592
    assert metrics["vram_total_bytes"] == 17163091968


def test_rocm_metrics_uses_one_combined_call(monkeypatch):
    calls = []

    def fake_run(command):
        calls.append(command)
        return ""

    monkeypatch.setattr("worker.gpu_metrics._run_smi", fake_run)
    from worker.gpu_metrics import _rocm_metrics

    assert _rocm_metrics("0000:02:00.0") == {}
    assert len(calls) == 1
    assert calls[0][-1] == "--json"


def test_rocm_gpu0_vram_parses_discrete_gpu():
    from worker.gpu_metrics import _rocm_gpu0_vram_bytes

    text = """GPU[0]		: VRAM Total Memory (B): 17163091968
GPU[0]		: VRAM Total Used Memory (B): 12375912448
GPU[1]		: VRAM Total Memory (B): 536870912
GPU[1]		: VRAM Total Used Memory (B): 32620544"""
    used, total = _rocm_gpu0_vram_bytes(text)
    assert used == 12375912448
    assert total == 17163091968


def test_torch_memory_query_uses_selected_index(monkeypatch):
    calls = []
    torch = type("Torch", (), {
        "cuda": type("Cuda", (), {
            "is_available": staticmethod(lambda: True),
            "mem_get_info": staticmethod(
                lambda device=None: (calls.append(device) or (2048, 4096))
            ),
        })(),
    })()
    monkeypatch.setitem(__import__("sys").modules, "torch", torch)

    from worker.gpu_metrics import _torch_vram_bytes

    assert _torch_vram_bytes("cuda:1") == (2048, 4096)
    assert calls == ["cuda:1"]


def test_nvidia_metrics_selects_matching_pci_row_and_parses_multiline_csv(monkeypatch):
    from worker.gpu_metrics import _nvidia_metrics

    monkeypatch.setattr("worker.gpu_metrics._run_smi", lambda _command: (
        "00000000:01:00.0, 11, 41, 90.0, 100, 8000\n"
        "00000000:02:00.0, 73, 62, 125.5, 2048, 16384\n"
    ))

    metrics = _nvidia_metrics("0000:02:00.0")

    assert metrics == {
        "util_pct": 73,
        "temperature_c": 62.0,
        "power_w": 125.5,
        "vram_used_bytes": 2048 * 1024 * 1024,
        "vram_total_bytes": 16384 * 1024 * 1024,
    }


def test_sample_gpu_maps_selected_torch_index_to_physical_nvidia_card(monkeypatch):
    from types import SimpleNamespace
    import sys

    calls = []
    torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_properties=lambda device: (
                calls.append(("properties", device)) or SimpleNamespace(
                    pci_domain_id=0, pci_bus_id=2, pci_device_id=0,
                )
            ),
            mem_get_info=lambda device=None: (
                calls.append(("memory", device)) or (100, 1000)
            ),
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr("worker.gpu_metrics.shutil.which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr("worker.gpu_metrics._run_smi", lambda _command: (
        "00000000:01:00.0, 11, 41, 90.0, 100, 8000\n"
        "00000000:02:00.0, 73, 62, 125.5, 2048, 16384\n"
    ))

    snapshot = sample_gpu("cuda", "cuda:1")

    assert snapshot["util_pct"] == 73
    assert snapshot["temperature_c"] == 62.0
    assert calls == [("properties", "cuda:1"), ("memory", "cuda:1")]


def test_rocm_metrics_selects_card_by_pci_identity():
    from worker.gpu_metrics import _parse_rocm_metrics

    captured = """{
  "card0": {"PCI Bus": "0000:01:00.0", "GPU use (%)": "11",
    "Temperature (Sensor edge) (C)": "41", "VRAM Total Memory (B)": "8000",
    "VRAM Total Used Memory (B)": "100"},
  "card1": {"PCI Bus": "0000:02:00.0", "GPU use (%)": "73",
    "Temperature (Sensor edge) (C)": "62", "VRAM Total Memory (B)": "16000",
    "VRAM Total Used Memory (B)": "2000"}
}"""

    metrics = _parse_rocm_metrics(captured, selected_pci="0000:02:00.0")

    assert metrics["util_pct"] == 73
    assert metrics["temperature_c"] == 62.0
    assert metrics["vram_used_bytes"] == 2000
    assert metrics["vram_total_bytes"] == 16000


def test_vendor_metrics_are_unknown_when_selected_identity_cannot_be_proven(monkeypatch):
    from worker.gpu_metrics import _nvidia_metrics

    monkeypatch.setattr("worker.gpu_metrics._run_smi", lambda _command: (
        "00000000:01:00.0, 11, 41, 90.0, 100, 8000\n"
    ))

    assert _nvidia_metrics(None) == {}
