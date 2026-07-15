from worker.gpu_metrics import sample_gpu


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


def test_rocm_metrics_parses_smi_output(monkeypatch):
    def fake_run(command):
        joined = " ".join(command)
        if "--showuse" in joined:
            return "GPU[0] : GPU use (%): 73"
        if "--showtemp" in joined:
            return "Temperature (Sensor edge) (C): 61.0"
        if "--showpower" in joined:
            return "Average Graphics Package Power (W): 88.0"
        if "--showmeminfo" in joined:
            return "VRAM Total Memory (B): 17163091968\nVRAM Total Used Memory (B): 8589934592"
        return ""

    monkeypatch.setattr("worker.gpu_metrics._run_smi", fake_run)
    monkeypatch.setattr("worker.gpu_metrics.shutil.which", lambda name: "/usr/bin/rocm-smi")
    monkeypatch.setattr("worker.gpu_metrics._torch_vram_bytes", lambda: None)
    from worker.gpu_metrics import _rocm_metrics

    metrics = _rocm_metrics()
    assert metrics["util_pct"] == 73
    assert metrics["temperature_c"] == 61.0
    assert metrics["power_w"] == 88.0
    metrics = _rocm_metrics()
    assert metrics["vram_used_bytes"] == 8589934592
    assert metrics["vram_total_bytes"] == 17163091968


def test_rocm_gpu0_vram_parses_discrete_gpu():
    from worker.gpu_metrics import _rocm_gpu0_vram_bytes

    text = """GPU[0]		: VRAM Total Memory (B): 17163091968
GPU[0]		: VRAM Total Used Memory (B): 12375912448
GPU[1]		: VRAM Total Memory (B): 536870912
GPU[1]		: VRAM Total Used Memory (B): 32620544"""
    used, total = _rocm_gpu0_vram_bytes(text)
    assert used == 12375912448
    assert total == 17163091968
