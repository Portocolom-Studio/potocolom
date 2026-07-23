"""Simulate campaign runner continue/timeout/resume without GPU work."""

from __future__ import annotations

import json
from pathlib import Path

from worker.illusion_campaign import (
    CampaignEntry,
    CampaignPlan,
    is_completed_matching,
    run_entry,
)


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
    plan = _plan(tmp_path)
    entry = _entry()
    out = tmp_path / entry.out_rel
    out.mkdir(parents=True)
    (out / "derived_1.png").write_bytes(b"a")
    (out / "derived_2.png").write_bytes(b"b")
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "git_sha": plan.git_sha,
                "spec_hash": entry.spec_hash(),
            }
        )
    )
    assert is_completed_matching(out, plan.git_sha, entry.spec_hash())
    result = run_entry(plan, entry, py="/bin/true", dry_run=True)
    # dry_run still creates driver_status when not skipped... actually skip happens first
    assert result["status"] == "skipped_completed"


def test_incomplete_creates_attempt_directory(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    entry = _entry("fox_rabbit")
    out = tmp_path / entry.out_rel
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"status": "running", "git_sha": "other"}))
    result = run_entry(plan, entry, py="/bin/true", dry_run=True)
    assert result["status"] == "dry_run"
    attempts = list(tmp_path.glob("wave1/legacy/fox_rabbit/seed_2_attempt_*"))
    assert attempts, "expected a new attempt directory for incomplete prior run"


def test_dry_run_continues_after_prior_failed_marker(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first = _entry("walrus_ladybug")
    second = _entry("mountain_valley")
    # Mark first as failed incomplete
    fail_dir = tmp_path / first.out_rel
    fail_dir.mkdir(parents=True)
    (fail_dir / "manifest.json").write_text(json.dumps({"status": "failed", "error": "oom"}))
    # Second should still dry-run fine
    result = run_entry(plan, second, py="/bin/true", dry_run=True)
    assert result["status"] == "dry_run"
