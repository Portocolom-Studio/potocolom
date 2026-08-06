"""Harness tests: manifests, resume, blind sheets, ratings gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker.illusion_experiment import (
    FINAL_PAIRS,
    PAIR_BY_ID,
    REFERENCE_PAIRS,
    SCREEN_PAIRS,
    _build_illusion_config,
    _manual_roc_auc,
    build_arg_parser,
    build_stage_blind_sheets,
    evaluate_ratings,
    is_completed_run,
    pair_margins,
    resolve_pair_prompts,
    resolve_run_out,
    run_single_experiment,
    write_manifest_atomic,
)


def test_prompt_corpus_has_oil_painting_scenes() -> None:
    assert len(FINAL_PAIRS) == 8
    assert len(SCREEN_PAIRS) == 4
    dog_pair = PAIR_BY_ID["dog_sloth"]
    assert "oil painting" in dog_pair.prompt_a
    assert "misty forest" in dog_pair.prompt_a
    assert "sloth" in dog_pair.prompt_b
    # Subjects are style-free.
    assert "oil painting" not in dog_pair.subject_a
    mtn = PAIR_BY_ID["mountain_valley"]
    assert "snowy mountain" in mtn.prompt_a
    assert "pine valley" in mtn.prompt_b
    assert len(REFERENCE_PAIRS) == 7
    assert "walrus_ladybug" not in {pair.pair_id for pair in REFERENCE_PAIRS}


def test_author_reference_recipe_is_explicit_and_frozen() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "run",
            "--pair-id",
            "giraffe_penguin_calibration",
            "--experimental-recipe",
            "author_reference",
            "--out",
            "x",
        ]
    )
    config, *_ = _build_illusion_config(args)
    assert config.experimental_recipe == "author_reference"
    assert config.sds_steps == 10_000
    assert config.sds_objective == "weighted_sds"
    assert config.sds_guidance == 60
    assert config.sds_lr == pytest.approx(1e-4)
    assert config.dream_lr == pytest.approx(3e-3)
    assert config.checkpoint_steps == (500, 2_000, 5_000, 10_000)


def test_style_oil_on_oil_corpus_does_not_double_wrap() -> None:
    pair = PAIR_BY_ID["dog_sloth"]
    for style in (None, "none", "oil"):
        subjects, effective = resolve_pair_prompts(pair, style)
        assert subjects == [pair.subject_a, pair.subject_b]
        assert effective == [pair.prompt_a, pair.prompt_b]
        assert effective[0].count("oil painting") == 1
    _subjects, pencil = resolve_pair_prompts(pair, "pencil")
    assert pencil[0] == "a detailed HB pencil sketch of a dog sitting in a misty forest"
    assert "oil painting" not in pencil[0]


def test_build_config_bakes_effective_prompts_with_style_none() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        ["run", "--pair-id", "dog_sloth", "--style", "oil", "--device", "cpu", "--out", "x"]
    )
    config, effective, subjects, style_requested = _build_illusion_config(args)
    pair = PAIR_BY_ID["dog_sloth"]
    assert effective == [pair.prompt_a, pair.prompt_b]
    assert config.prompts == [pair.prompt_a, pair.prompt_b]
    # style is baked in already so the optimizer must not re-wrap.
    assert config.style is None
    assert style_requested == "oil"
    assert subjects == [pair.subject_a, pair.subject_b]


def test_sds_guidance_defaults_and_sqrt_alias() -> None:
    parser = build_arg_parser()
    legacy = parser.parse_args(["run", "--pair-id", "dog_sloth", "--out", "x"])
    config, *_ = _build_illusion_config(legacy)
    assert legacy.sds_guidance is None
    assert config.sds_guidance == 100.0
    sqrt = parser.parse_args(
        ["run", "--pair-id", "dog_sloth", "--sqrt-timestep-anneal", "--out", "x"]
    )
    assert sqrt.sqrt_timestep_anneal is True
    alias = parser.parse_args(["run", "--pair-id", "dog_sloth", "--hifa-schedule", "--out", "x"])
    assert alias.sqrt_timestep_anneal is True


def test_manual_roc_auc_perfect_anti_perfect_and_ties() -> None:
    assert _manual_roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)
    assert _manual_roc_auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(0.0)
    assert _manual_roc_auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.5)
    # a single tie between one positive and one negative averages to 0.5
    assert _manual_roc_auc([1, 0], [0.5, 0.5]) == pytest.approx(0.5)


def test_score_tree_argparse_accepts_root_flag_and_positional() -> None:
    parser = build_arg_parser()
    flagged = parser.parse_args(["score-tree", "--root", "some/tree"])
    assert flagged.root_flag == Path("some/tree")
    positional = parser.parse_args(["score-tree", "some/tree"])
    assert positional.root == Path("some/tree")


def test_stage_blind_builds_three_separate_review_stages(tmp_path: Path) -> None:
    from PIL import Image

    run = tmp_path / "runs" / "attempt_001"
    for directory in (
        run,
        run / "ckpt_sds_10000",
        run / "ckpt_dream_round_01",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), (10, 20, 30)).save(directory / "derived_1.png")
        Image.new("RGB", (4, 4), (30, 20, 10)).save(directory / "derived_2.png")
    write_manifest_atomic(
        run / "manifest.json",
        {"status": "completed", "pair_id": "pine_chandelier", "config": {"seed": 11}},
    )
    out = tmp_path / "review"
    summary = build_stage_blind_sheets(tmp_path / "runs", out, seed=3)
    assert summary["cells"] == {"sds_end": 2, "dream_d1": 2, "final": 2}
    assert (out / "sds_end.png").is_file()
    assert (out / "dream_d1.png").is_file()
    assert (out / "final.png").is_file()
    assert len((out / "ratings.jsonl").read_text().splitlines()) == 6


def test_phase_timing_from_sds_end_only(tmp_path: Path, monkeypatch) -> None:
    import torch

    import worker.illusion_experiment as mod
    import worker.illusions as illusions
    from worker.illusions import IllusionResult, PhaseEvent

    def fake_optimize(
        config, progress=lambda fraction: None, *, on_phase=None, on_checkpoint=None, arms=None
    ):
        assert arms is None, "the unforked path must not pass arms"
        prime = torch.zeros(1, 3, 8, 8)
        derived = [torch.zeros(1, 3, 8, 8), torch.zeros(1, 3, 8, 8)]
        if on_phase is not None:
            # No mid-run sds_* image checkpoints: only the boundary events.
            on_phase(PhaseEvent(phase="sds_begin", step=0, wall_s=0.0))
            on_phase(PhaseEvent(phase="sds_end", step=500, wall_s=1.5))
            on_phase(PhaseEvent(phase="dream_begin", wall_s=1.6))
            on_phase(PhaseEvent(phase="dream_end", wall_s=2.0))
            on_phase(PhaseEvent(phase="final", primes=[prime], derived=derived, wall_s=2.0))
        return IllusionResult(
            primes=[prime],
            derived=derived,
            diagnostics={"round_robin_exposures": [0, 0], "conflict": [], "losses": []},
        )

    monkeypatch.setattr(illusions, "optimize_illusion", fake_optimize)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(mod, "model_revision", lambda *_: None)
    monkeypatch.setattr(mod, "gpu_name", lambda: None)
    monkeypatch.setattr(mod, "peak_vram_mb", lambda: None)
    monkeypatch.setattr(mod, "package_versions", lambda: {})
    monkeypatch.setattr(mod, "git_sha", lambda: "test")

    parser = build_arg_parser()
    out = tmp_path / "run"
    args = parser.parse_args(
        [
            "run",
            "--pair-id",
            "dog_sloth",
            "--type",
            "flip",
            "--device",
            "cpu",
            "--skip-clip",
            "--out",
            str(out),
        ]
    )
    assert run_single_experiment(args) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["phase_timings"]["sds_s"] == pytest.approx(1.5)
    assert manifest["phase_timings"]["sds_s"] > 0
    assert manifest["effective_prompts"] == [
        PAIR_BY_ID["dog_sloth"].prompt_a,
        PAIR_BY_ID["dog_sloth"].prompt_b,
    ]


def test_manifest_resume_skips_only_completed_with_images(tmp_path: Path) -> None:
    run = tmp_path / "run_a"
    run.mkdir()
    write_manifest_atomic(run / "manifest.json", {"status": "running", "pid": 1})
    assert is_completed_run(run) is False
    write_manifest_atomic(run / "manifest.json", {"status": "completed", "pid": 1})
    assert is_completed_run(run) is False
    from PIL import Image

    Image.new("RGB", (2, 2), (1, 2, 3)).save(run / "derived_1.png")
    Image.new("RGB", (2, 2), (4, 5, 6)).save(run / "derived_2.png")
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
    from PIL import Image

    Image.new("RGB", (2, 2), (1, 2, 3)).save(first / "derived_1.png")
    Image.new("RGB", (2, 2), (4, 5, 6)).save(first / "derived_2.png")
    write_manifest_atomic(first / "manifest.json", {"status": "completed"})
    assert resolve_run_out(requested) is None


def test_dream_arm_flag_parses_and_refuses_nonsense() -> None:
    from worker.illusion_experiment import parse_dream_arm

    off = parse_dream_arm("neg_off_indep:indep:off", "watermark")
    assert off.name == "neg_off_indep"
    assert off.dream_joint is False
    assert off.negative_prompt is None

    on = parse_dream_arm("neg_on_joint:joint:on", "watermark")
    assert on.dream_joint is True
    assert on.negative_prompt == "watermark"

    for bad in ("name:indep", "name:sideways:off", "name:indep:maybe", "../x:indep:off"):
        with pytest.raises(ValueError):
            parse_dream_arm(bad, "watermark")
    # A negative-on arm with no negative prompt would silently be a duplicate
    # of the negative-off arm, which is worse than failing.
    with pytest.raises(ValueError, match="negative-on"):
        parse_dream_arm("name:indep:on", None)


def _textured_png(path: Path) -> None:
    """A 16px checkerboard: passes the catastrophe floors a flat colour fails."""
    from PIL import Image

    image = Image.new("L", (16, 16))
    image.putdata([255 if (x // 2 + y // 2) % 2 else 0 for y in range(16) for x in range(16)])
    image.convert("RGB").save(path)


def test_forked_base_is_complete_only_when_every_arm_is(tmp_path: Path) -> None:
    from worker.illusion_experiment import degenerate_run

    base = tmp_path / "base"
    base.mkdir()
    write_manifest_atomic(
        base / "manifest.json",
        {"status": "completed", "arms": ["a", "b"]},
    )
    for arm in ("a", "b"):
        (base / f"arm_{arm}").mkdir()
    # No images yet: a forked base holds none of its own, so this must not pass.
    assert is_completed_run(base) is False

    for arm in ("a", "b"):
        for view in (1, 2):
            _textured_png(base / f"arm_{arm}" / f"derived_{view}.png")
    assert is_completed_run(base) is True
    # The catastrophe brake has to see the arms too, or every forked cell reads
    # as degenerate and the driver aborts on cell three.
    assert degenerate_run(base) is False


def test_forked_run_writes_one_manifest_and_images_per_arm(tmp_path: Path, monkeypatch) -> None:
    import torch

    import worker.illusion_experiment as mod
    import worker.illusions as illusions
    from worker.illusions import IllusionResult, PhaseEvent

    def fake_optimize(
        config, progress=lambda fraction: None, *, on_phase=None, on_checkpoint=None, arms=None
    ):
        assert arms is not None
        assert [arm.name for arm in arms] == ["neg_off_indep", "neg_on_joint"]
        prime = torch.full((1, 3, 8, 8), 0.5)
        on_phase(PhaseEvent(phase="sds_begin", step=0, wall_s=0.0))
        on_phase(PhaseEvent(phase="sds_end", step=5000, wall_s=100.0))
        results = {}
        for index, arm in enumerate(arms, start=1):
            shade = 0.1 * index
            derived = [torch.full((1, 3, 8, 8), shade), torch.full((1, 3, 8, 8), shade + 0.05)]
            on_phase(PhaseEvent(phase="dream_begin", arm=arm.name, wall_s=100.0 + index))
            on_phase(PhaseEvent(phase="dream_end", arm=arm.name, wall_s=100.5 + index))
            on_phase(
                PhaseEvent(
                    phase="final",
                    arm=arm.name,
                    primes=[prime],
                    derived=derived,
                    wall_s=100.5 + index,
                )
            )
            results[arm.name] = IllusionResult(primes=[prime], derived=derived)
        lead = results[arms[0].name]
        return IllusionResult(
            primes=lead.primes,
            derived=lead.derived,
            diagnostics={"round_robin_exposures": [0, 0], "conflict": [], "losses": []},
            arm_results=results,
        )

    monkeypatch.setattr(illusions, "optimize_illusion", fake_optimize)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(mod, "model_revision", lambda *_: None)
    monkeypatch.setattr(mod, "gpu_name", lambda: None)
    monkeypatch.setattr(mod, "peak_vram_mb", lambda: None)
    monkeypatch.setattr(mod, "package_versions", lambda: {})
    monkeypatch.setattr(mod, "git_sha", lambda: "test")

    out = tmp_path / "base"
    args = build_arg_parser().parse_args(
        [
            "run",
            "--pair-id",
            "wolf_raven",
            "--style",
            "reference_sketch",
            "--device",
            "cpu",
            "--skip-clip",
            "--negative-prompt",
            "watermark, frame",
            "--dream-arm",
            "neg_off_indep:indep:off",
            "--dream-arm",
            "neg_on_joint:joint:on",
            "--out",
            str(out),
        ]
    )
    assert run_single_experiment(args) == 0

    base = json.loads((out / "manifest.json").read_text())
    assert base["arms"] == ["neg_off_indep", "neg_on_joint"]
    assert not (out / "derived_1.png").exists(), "a forked base owns no images"
    assert base["phase_timings"]["sds_s"] == pytest.approx(100.0)

    off = json.loads((out / "arm_neg_off_indep" / "manifest.json").read_text())
    on = json.loads((out / "arm_neg_on_joint" / "manifest.json").read_text())
    for arm_manifest in (off, on):
        assert arm_manifest["status"] == "completed"
        assert arm_manifest["arms"] == []
        # Each arm reports its own timings, so the smoke can check ~40s per arm.
        assert arm_manifest["phase_timings"]["dream_s"] == pytest.approx(0.5)
    # An arm's mode and negative state must not be inherited from the base.
    assert off["dream_arm"] == "neg_off_indep"
    assert off["config"]["dream_joint"] is False
    assert off["config"]["negative_prompt"] is None
    assert on["config"]["dream_joint"] is True
    assert on["config"]["negative_prompt"] == "watermark, frame"
    # Both arms carry a rateable image pair, and the base carries none.
    for arm in ("neg_off_indep", "neg_on_joint"):
        for view in (1, 2):
            assert (out / f"arm_{arm}" / f"derived_{view}.png").is_file()
    assert is_completed_run(out) is True


def test_pair_margins_min() -> None:
    matrix = [[0.9, 0.2], [0.3, 0.8]]
    margins, score = pair_margins(matrix)
    assert margins[0] == pytest.approx(0.7)
    assert margins[1] == pytest.approx(0.5)
    assert score == pytest.approx(0.5)


def _build_gate_fixture(
    tmp_path: Path, *, drop_case_id: str | None = None
) -> tuple[Path, Path, Path]:
    """Build a fully valid 24-case blind fixture; optionally drop one rating."""
    final_root = tmp_path / "final"
    final_root.mkdir()
    answer_cases = []
    rating_rows = []
    case_id = 0
    for pair in FINAL_PAIRS:
        pair_id = pair.pair_id
        for seed in (0, 1, 2):
            legacy_dir = final_root / "legacy" / f"{pair_id}_seed{seed}"
            finalist_dir = final_root / "finalist" / f"{pair_id}_seed{seed}"
            for directory in (legacy_dir, finalist_dir):
                directory.mkdir(parents=True)
                write_manifest_atomic(
                    directory / "manifest.json",
                    {
                        "status": "completed",
                        "pair_id": pair_id,
                        "config": {"seed": seed},
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
            # Keep finalist for first 2 seeds of every pair plus both controls.
            keep_finalist = seed < 2 or pair_id in ("elephant_swan", "moose_butterfly")
            keep_legacy = seed == 0
            if cid != drop_case_id:
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
    return ratings_path, answer_path, final_root


def test_evaluate_ratings_gate(tmp_path: Path) -> None:
    ratings_path, answer_path, final_root = _build_gate_fixture(tmp_path)
    report = evaluate_ratings(ratings_path, answer_path, final_root)
    assert report["finalist_keepers"] >= 16
    assert report["pairs_two_thirds"] >= 6
    assert report["failures"] == []
    assert report["gate_pass"] is True


def test_strict_ratings_missing_case_fails(tmp_path: Path) -> None:
    ratings_path, answer_path, final_root = _build_gate_fixture(tmp_path, drop_case_id="case-05")
    report = evaluate_ratings(ratings_path, answer_path, final_root)
    assert report["gate_pass"] is False
    assert any("missing rating" in failure for failure in report["failures"])
    assert any("case-05" in failure for failure in report["failures"])


def test_clip_feature_tensor_unwraps_pooling_output() -> None:
    import torch
    from worker.illusion_experiment import _clip_feature_tensor

    class FakePooling:
        def __init__(self, pooler_output):
            self.pooler_output = pooler_output

    pooled = torch.ones(2, 4)
    assert _clip_feature_tensor(FakePooling(pooled)) is pooled
    plain = torch.zeros(3, 5)
    assert _clip_feature_tensor(plain) is plain


def test_score_run_dir_writes_sidecar_not_manifest(tmp_path, monkeypatch) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "effective_prompts": ["an oil painting of a dog", "an oil painting of a sloth"],
                "config": {"prompts": ["an oil painting of a dog", "an oil painting of a sloth"]},
            }
        )
        + "\n"
    )
    # Minimal PNGs via PIL
    from PIL import Image

    Image.new("RGB", (8, 8), (10, 20, 30)).save(run / "derived_1.png")
    Image.new("RGB", (8, 8), (40, 50, 60)).save(run / "derived_2.png")
    before = (run / "manifest.json").read_text()

    def fake_score_images_for_prompts(*args, **kwargs):
        return {"clip_pair_score": 0.5, "clip_margins": [0.1, 0.2]}

    monkeypatch.setattr(
        "worker.illusion_experiment.score_images_for_prompts", fake_score_images_for_prompts
    )
    monkeypatch.setattr(
        "worker.illusion_experiment.load_clip", lambda device="cpu": (None, None, "rev")
    )
    from worker.illusion_experiment import score_run_dir

    score_run_dir(run, device="cpu")
    assert (run / "clip_scores.json").is_file()
    assert (run / "manifest.json").read_text() == before
