"""The fleet a worker serves: bundled manifests from the image, then operator ones."""

import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from worker import manifests
from worker.manifests import load_fleet_manifests, load_manifests

ROOT = Path(__file__).resolve().parents[2]
# The upgrade check lives in the release driver, which needs no worker imports, so it is
# loaded by path here: the pydantic loader and the check run together in one process.
_release_spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release-check.py")
assert _release_spec is not None and _release_spec.loader is not None
release_check = importlib.util.module_from_spec(_release_spec)
sys.modules[_release_spec.name] = release_check
_release_spec.loader.exec_module(release_check)
PIN = "0" * 40

ALPHA = {"id": "alpha", "name": "Alpha", "capabilities": ["text_to_image"],
         "source": "org/alpha", "source_revision": PIN}
ALPHA_OLD = {**ALPHA, "name": "Alpha old", "source_revision": "b" * 40}
BETA = {"id": "beta", "name": "Beta", "capabilities": ["text_to_image"],
        "source": "org/beta", "source_revision": PIN}
GAMMA_OLD = {"id": "gamma", "name": "Gamma", "capabilities": ["text_to_image"],
             "source": "org/gamma", "source_revision": "c" * 40}
DELTA = {"id": "delta", "name": "Delta", "capabilities": ["text_to_image"],
         "source": "org/delta", "source_revision": PIN}


def _bytes(body: dict) -> bytes:
    return json.dumps(body).encode()


def _entry(body: dict) -> tuple[str, dict]:
    raw = _bytes(body)
    return hashlib.sha256(raw).hexdigest(), {"id": body["id"], "body": raw.decode()}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A two-model bundle, a history that knows one old copy of alpha and gamma,
    and an empty models directory for the operator."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "alpha.json").write_bytes(_bytes(ALPHA))
    (bundled / "beta.json").write_bytes(_bytes(BETA))
    history = dict([_entry(ALPHA), _entry(ALPHA_OLD), _entry(GAMMA_OLD)])
    history_file = tmp_path / "manifest_history.json"
    history_file.write_text(json.dumps(history))
    monkeypatch.setattr(manifests, "BUNDLED_MODELS_DIR", bundled)
    monkeypatch.setattr(manifests, "MANIFEST_HISTORY", history_file)
    models = tmp_path / "models"
    models.mkdir()
    return models


def _names(loaded):
    return {manifest.id: manifest.name for manifest in loaded}


def test_stale_seed_is_ignored(fleet):
    # An earlier release's copy of a model that is still bundled must not win.
    (fleet / "alpha.json").write_bytes(_bytes(ALPHA_OLD))
    assert _names(load_fleet_manifests(str(fleet))) == {"alpha": "Alpha", "beta": "Beta"}


def test_edited_seed_wins_with_a_log_line(fleet, caplog):
    caplog.set_level(logging.INFO, logger="potocolom.worker")
    (fleet / "alpha.json").write_bytes(_bytes({**ALPHA, "name": "Alpha edited"}))
    assert _names(load_fleet_manifests(str(fleet)))["alpha"] == "Alpha edited"
    assert "operator manifest replaces the bundled manifest alpha" in caplog.text


def test_override_wins_even_when_its_hash_is_historical(fleet):
    overrides = fleet / "overrides"
    overrides.mkdir()
    (overrides / "alpha.json").write_bytes(_bytes(ALPHA_OLD))
    assert _names(load_fleet_manifests(str(fleet)))["alpha"] == "Alpha old"


def test_upgrade_check_passes_only_when_the_stale_seed_is_ignored(fleet):
    # The rehearsal seeds the previous release's alpha, which has an old pin, into the
    # volume before the upgrade. The loader must ignore it, so the upgrade check passes
    # with the candidate's pins. Without the stale-seed filter the loaded alpha carries
    # the old pin, and the check refuses it.
    (fleet / "alpha.json").write_bytes(_bytes(ALPHA_OLD))
    bundle = {"alpha.json": _bytes(ALPHA).decode(), "beta.json": _bytes(BETA).decode()}
    loaded = {m.id: m.model_dump(mode="json") for m in load_fleet_manifests(str(fleet))}
    listing = [{"worker_id": "w1", "manageable": True,
                "models": [{"id": "alpha"}, {"id": "beta"}]}]
    with patch.object(release_check, "http_json", return_value=listing), \
            patch.object(release_check, "worker_fleet", return_value=loaded):
        release_check.assert_upgraded_fleet("http://localhost:8080", Path("/install"), "p", "rocm", bundle)


def test_historical_seed_of_a_removed_model_loads_with_a_log_line(fleet, caplog):
    caplog.set_level(logging.INFO, logger="potocolom.worker")
    (fleet / "gamma.json").write_bytes(_bytes(GAMMA_OLD))
    assert _names(load_fleet_manifests(str(fleet)))["gamma"] == "Gamma"
    assert "an earlier release's manifest for gamma" in caplog.text


def test_operator_only_id_loads_beside_the_bundle(fleet):
    (fleet / "delta.json").write_bytes(_bytes(DELTA))
    assert _names(load_fleet_manifests(str(fleet))) == {
        "alpha": "Alpha", "beta": "Beta", "delta": "Delta"}


def test_duplicate_operator_ids_refuse_to_start(fleet):
    (fleet / "delta.json").write_bytes(_bytes(DELTA))
    (fleet / "delta-copy.json").write_bytes(_bytes(DELTA))
    with pytest.raises(ValueError, match="duplicate manifest id: delta"):
        load_fleet_manifests(str(fleet))


def test_override_and_top_level_with_one_id_refuse_to_start(fleet):
    (fleet / "delta.json").write_bytes(_bytes(DELTA))
    (fleet / "overrides").mkdir()
    (fleet / "overrides" / "delta.json").write_bytes(_bytes(DELTA))
    with pytest.raises(ValueError, match="duplicate manifest id: delta"):
        load_fleet_manifests(str(fleet))


def test_bundled_disabled_loads_only_operator_files(fleet, monkeypatch):
    # The bundle is empty, so a stock file is an operator manifest even though
    # its hash is in the history, and a missing bundled directory is not checked.
    monkeypatch.setattr(manifests, "BUNDLED_MODELS_DIR", fleet.parent / "nowhere")
    (fleet / "alpha.json").write_bytes(_bytes(ALPHA))
    assert _names(load_fleet_manifests(str(fleet), bundled_on=False)) == {"alpha": "Alpha"}


def test_bundled_disabled_still_refuses_an_empty_directory(fleet):
    with pytest.raises(ValueError, match="no manifests found"):
        load_fleet_manifests(str(fleet), bundled_on=False)


def test_missing_models_directory_is_loud(fleet):
    # A typo in MODELS_DIR must not silently drop the operator's manifests.
    with pytest.raises(ValueError, match="does not exist"):
        load_fleet_manifests(str(fleet.parent / "typo"))


def test_missing_bundled_directory_fails_startup(fleet, monkeypatch):
    monkeypatch.setattr(manifests, "BUNDLED_MODELS_DIR", fleet.parent / "nowhere")
    with pytest.raises(ValueError, match="bundled manifests missing"):
        load_fleet_manifests(str(fleet))


def test_models_dir_equal_to_the_bundle_loads_each_file_once(fleet, monkeypatch):
    # The bundled files are current, so they would be stale seeds if scanned again.
    # The result alone cannot show a second scan (it ignores the stale copies), so
    # count the reads: each manifest file is read exactly once.
    bundled = manifests.BUNDLED_MODELS_DIR
    reads: list[str] = []
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        reads.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    assert _names(load_fleet_manifests(str(bundled))) == {"alpha": "Alpha", "beta": "Beta"}
    assert sorted(reads) == ["alpha.json", "beta.json"]


def test_shipped_bundle_loads_through_the_fleet_once_per_model():
    shipped = ROOT / "worker" / "models"
    fleet_ids = [m.id for m in load_fleet_manifests(str(shipped))]
    assert fleet_ids == sorted(m.id for m in load_manifests(str(shipped)))
    assert len(fleet_ids) == len(set(fleet_ids)) == len(list(shipped.glob("*.json")))
