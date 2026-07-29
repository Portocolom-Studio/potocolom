"""Tests for campaign plan counts and GPU-lock parsing helpers."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from worker.illusion_campaign import (
    build_early_dream_backup,
    build_full_plan,
    build_pilot_wave1,
    build_pilot_wave2,
    build_reference_author_60h,
    plan_counts,
)


def test_wave_counts() -> None:
    assert len(build_pilot_wave1()) == 24
    assert len(build_pilot_wave2(["--sds-objective", "legacy"])) == 16


def test_reference_campaign_is_breadth_first_and_excludes_walrus() -> None:
    entries = build_reference_author_60h()
    assert len(entries) == 36
    assert entries[0].pair_id == "giraffe_penguin_calibration"
    assert entries[0].seed == 11
    assert {entry.seed for entry in entries} == {11, 23, 37, 53, 71, 89}
    assert "walrus_ladybug" not in {entry.pair_id for entry in entries}
    assert all("--experimental-recipe" in entry.flags for entry in entries)
    assert all(entry.estimate_s == 5280 for entry in entries)


def test_early_dream_backup_has_48_short_cells() -> None:
    entries = build_early_dream_backup()
    assert len(entries) == 48
    assert all(entry.estimate_s == 600 for entry in entries)
    assert all("--round-robin" in entry.flags for entry in entries)
    assert all("--dream-strength" in entry.flags for entry in entries)


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


def test_gpu_lock_hands_out_exactly_n_slots(tmp_path) -> None:
    """SLOTS=N admits N concurrent holders and refuses the N+1th."""
    if subprocess.run(["pgrep", "-x", "Runner.Worker"], capture_output=True).returncode == 0:
        pytest.skip("self-hosted runner active; gpu-lock refuses by design")
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "gpu-lock.sh"
    lock = tmp_path / "gpu.lock"
    env = {
        **os.environ,
        "POTOCOLOM_GPU_LOCK": str(lock),
        "POTOCOLOM_GPU_SLOTS": "2",
        "POTOCOLOM_GPU_WAIT_S": "1",
    }
    held = [
        subprocess.Popen(
            ["bash", str(script), "--", "sleep", "20"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    try:
        # Both slot files exist and are locked once the holders are up.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if lock.exists() and Path(f"{lock}.slot2").exists():
                break
            time.sleep(0.2)
        # Slot 1 keeps the original path so single-slot callers stay exclusive.
        assert lock.exists()
        assert Path(f"{lock}.slot2").exists()
        third = subprocess.run(
            ["bash", str(script), "--", "true"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert third.returncode == 75, third.stderr
        assert "busy" in third.stderr
    finally:
        for proc in held:
            proc.kill()
            proc.wait()


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


def test_window_plan_is_breadth_first_and_covers_the_prompt_axis() -> None:
    from worker.illusion_campaign import (
        WINDOW_SEEDS,
        build_window_60h,
        _window_pair_ids,
    )

    entries = build_window_60h()
    pair_ids = _window_pair_ids()

    # The curated issue #138 corpus is actually in the sweep, not just the five
    # reference pairs the earlier plan sampled.
    assert "stag_oak" in pair_ids
    assert "penguin_bat" in pair_ids
    assert len(pair_ids) >= 20

    # The rig check is first, on the one pair whose good outcome is known.
    assert entries[0].profile == "anchor"
    assert entries[0].pair_id == "giraffe_penguin_calibration"
    assert entries[0].seed == WINDOW_SEEDS[0]

    # Breadth-first: every pair gets its first seed before any gets its second,
    # so a window that ends early still answers which pairs work at all.
    sweep = [e for e in entries if e.profile in ("anchor", "sweep")]
    first_block = sweep[: len(pair_ids)]
    assert {e.pair_id for e in first_block} == set(pair_ids)
    assert {e.seed for e in first_block} == {WINDOW_SEEDS[0]}

    # No pair/seed is planned twice, and the anchor is not duplicated.
    keys = [(e.pair_id, e.seed, e.profile) for e in entries]
    assert len(keys) == len(set(keys))
    sweep_keys = [(e.pair_id, e.seed) for e in sweep]
    assert len(sweep_keys) == len(set(sweep_keys))

    # The full-budget control exists and is scheduled last.
    controls = [e for e in entries if e.profile == "budget_control_10k"]
    assert controls
    assert [e.priority for e in controls] == sorted(e.priority for e in entries)[-len(controls) :]
    assert "--sds-steps" in controls[0].flags
    assert controls[0].flags[controls[0].flags.index("--sds-steps") + 1] == "10000"

    hashes = [e.spec_hash() for e in entries]
    assert len(hashes) == len(set(hashes))
