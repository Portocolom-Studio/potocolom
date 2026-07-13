import json

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


def test_unknown_manifest_field_is_loud(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({**SD_TURBO, "vram": 8}))
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))
