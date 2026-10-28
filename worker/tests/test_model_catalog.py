import json
from pathlib import Path

import pytest

from worker.manifests import load_manifests

ROOT = Path(__file__).resolve().parents[2]
NEW_MODELS = {"moss-image-sft", "kromoss", "iris-3b", "flux3-klein"}
REMOVED_MODELS = {"dreamshaper-lcm", "sana-sprint-06b", "sd-turbo", "vega-rt",
                  "sdxl-turbo"}


def test_requested_catalog_changes_and_evaluation_visibility():
    manifests = {m.id: m for m in load_manifests(str(ROOT / "worker/models"))}
    assert NEW_MODELS <= manifests.keys()
    assert not REMOVED_MODELS & manifests.keys()
    for model_id in NEW_MODELS:
        manifest = manifests[model_id]
        assert manifest.benchmark_only
        assert manifest.capabilities == ["text_to_image"]
        assert manifest.source_revision
        assert manifest.model_offload_vram_gb == (10 if model_id == "iris-3b" else 12)
        wire = manifest.wire()
        assert "model_offload_vram_gb" not in wire
        assert "source" not in wire
        assert "source_revision" not in wire
        assert "pipeline" not in wire


def test_requested_models_are_explicit_benchmark_candidates():
    matrix = json.loads((ROOT / "scripts/benchmark-matrix.json").read_text())
    defaults = {m["id"] for m in matrix["models"] + matrix["capped_commercial"]}
    candidates = {m["id"]: m for m in matrix["candidates"]}
    assert not NEW_MODELS & defaults
    assert NEW_MODELS <= candidates.keys()
    for model_id in NEW_MODELS:
        assert len(candidates[model_id]["variants"]) == matrix["images_per_prompt"]


@pytest.mark.parametrize("filename", [
    "benchmark-matrix.json", "benchmark-matrix-best.json", "benchmark-matrix-tuned.json",
])
def test_active_benchmark_matrices_do_not_offer_removed_models(filename):
    matrix = json.loads((ROOT / "scripts" / filename).read_text())
    offered = {
        model["id"]
        for section in ("models", "capped_commercial", "candidates")
        for model in matrix.get(section, [])
    }
    assert not REMOVED_MODELS & offered
    # One matrix lists excluded rows, the other two map an id to its reason.
    # A retired id belongs in neither shape.
    excluded = matrix.get("excluded", {})
    excluded_ids = (
        {row["id"] for row in excluded} if isinstance(excluded, list) else set(excluded)
    )
    assert not REMOVED_MODELS & excluded_ids
