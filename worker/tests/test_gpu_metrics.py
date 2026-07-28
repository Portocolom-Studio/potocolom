from worker.gpu_metrics import _parse_rocm_metrics, sample_gpu


def test_sample_gpu_cpu_is_unavailable(monkeypatch):
    monkeypatch.setattr("worker.gpu_metrics._torch_vram_bytes", lambda: None)
    snapshot = sample_gpu("cpu")
    assert snapshot["device"] == "cpu"
    assert snapshot["available"] is False


def test_sample_gpu_merges_torch_vram(monkeypatch):
    monkeypatch.setattr("worker.gpu_metrics.shutil.which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr("worker.gpu_metrics._nvidia_metrics", lambda: {"util_pct": 42})
    monkeypatch.setattr("worker.gpu_metrics._torch_vram_bytes", lambda: (4 * 1024**3, 8 * 1024**3))
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
        lambda: (_ for _ in ()).throw(RuntimeError("collector failed")),
    )
    monkeypatch.setattr(
        "worker.gpu_metrics._torch_vram_bytes",
        lambda: (_ for _ in ()).throw(RuntimeError("torch failed")),
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

    assert _rocm_metrics() == {}
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
