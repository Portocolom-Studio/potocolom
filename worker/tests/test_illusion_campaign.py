"""Tests for campaign plan counts and GPU-lock parsing helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from worker.illusion_campaign import (
    build_full_plan,
    build_pilot_wave1,
    build_pilot_wave2,
    plan_counts,
)


def test_wave_counts() -> None:
    assert len(build_pilot_wave1()) == 24
    assert len(build_pilot_wave2(["--sds-objective", "legacy"])) == 16


def test_full_plan_dry_run_bounds(tmp_path: Path) -> None:
    plan = build_full_plan(
        evidence_root=tmp_path / "v3",
        model_id="m",
        dream_model_id="d",
        include_away=True,
    )
    counts = plan_counts(plan)
    assert counts["wave1"] == 24
    assert counts["wave2"] == 16
    away = sum(v for k, v in counts.items() if k.startswith("tier"))
    assert away <= 184
    hashes = [e.spec_hash() for e in plan.entries]
    assert len(hashes) == len(set(hashes))


def test_gpu_lock_parse_rocm_format() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "gpu-lock.sh"
    # Source the parse function
    raw = "============================ ROCm System Management Interface ============================\nGPU[0]\t\t\t: GPU use (%): 7\nGPU[1]\t\t\t: GPU use (%): 42\n"
    out = subprocess.check_output(
        ["bash", "-c", f'source "{script}"; parse_rocm_gpu_use_pct "$1"', "_", raw],
        text=True,
    ).strip()
    assert out == "42"


def test_encode_latent_sample_ignores_sds_generator() -> None:
    """Legacy path must call posterior.sample() with no generator arg."""
    import torch
    from types import SimpleNamespace

    from worker.illusions import DiffusionAdapter

    adapter = object.__new__(DiffusionAdapter)
    adapter.device = "cpu"
    adapter.dtype = torch.float32
    adapter.encode_batch_sizes = []
    adapter.backward_before_next_encode = []
    adapter._encodes_since_backward = 0

    calls: list[dict] = []

    class FakePosterior:
        mean = torch.zeros(1, 4, 2, 2)
        std = torch.ones(1, 4, 2, 2) * 0.1

        def sample(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return self.mean

    class FakeVae:
        config = SimpleNamespace(scaling_factor=0.18215)

        def encode(self, scaled):
            return SimpleNamespace(latent_dist=FakePosterior())

    adapter.pipe = SimpleNamespace(vae=FakeVae())
    image = torch.rand(1, 3, 16, 16)
    gen = torch.Generator().manual_seed(0)
    DiffusionAdapter.encode_latent(adapter, image, generator=gen)
    assert calls and calls[0]["args"] == () and calls[0]["kwargs"] == {}
