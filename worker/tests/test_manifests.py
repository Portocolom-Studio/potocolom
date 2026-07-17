import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from worker.manifests import load_manifests

SD_TURBO = {
    "id": "sd-turbo",
    "name": "SD Turbo",
    "capabilities": ["text_to_image", "realtime"],
    "min_vram_gb": 8,
    "source": "stabilityai/sd-turbo",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
}


def test_load_manifests_and_wire_shape(tmp_path):
    (tmp_path / "sd-turbo.json").write_text(json.dumps(SD_TURBO))
    manifests = load_manifests(str(tmp_path))
    assert [m.id for m in manifests] == ["sd-turbo"]
    wire = manifests[0].wire()
    assert wire["capabilities"] == ["text_to_image", "realtime"]
    assert "source" not in wire  # weight locations stay worker side


def test_empty_models_dir_is_loud(tmp_path):
    with pytest.raises(ValueError, match="no manifests"):
        load_manifests(str(tmp_path))


def test_duplicate_manifest_ids_are_loud(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(SD_TURBO))
    (tmp_path / "b.json").write_text(json.dumps({**SD_TURBO, "name": "Other"}))
    with pytest.raises(ValueError, match="duplicate manifest id"):
        load_manifests(str(tmp_path))


def test_shipped_manifests_load():
    models_dir = Path(__file__).resolve().parents[1] / "models"
    manifests = load_manifests(str(models_dir))
    ids = {m.id for m in manifests}
    assert "sdxl-hypersd" in ids
    assert "vega-rt" in ids
    assert "ssd-1b-lightning" in ids
    assert "realesrgan" in ids
    assert "realesrgan-fast" in ids
    hypersd = next(m for m in manifests if m.id == "sdxl-hypersd")
    assert hypersd.benchmark_only
    assert hypersd.scheduler == "euler-trailing"
    vega = next(m for m in manifests if m.id == "vega-rt")
    assert not vega.benchmark_only
    assert vega.scheduler == "lcm"
    assert vega.license_id == "apache-2.0"
    assert "realtime" in vega.capabilities
    assert "image_to_image" in vega.capabilities
    assert "strength" in vega.parameters["properties"]
    lightning = next(m for m in manifests if m.id == "ssd-1b-lightning")
    assert not lightning.benchmark_only
    assert lightning.scheduler == "euler-trailing"
    realesrgan = next(m for m in manifests if m.id == "realesrgan")
    assert realesrgan.capabilities == ["upscale"]
    assert realesrgan.parameters["required"] == ["factor"]
    assert realesrgan.parameters["properties"]["factor"]["enum"] == [2, 4]
    assert realesrgan.license_id == "bsd-3-clause"
    fast = next(m for m in manifests if m.id == "realesrgan-fast")
    assert fast.capabilities == ["upscale"]
    assert fast.source.endswith("realesr-general-x4v3.pth")
    assert fast.min_vram_gb == 1
    assert fast.parameters["properties"]["factor"]["enum"] == [2, 4]


def test_upscale_cannot_mix_with_diffusion(tmp_path):
    bad = {
        **SD_TURBO,
        "id": "bad-upscale",
        "capabilities": ["upscale", "text_to_image"],
    }
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="upscale cannot combine"):
        load_manifests(str(tmp_path))


def test_unknown_manifest_field_is_loud(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({**SD_TURBO, "vram": 8}))
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))
