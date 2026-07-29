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
        build_window,
        _window_pair_ids,
    )

    entries = build_window()
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

    # Breadth-first: every pair gets its first seed and mode before any gets a
    # second, so a window that ends early still answers which pairs work at all.
    sweep = [e for e in entries if e.profile.startswith(("anchor", "sweep"))]
    first_block = sweep[: len(pair_ids)]
    assert {e.pair_id for e in first_block} == set(pair_ids)
    assert {e.seed for e in first_block} == {WINDOW_SEEDS[0]}
    assert all("--dream-joint" not in e.flags for e in first_block)

    # No (pair, seed, mode) is planned twice, and the anchor is not duplicated.
    keys = [(e.pair_id, e.seed, e.profile, "--dream-joint" in e.flags) for e in entries]
    assert len(keys) == len(set(keys))
    sweep_keys = [(e.pair_id, e.seed, "--dream-joint" in e.flags) for e in sweep]
    assert len(sweep_keys) == len(set(sweep_keys))

    # The full-budget control exists at the paper's budget.
    controls = [e for e in entries if e.profile == "budget_control_10k"]
    assert controls
    assert "--sds-steps" in controls[0].flags
    assert controls[0].flags[controls[0].flags.index("--sds-steps") + 1] == "10000"

    # Controls sit mid-sweep, not at the end: a short window must drop sweep
    # tail rather than the comparison the sweep is measured against.
    all_controls = [e for e in entries if "control" in e.profile]
    assert all_controls
    assert max(e.priority for e in all_controls) < max(e.priority for e in entries)

    hashes = [e.spec_hash() for e in entries]
    assert len(hashes) == len(set(hashes))


def test_shard_splits_a_plan_disjointly_and_covers_it() -> None:
    from worker.illusion_campaign import _shard, build_window

    entries = build_window()
    assert _shard("0/2") == (0, 2)
    assert _shard("2/3") == (2, 3)
    for bad in ("1", "3/3", "-1/2", "0/0"):
        with pytest.raises(Exception):
            _shard(bad)

    for count in (2, 3):
        shards = [
            [e for i, e in enumerate(entries) if i % count == index] for index in range(count)
        ]
        seen = [e.entry_id for shard in shards for e in shard]
        # Every entry runs exactly once across the shards.
        assert sorted(seen) == sorted(e.entry_id for e in entries)
        assert len(seen) == len(set(seen))
        # And the work is split about evenly.
        assert max(len(s) for s in shards) - min(len(s) for s in shards) <= 1


def test_local_snapshot_resolves_hub_ids_offline(tmp_path, monkeypatch) -> None:
    """HF_HUB_OFFLINE refuses an incomplete snapshot; a path loads off disk."""
    from worker.illusion_experiment import local_snapshot

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    repo = tmp_path / "hub" / "models--org--model"
    (repo / "snapshots" / "abc123").mkdir(parents=True)
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("abc123\n")
    assert local_snapshot("org/model") == str(repo / "snapshots" / "abc123")

    # A single snapshot with no ref still resolves unambiguously.
    solo = tmp_path / "hub" / "models--org--solo"
    (solo / "snapshots" / "deadbeef").mkdir(parents=True)
    assert local_snapshot("org/solo") == str(solo / "snapshots" / "deadbeef")

    # Two snapshots and no ref is ambiguous: the caller must name a revision.
    two = tmp_path / "hub" / "models--org--two"
    (two / "snapshots" / "aaa").mkdir(parents=True)
    (two / "snapshots" / "bbb").mkdir(parents=True)
    assert local_snapshot("org/two") == "org/two"

    # Uncached ids and existing directories pass through, so it is idempotent.
    assert local_snapshot("org/missing") == "org/missing"
    assert local_snapshot(str(tmp_path)) == str(tmp_path)
    assert local_snapshot(local_snapshot("org/model")) == local_snapshot("org/model")


def test_plan_pins_model_snapshots_so_offline_cells_can_load(tmp_path) -> None:
    from worker.illusion_campaign import build_phase_plan

    plan = build_phase_plan(
        phase="window",
        evidence_root=tmp_path,
        model_id="stable-diffusion-v1-5/stable-diffusion-v1-5",
        dream_model_id="lykon/dreamshaper-8-lcm",
    )
    # Whatever the caller passed, the plan records something a pipeline can
    # actually open with outgoing traffic disabled.
    for recorded in (plan.model_id, plan.dream_model_id):
        assert Path(recorded).is_dir(), recorded


def test_window_plan_reflects_the_pre_window_measurements() -> None:
    """The constants encode measured decisions; guard the ones that cost money."""
    from worker.illusion_campaign import (
        WINDOW_CELL_ESTIMATE_S,
        WINDOW_SDS_STEPS,
        build_window,
    )

    entries = build_window()

    # A5 and A6: Dream mode is an axis, because joint rescued crown_octopus and
    # destroyed the calibration pair. Every pair and seed must get BOTH modes,
    # so yield can be read as the better of the two.
    sweep = [e for e in entries if e.profile.startswith(("anchor", "sweep"))]
    joint = {(e.pair_id, e.seed) for e in sweep if "--dream-joint" in e.flags}
    independent = {(e.pair_id, e.seed) for e in sweep if "--dream-joint" not in e.flags}
    assert joint == independent, "every pair/seed needs both Dream modes"
    assert joint

    # Cell 1 is the rig check and must use the mode the smoke was reviewed
    # under. Leading with joint would reproduce A6's known collapse instead.
    assert entries[0].profile == "anchor"
    assert "--dream-joint" not in entries[0].flags

    # A3: quality still improved at 5000, so the budget is not cut below it.
    assert WINDOW_SDS_STEPS >= 5_000
    assert all(e.flags[e.flags.index("--sds-steps") + 1] == str(WINDOW_SDS_STEPS) for e in sweep)

    # A2: the wording with a human-approved cell behind it.
    assert all(e.style == "reference_sketch" for e in entries)

    # A4: 512px primes cost 3.3x for no gain, so no cell asks for them.
    assert all("--prime-resolution" not in e.flags for e in entries)

    # The matrix deliberately overshoots the window: breadth-first ordering means
    # the tail is what a short window drops, so planning past the deadline buys
    # optionality if the absence runs long. What must fit is everything up to and
    # including the controls, comfortably.
    assert WINDOW_CELL_ESTIMATE_S >= 1_750, "must not undercut the measured cell time"
    last_control = max(e.priority for e in entries if "control" in e.profile)
    guaranteed = sum(e.estimate_s for e in entries if e.priority <= last_control)
    assert guaranteed < 40 * 3600, f"controls only complete at {guaranteed / 3600:.1f}h"
