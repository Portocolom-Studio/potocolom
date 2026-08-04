"""The review server must show enough to judge and hide enough to stay unbiased."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PIL")


def _run(tmp_path, pair, seed, joint):
    from PIL import Image

    run = tmp_path / "runs" / ("joint" if joint else "indep") / pair / f"seed_{seed}"
    (run / "ckpt_dream_round_01").mkdir(parents=True)
    (run / "ckpt_sds_0500").mkdir(parents=True)
    (run / "ckpt_sds_5000").mkdir(parents=True)
    targets = (run, run / "ckpt_dream_round_01", run / "ckpt_sds_0500", run / "ckpt_sds_5000")
    for target in targets:
        for view in (1, 2):
            Image.new("RGB", (16, 16), (9, 9, 9)).save(target / f"derived_{view}.png")
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "pair_id": pair,
                "subjects": ["a wolf howling", "a raven perched"],
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
