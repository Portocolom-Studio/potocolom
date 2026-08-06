"""The review server must show enough to judge and hide enough to stay unbiased."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PIL")


def _run(tmp_path, pair, seed, joint, *, style="reference_sketch", duplicate_dream=False):
    from PIL import Image

    run = tmp_path / "runs" / ("joint" if joint else "indep") / pair / f"seed_{seed}"
    (run / "ckpt_dream_round_01").mkdir(parents=True)
    (run / "ckpt_sds_0500").mkdir(parents=True)
    (run / "ckpt_sds_5000").mkdir(parents=True)
    targets = (run, run / "ckpt_dream_round_01", run / "ckpt_sds_0500", run / "ckpt_sds_5000")
    for index, target in enumerate(targets):
        for view in (1, 2):
            # Distinct pixels per stage unless the caller asks for the Dream=1
            # case, where dream_round_01 IS the final image.
            shade = 9 if duplicate_dream and index < 2 else 9 + index * 20 + view
            Image.new("RGB", (16, 16), (shade, shade, shade)).save(target / f"derived_{view}.png")
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "pair_id": pair,
                "subjects": ["a wolf howling", "a raven perched"],
                "style_requested": style,
                "spec_hash": f"hash_{pair}_{seed}_{joint}",
                "config": {"seed": seed, "dream_joint": joint, "sds_steps": 5000},
            }
        )
    )
    return run


def test_collect_pairs_both_views_and_resolves_stages(tmp_path) -> None:
    from worker.illusion_review import collect

    _run(tmp_path, "wolf_raven", 11, False)
    items = collect(tmp_path / "runs", ("final", "dream_d1", "sds_end"), seed=1)

    # One item per (run, stage), each carrying BOTH views: that is the unit a
    # human can actually judge an illusion from.
    assert len(items) == 3
    assert {i["stage"] for i in items} == {"final", "dream_d1", "sds_end"}
    for item in items:
        assert len(item["paths"]) == 2
        assert item["subject_a"] and item["subject_b"]

    # sds_end resolves to the LAST checkpoint, not the first.
    sds = next(i for i in items if i["stage"] == "sds_end")
    assert "ckpt_sds_5000" in sds["paths"][0]


def test_ids_are_opaque_and_stable(tmp_path) -> None:
    from worker.illusion_review import collect

    _run(tmp_path, "wolf_raven", 11, False)
    _run(tmp_path, "wolf_raven", 11, True)
    a = collect(tmp_path / "runs", ("final",), seed=1)
    b = collect(tmp_path / "runs", ("final",), seed=1)
    assert [i["id"] for i in a] == [i["id"] for i in b], "same seed must be reproducible"
    assert len({i["id"] for i in a}) == 2

    # The id must not spell out what it is, or the blinding is decorative.
    for item in a:
        for tell in ("joint", "indep", "wolf", "5000"):
            assert tell not in item["id"]


def test_incomplete_runs_are_skipped(tmp_path) -> None:
    from worker.illusion_review import collect

    run = _run(tmp_path, "wolf_raven", 11, False)
    data = json.loads((run / "manifest.json").read_text())
    data["status"] = "failed"
    (run / "manifest.json").write_text(json.dumps(data))
    assert collect(tmp_path / "runs", ("final",), seed=1) == []


def test_a_stage_that_duplicates_another_is_suppressed(tmp_path) -> None:
    """With one Dream round, dream_d1 IS final. Rating both inflates the sample."""
    from worker.illusion_review import collect

    _run(tmp_path, "wolf_raven", 11, False, duplicate_dream=True)
    items = collect(tmp_path / "runs", ("final", "dream_d1", "sds_end"), seed=1)
    assert {i["stage"] for i in items} == {"final", "sds_end"}


def test_only_final_items_ask_the_complaint_fields(tmp_path) -> None:
    from worker.illusion_review import collect, public_items

    _run(tmp_path, "wolf_raven", 11, False)
    _run(tmp_path, "koi_moon", 11, False, style="oil")
    items = collect(tmp_path / "runs", ("final", "sds_end"), seed=1)
    ask = {(i["pair_id"], i["stage"]): i["ask"] for i in items}

    # SDS-end gets the score alone: four judgments on 400 items trades score
    # quality for answers nobody needs there.
    assert ask[("wolf_raven", "sds_end")] == ["score"]
    # Pencil cannot deliver colour, so those questions are not asked at all.
    assert ask[("wolf_raven", "final")] == ["score", "frame_artifact"]
    assert ask[("koi_moon", "final")] == [
        "score",
        "frame_artifact",
        "colour_delivered",
        "colour_consistent_between_views",
    ]

    # The browser is told nothing that could prime the judgment.
    for public in public_items(items):
        assert set(public) == {"id", "subject_a", "subject_b", "ask"}


def test_forked_arms_are_separate_items(tmp_path) -> None:
    from PIL import Image

    from worker.illusion_review import collect

    base = tmp_path / "runs" / "a_forked" / "wolf_raven" / "seed_11"
    sds = base / "ckpt_sds_5000"
    sds.mkdir(parents=True)
    for view in (1, 2):
        Image.new("RGB", (16, 16), (7, 7, view)).save(sds / f"derived_{view}.png")
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "pair_id": "wolf_raven",
                "subjects": ["a wolf howling", "a raven perched"],
                "style_requested": "reference_sketch",
                "spec_hash": "base",
                "arms": ["neg_off_indep", "neg_on_joint"],
                "config": {"seed": 11, "dream_joint": False, "sds_steps": 5000},
            }
        )
    )
    for index, (arm, joint) in enumerate((("neg_off_indep", False), ("neg_on_joint", True))):
        arm_dir = base / f"arm_{arm}"
        arm_dir.mkdir()
        for view in (1, 2):
            Image.new("RGB", (16, 16), (40 + index * 10, view, 3)).save(
                arm_dir / f"derived_{view}.png"
            )
        (arm_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "pair_id": "wolf_raven",
                    "subjects": ["a wolf howling", "a raven perched"],
                    "style_requested": "reference_sketch",
                    "spec_hash": "base",
                    "dream_arm": arm,
                    "config": {
                        "seed": 11,
                        "dream_joint": joint,
                        "sds_steps": 5000,
                        "negative_prompt": "watermark" if joint else None,
                    },
                }
            )
        )

    items = collect(tmp_path / "runs", ("final", "dream_d1", "sds_end"), seed=1)
    # One final per arm, and one shared sds_end from the base they forked from.
    finals = sorted(i["arm"] for i in items if i["stage"] == "final")
    assert finals == ["neg_off_indep", "neg_on_joint"]
    sds = [i for i in items if i["stage"] == "sds_end"]
    assert len(sds) == 1 and sds[0]["arm"] == ""
    modes = {i["arm"]: i["mode"] for i in items if i["stage"] == "final"}
    assert modes == {"neg_off_indep": "indep", "neg_on_joint": "joint"}


def test_rating_row_fills_na_for_questions_it_never_asked() -> None:
    from worker.illusion_review import questions, rating_row

    item = {
        "id": "abc",
        "ask": questions("final", "reference_sketch"),
        "stage": "final",
        "arm": "neg_on_joint",
        "spec_hash": "h",
        "pair_id": "wolf_raven",
        "seed": 11,
        "mode": "joint",
        "sds_steps": 5000,
        "style": "reference_sketch",
        "negative_prompt": "watermark",
        "run_dir": "/runs/x",
    }
    row = rating_row(item, {"score": 4, "frame_artifact": "minor"})
    assert row["frame_artifact"] == "minor"
    assert row["colour_delivered"] == "na"
    assert row["colour_consistent_between_views"] == "na"
    assert row["arm"] == "neg_on_joint" and row["spec_hash"] == "h"

    colour = {**item, "ask": questions("final", "oil"), "style": "oil"}
    # Consistency is only meaningful when there was colour to disagree about.
    no_colour = rating_row(colour, {"score": 3, "colour_delivered": "no"})
    assert no_colour["colour_consistent_between_views"] == "na"
    both_views = rating_row(
        colour,
        {"score": 3, "colour_delivered": "yes", "colour_consistent_between_views": "no"},
    )
    assert both_views["colour_consistent_between_views"] == "no"

    for bad in ({"score": 9}, {"score": 1, "frame_artifact": "awful"}):
        with pytest.raises(ValueError):
            rating_row(item, bad)


def test_canonical_export_is_last_score_wins_and_keyed_for_analysis(tmp_path) -> None:
    from worker.illusion_review import canonical_ratings, duplicate_keys

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"id": "a", "score": 0, "stage": "final", "run_dir": str(tmp_path / "run_a")},
                {"id": "b", "score": 2, "stage": "sds_end", "run_dir": str(tmp_path / "run_a")},
                {"id": "a", "score": 5, "stage": "final", "run_dir": str(tmp_path / "run_a")},
            )
        )
        + "\n"
    )
    (tmp_path / "run_a").mkdir()
    (tmp_path / "run_a" / "manifest.json").write_text(
        json.dumps(
            {
                "spec_hash": "spec1",
                "dream_arm": "neg_on_indep",
                "style_requested": "oil",
                "config": {"negative_prompt": "watermark"},
            }
        )
    )

    rows = canonical_ratings(raw)
    assert len(rows) == 2, "one row per id; the re-rating replaces the first"
    scores = {row["id"]: row["score"] for row in rows}
    assert scores == {"a": 5, "b": 2}
    # The key carries style, negative state and schedule through spec_hash, so
    # an analysis cannot silently merge two different cells.
    assert {tuple(row["key"]) for row in rows} == {
        ("spec1", "final", "neg_on_indep"),
        ("spec1", "sds_end", "neg_on_indep"),
    }
    assert all(row["style"] == "oil" for row in rows)
    assert duplicate_keys(rows) == []
    assert duplicate_keys(rows + rows[:1]) == [list(rows[0]["key"])]
