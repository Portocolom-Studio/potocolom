"""manifest_history.json and the Hub lock's coverage of stored manifest bodies."""

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import huggingface_hub
from huggingface_hub import constants as hub_constants

from worker import model_store
from worker.manifests import MANIFEST_HISTORY, load_manifests
from worker.model_store import ModelStore

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "worker" / "models"
LOCK = ROOT / "worker" / "worker" / "hub_lock.json"

_spec = importlib.util.spec_from_file_location(
    "generate_hub_lock", ROOT / "scripts" / "generate-hub-lock.py")
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gen
_spec.loader.exec_module(gen)

PIN = "0" * 40
SD_TURBO = {"id": "sd-turbo", "name": "SD Turbo", "capabilities": ["text_to_image"],
            "source": "stabilityai/sd-turbo", "source_revision": "b" * 40}


def _history() -> dict:
    return json.loads(MANIFEST_HISTORY.read_text())


def test_history_covers_every_current_manifest_file():
    history = _history()
    for path in sorted(SHIPPED.glob("*.json")):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest in history, path.name
        assert history[digest]["body"] == raw.decode("utf-8"), path.name


def test_every_history_entry_has_a_body():
    for digest, entry in _history().items():
        assert isinstance(entry["body"], str) and entry["body"], digest
        assert hashlib.sha256(entry["body"].encode("utf-8")).hexdigest() == digest


def test_unpinned_and_unparseable_bodies_are_absent_from_the_lock_and_do_not_fail(tmp_path):
    unpinned = {k: v for k, v in SD_TURBO.items() if k != "source_revision"}
    history = {
        "a" * 64: {"id": "sd-turbo", "body": json.dumps(unpinned)},
        "b" * 64: {"id": None, "body": "{not json"},
    }
    assert gen.historical_manifests(history, set()) == []


def test_a_removed_bundled_file_keeps_its_pins_without_git_history(tmp_path):
    bundled = tmp_path / "models"
    shutil.copytree(SHIPPED, bundled)
    (bundled / "kromoss.json").unlink()
    history_file = tmp_path / "history.json"
    history_file.write_text(MANIFEST_HISTORY.read_text())
    kromoss = [m for m in load_manifests(str(SHIPPED)) if m.id == "kromoss"]
    kept = gen.lock_refs(gen.lock_manifests(bundled, history_file))
    assert gen.lock_refs(kromoss) <= kept


def test_a_historical_body_of_a_removed_model_is_locked(tmp_path):
    history = _history()
    old = {**SD_TURBO, "id": "gone", "source": "org/gone", "source_revision": PIN}
    history["c" * 64] = {"id": "gone", "body": json.dumps(old)}
    history_file = tmp_path / "history.json"
    history_file.write_text(json.dumps(history))
    refs = gen.lock_refs(gen.lock_manifests(SHIPPED, history_file))
    assert ("org/gone", PIN, "pipeline") in refs


def test_build_history_appends_current_files_and_keeps_existing_entries(tmp_path):
    bundled = tmp_path / "models"
    shutil.copytree(SHIPPED, bundled)
    kept = {"d" * 64: {"id": "old", "body": "{}"}}
    history_file = tmp_path / "history.json"
    history_file.write_text(json.dumps(kept))
    history = gen.build_history(False, bundled, history_file)
    assert kept.keys() <= history.keys()
    assert len(history) == len(kept) + len(list(SHIPPED.glob("*.json")))


def test_every_pin_of_a_historical_manifest_is_in_the_hub_lock():
    # Offline adoption of a model no longer bundled reads its pins from the lock,
    # so each one must be there. This fails until the lock is regenerated with
    # network access (see the WS0 report).
    lock_repos = json.loads(LOCK.read_text())["repos"]
    current_ids = {m.id for m in load_manifests(str(SHIPPED))}
    missing = set()
    for manifest in gen.historical_manifests(_history(), current_ids):
        for repo, revision, _kind in gen.lock_refs([manifest]):
            if f"{repo}@{revision}" not in lock_repos:
                missing.add(f"{repo}@{revision}")
    assert missing == set()


def _historical_sdxl_turbo():
    current_ids = {m.id for m in load_manifests(str(SHIPPED))}
    found = [m for m in gen.historical_manifests(_history(), current_ids) if m.id == "sdxl-turbo"]
    assert found, "no loadable sdxl-turbo body in the history"
    return found[-1]


def test_a_markerless_historical_cache_is_adopted_with_the_network_refused(tmp_path, monkeypatch):
    # sdxl-turbo is no longer bundled, so its historical body loads as an operator
    # manifest. Its install plan must come from the lock: with every network call
    # refused, a cache laid out as the Hub lays it out, with no marker, is adopted at scan.
    cache = tmp_path / "hub"
    monkeypatch.setattr(hub_constants, "HF_HUB_CACHE", str(cache))

    def refused(*_args, **_kwargs):
        raise RuntimeError("network refused: the lock must cover this plan")

    monkeypatch.setattr(model_store, "hub_listing", refused)
    monkeypatch.setattr(model_store, "hub_file_text", refused)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", refused)
    manifest = _historical_sdxl_turbo()
    models = tmp_path / "models"
    items, _gated = model_store.plan_items(manifest, str(models))
    assert items
    for item in items:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        if item.folder is not None and item.blob_key:
            blob = item.folder / "blobs" / item.blob_key
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.touch()
            os.truncate(blob, item.size)
            item.path.unlink(missing_ok=True)
            os.symlink(os.path.relpath(blob, item.path.parent), item.path)
        else:
            item.path.touch()
            os.truncate(item.path, item.size)
    store = ModelStore([manifest], None, str(models))
    asyncio.run(store.scan())
    assert set(store.installed) == {manifest.id}
