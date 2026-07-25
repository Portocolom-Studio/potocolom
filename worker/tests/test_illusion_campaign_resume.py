"""Simulate campaign runner continue/timeout/resume without GPU work."""

from __future__ import annotations

import json
from pathlib import Path

from worker.illusion_campaign import (
    CampaignEntry,
    CampaignPlan,
    build_phase_plan,
    is_completed_matching,
    main,
    run_entry,
)
from worker.illusion_experiment import git_sha


def _plan(tmp_path: Path) -> CampaignPlan:
    return CampaignPlan(
        campaign_id="sim",
        git_sha="deadbeef",
        created_at="2026-01-01T00:00:00+00:00",
        evidence_root=str(tmp_path),
        model_id="m",
        dream_model_id="d",
        entries=[],
    )


def _entry(pair_id: str = "dog_sloth") -> CampaignEntry:
    return CampaignEntry(
        entry_id=f"sim_{pair_id}",
        tier="wave1",
        profile="legacy",
        pair_id=pair_id,
        seed=2,
        flags=("--sds-objective", "legacy"),
        out_rel=f"wave1/legacy/{pair_id}/seed_2",
        priority=10,
    )


def test_skip_completed_matching_sha_and_spec(tmp_path: Path) -> None:
    from PIL import Image

    plan = _plan(tmp_path)
    entry = _entry()
    plan_identity = plan.to_json()["plan_sha"]
    out = tmp_path / entry.out_rel / "attempt_001"
    out.mkdir(parents=True)
    Image.new("RGB", (2, 2), (1, 2, 3)).save(out / "derived_1.png")
    Image.new("RGB", (2, 2), (4, 5, 6)).save(out / "derived_2.png")
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "git_sha": plan.git_sha,
                "spec_hash": entry.spec_hash(model_id="m", dream_model_id="d"),
                "plan_sha": plan_identity,
            }
        )
    )
    assert is_completed_matching(
        out, plan.git_sha, entry.spec_hash(model_id="m", dream_model_id="d"), plan_identity
    )
    result = run_entry(plan, entry, py="/bin/true", dry_run=True, plan_identity=plan_identity)
    assert result["status"] == "skipped_completed"


def test_incomplete_creates_attempt_directory(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    entry = _entry("fox_rabbit")
    out = tmp_path / entry.out_rel / "attempt_001"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"status": "running", "git_sha": "other"}))
    result = run_entry(plan, entry, py="/bin/true", dry_run=True)
    assert result["status"] == "dry_run"
    attempts = list(tmp_path.glob("wave1/legacy/fox_rabbit/seed_2/attempt_*"))
    assert attempts, "expected a new attempt directory for incomplete prior run"
    assert (tmp_path / entry.out_rel / "driver" / "attempt_002" / "status.json").is_file()


def test_dry_run_continues_after_prior_failed_marker(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first = _entry("walrus_ladybug")
    second = _entry("mountain_valley")
    # Mark first as failed incomplete
    fail_dir = tmp_path / first.out_rel / "attempt_001"
    fail_dir.mkdir(parents=True)
    (fail_dir / "manifest.json").write_text(json.dumps({"status": "failed", "error": "oom"}))
    # Second should still dry-run fine
    result = run_entry(plan, second, py="/bin/true", dry_run=True)
    assert result["status"] == "dry_run"


def test_phase_planning_keeps_wave_counts(tmp_path: Path) -> None:
    common = {"evidence_root": tmp_path, "model_id": "m", "dream_model_id": "d"}
    assert len(build_phase_plan(phase="wave1", **common).entries) == 24
    assert len(build_phase_plan(phase="wave2", **common).entries) == 16


def test_run_refuses_head_mismatch(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    data = plan.to_json()
    data["git_sha"] = "not-head"
    from worker.illusion_campaign import plan_sha

    data["plan_sha"] = plan_sha(data)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data))
    assert main(["run", "--plan", str(path)]) == 1


def test_deadline_reserve_does_not_create_optimizer_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    plan = _plan(Path("evidence"))
    plan.git_sha = git_sha()
    plan.entries = [_entry()]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan.to_json()))
    assert main(["run", "--plan", str(path), "--deadline-s", "0"]) == 0
    assert not (tmp_path / "evidence" / _entry().out_rel).exists()


def test_exit_75_is_temporary_busy(tmp_path: Path, monkeypatch) -> None:
    import worker.illusion_campaign as campaign

    class BusyProcess:
        pid = 1

        def poll(self) -> int:
            return 75

    monkeypatch.setattr(campaign.subprocess, "Popen", lambda *args, **kwargs: BusyProcess())
    monkeypatch.setattr(campaign, "_sample_telemetry", lambda: {})
    result = run_entry(_plan(tmp_path), _entry(), py="/bin/true", plan_identity="test-plan")
    assert result["status"] == "busy"
    assert result["exit_code"] == 75
