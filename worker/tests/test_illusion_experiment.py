"""Harness tests: manifests, resume, blind sheets, ratings gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.illusion_experiment import (
    FINAL_PAIRS,
    SCREEN_PAIRS,
    evaluate_ratings,
    is_completed_run,
    pair_margins,
    resolve_run_out,
    write_manifest_atomic,
)


def test_prompt_corpus_has_oil_painting_scenes() -> None:
    assert len(FINAL_PAIRS) == 8
    assert len(SCREEN_PAIRS) == 4
    dog_pair = next(pair for pair in FINAL_PAIRS if pair[0] == "dog_sloth")
    assert "oil painting" in dog_pair[1]
    assert "misty forest" in dog_pair[1]
    assert "sloth" in dog_pair[2]
    mtn = next(pair for pair in FINAL_PAIRS if pair[0] == "mountain_valley")
    assert "snowy mountain" in mtn[1]
    assert "pine valley" in mtn[2]


def test_manifest_resume_skips_only_completed_with_images(tmp_path: Path) -> None:
    run = tmp_path / "run_a"
    run.mkdir()
    write_manifest_atomic(run / "manifest.json", {"status": "running", "pid": 1})
    assert is_completed_run(run) is False
    write_manifest_atomic(run / "manifest.json", {"status": "completed", "pid": 1})
    assert is_completed_run(run) is False
    (run / "derived_1.png").write_bytes(b"x")
    (run / "derived_2.png").write_bytes(b"x")
    assert is_completed_run(run) is True


def test_allocate_run_dir_preserves_incomplete(tmp_path: Path) -> None:
    requested = tmp_path / "exp"
    first = resolve_run_out(requested)
    assert first == requested
    assert first is not None
    first.mkdir()
    write_manifest_atomic(first / "manifest.json", {"status": "failed", "error": "oom"})
    second = resolve_run_out(requested)
    assert second == tmp_path / "exp_attempt_1"
    (first / "derived_1.png").write_bytes(b"x")
    (first / "derived_2.png").write_bytes(b"x")
    write_manifest_atomic(first / "manifest.json", {"status": "completed"})
    assert resolve_run_out(requested) is None


def test_pair_margins_min() -> None:
    matrix = [[0.9, 0.2], [0.3, 0.8]]
    margins, score = pair_margins(matrix)
    assert margins[0] == pytest.approx(0.7)
    assert margins[1] == pytest.approx(0.5)
    assert score == pytest.approx(0.5)


def test_evaluate_ratings_gate(tmp_path: Path) -> None:
    final_root = tmp_path / "final"
    final_root.mkdir()
    answer_cases = []
    rating_rows = []
    case_id = 0
    # Build 24 cases with enough keepers to approach the gate; create stub manifests.
    for pair_id, _, _ in FINAL_PAIRS:
        for seed in (0, 1, 2):
            legacy_dir = final_root / "legacy" / f"{pair_id}_seed{seed}"
            finalist_dir = final_root / "finalist" / f"{pair_id}_seed{seed}"
            for directory in (legacy_dir, finalist_dir):
                directory.mkdir(parents=True)
                write_manifest_atomic(
                    directory / "manifest.json",
                    {
                        "status": "completed",
                        "phase_timings": {"total_s": 900.0},
                        "error": None,
                    },
                )
                (directory / "derived_1.png").write_bytes(b"x")
                (directory / "derived_2.png").write_bytes(b"x")
            cid = f"case-{case_id:02d}"
            answer_cases.append(
                {
                    "case_id": cid,
                    "pair_id": pair_id,
                    "seed": seed,
                    "column_a": "legacy",
                    "column_b": "finalist",
                    "legacy_dir": str(legacy_dir),
                    "finalist_dir": str(finalist_dir),
                }
            )
            # Keep finalist for first 2 seeds of every pair (16/24) and both controls.
            keep_finalist = seed < 2 or pair_id in ("elephant_swan", "moose_butterfly")
            keep_legacy = seed == 0
            rating_rows.append(
                {
                    "case_id": cid,
                    "keep_a": keep_legacy,
                    "keep_b": keep_finalist,
                    "notes": "",
                }
            )
            case_id += 1

    answer_path = tmp_path / "answer_key.json"
    ratings_path = tmp_path / "ratings.jsonl"
    answer_path.write_text(json.dumps({"cases": answer_cases}) + "\n")
    ratings_path.write_text("\n".join(json.dumps(row) for row in rating_rows) + "\n")
    report = evaluate_ratings(ratings_path, answer_path, final_root)
    assert report["finalist_keepers"] >= 16
    assert report["pairs_two_thirds"] >= 6
    assert "gate_pass" in report
