import asyncio
import hashlib
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from worker import model_store
from types import SimpleNamespace

from worker.client import JobRun, SessionManager, model_busy, serve_connection
from worker.engine import SimulatedEngine
from worker.manifests import Manifest
from worker.model_store import (
    DISK_MARGIN_BYTES,
    Item,
    ModelStore,
    ModelStoreError,
    _PROBES,
    _delete_model_files,
    is_installed,
    select_plan,
    snapshot_dir,
    repo_folder,
)
from worker.categorize import SOURCE as CATEGORIZER_SOURCE, SOURCE_REVISION as CATEGORIZER_REVISION
from worker.protocol6 import Protocol6Error, canonical_bytes, decode_control, validate_message
from worker.settings import Settings

MODEL_INDEX = b'{"unet": ["diffusers", "UNet2DConditionModel"]}'
MODEL_INDEX_SIZE = len(MODEL_INDEX)
REV = "a" * 40
VAE_REV = "b" * 40
OTHER_REV = "c" * 40


def settled(manifest, models_dir=""):
    """Installed by marker, or adopted now from a cache that predates markers."""
    model_store.adopt_legacy(manifest, models_dir)
    return model_store.is_installed(manifest, models_dir)


REAL_URL_SIZE = model_store.url_size
REAL_PLAN_ITEMS = model_store.plan_items
# What the fake hub holds: (repo, revision, file, size). put_snapshot_file adds to it, and
# remote() adds a file the hub has that a test does not write locally.
REGISTRY: list[tuple[str, str, str, int]] = []


def registry_plan(manifest, models_dir, device="cuda"):
    """The plan an install would make, read from the fake hub's registry. Upscaler files are
    planned at the size they have on disk, which is what adoption checks against."""
    if "upscale" in manifest.capabilities:
        cache = model_store._upscale_dir(manifest, models_dir)
        items = []
        for url, path in model_store.upscale_targets(manifest, models_dir):
            # A file on disk is planned at its own size, which is what adoption checks. A missing
            # or empty one is planned at the size its URL declares, as an install plans it.
            on_disk = path.stat().st_size if path.is_file() else 0
            size = on_disk or model_store.url_size(url)
            items.append(model_store.Item(path=path, size=size, url=url, root=cache))
        return items, False
    items = []
    for ref in model_store.repo_refs(manifest, device):
        held = [(name, size) for repo, revision, name, size in REGISTRY
                if repo == ref.repo and revision == ref.revision]
        if ref.kind == "adapter":
            held = [entry for entry in held if entry[0] == ref.filename]
        if not held:
            # A repository the fake hub does not know is unreachable, not an empty plan.
            raise ModelStoreError("unavailable")
        for name, size in dict(held).items():
            items.append(model_store.Item(
                path=snapshot_dir(ref.repo, ref.revision) / name, size=size,
                repo=ref.repo, revision=ref.revision, name=name,
                folder=repo_folder(ref.repo), root=model_store.hub_cache_root()))
    return items, False


@pytest.fixture(autouse=True)
def offline_hub(tmp_path, monkeypatch):
    """No test reaches the network or the real hub cache."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setattr(model_store, "hub_cache_root", lambda: hub)
    monkeypatch.setattr(model_store, "hub_listing", _offline)
    monkeypatch.setattr(model_store, "url_size", _offline)
    monkeypatch.setattr(model_store, "plan_items", registry_plan)
    REGISTRY.clear()
    _PROBES.clear()
    yield hub
    _PROBES.clear()


def _offline(*_args, **_kwargs):
    raise ModelStoreError("unavailable")


def pipeline_manifest(model_id: str = "pipe-test", source: str = "org/pipe",
                      vae: str = "org/vae") -> Manifest:
    return Manifest(
        id=model_id, name=model_id, capabilities=["text_to_image"],
        source=source, source_revision=REV, vae=vae, vae_revision=VAE_REV,
    )


def remote(repo: str, revision: str, name: str, size: int = 8) -> None:
    """A file the hub has that this test does not write locally."""
    REGISTRY.append((repo, revision, name, size))


def put_snapshot_file(repo: str, revision: str, name: str, size: int = 8,
                      content: bytes | None = None) -> None:
    """Materialise one completed hub file, and register it with the fake hub at its size."""
    written = _write_snapshot_file(repo, revision, name, size, content)
    REGISTRY.append((repo, revision, name, written))


def _write_snapshot_file(repo: str, revision: str, name: str, size: int = 8,
                         content: bytes | None = None) -> int:
    """Materialise one completed hub file the way huggingface_hub lays it out. Returns its size."""
    folder = repo_folder(repo)
    digest = hashlib.sha256(f"{repo}/{revision}/{name}".encode()).hexdigest()
    blob = folder / "blobs" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    # model_index.json is read as JSON, so it carries one real component.
    if content is None:
        content = MODEL_INDEX if name == "model_index.json" else b"x" * size
    blob.write_bytes(content)
    link = snapshot_dir(repo, revision) / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(blob, link.parent))
    return len(content)


def complete_pipeline(repo: str = "org/pipe", revision: str = REV) -> None:
    put_snapshot_file(repo, revision, "model_index.json")
    put_snapshot_file(repo, revision, "unet/config.json")
    put_snapshot_file(repo, revision, "unet/diffusion_pytorch_model.fp16.safetensors")


def complete_vae() -> None:
    put_snapshot_file("org/vae", VAE_REV, "config.json")
    put_snapshot_file("org/vae", VAE_REV, "diffusion_pytorch_model.safetensors")


def complete_categorizer() -> None:
    put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "config.json")
    put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "model.safetensors")


def complete_companions() -> None:
    complete_vae()
    complete_categorizer()


def test_complete_pipeline_with_its_companion_is_installed():
    complete_pipeline()
    complete_companions()
    assert settled(pipeline_manifest(), "")


def test_pipeline_with_a_missing_weight_is_not_installed():
    remote("org/pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors")
    put_snapshot_file("org/pipe", REV, "model_index.json")
    put_snapshot_file("org/pipe", REV, "unet/config.json")
    complete_companions()
    assert not settled(pipeline_manifest(), "")


def test_a_dangling_weight_link_is_not_installed():
    complete_pipeline()
    complete_companions()
    weight = snapshot_dir("org/pipe", REV) / "unet/diffusion_pytorch_model.fp16.safetensors"
    weight.unlink()
    weight.symlink_to("../../blobs/missing")
    assert not settled(pipeline_manifest(), "")


def test_an_incomplete_download_never_counts_as_installed():
    complete_pipeline()
    complete_companions()
    folder = repo_folder("org/pipe")
    (folder / "blobs" / "deadbeef.incomplete").write_bytes(b"partial")
    assert settled(pipeline_manifest(), "")
    for link in (snapshot_dir("org/pipe", REV) / "unet").iterdir():
        if link.name.endswith(".safetensors"):
            link.unlink()
    assert not settled(pipeline_manifest(), "")


def test_missing_companion_weights_mean_not_installed():
    remote("org/vae", VAE_REV, "diffusion_pytorch_model.safetensors", 400)
    complete_pipeline()
    put_snapshot_file("org/vae", VAE_REV, "config.json")
    assert not settled(pipeline_manifest(), "")


def test_sharded_weights_need_every_shard():
    remote("org/pipe", REV, "unet/diffusion_pytorch_model-00002-of-00002.safetensors")
    complete_companions()
    put_snapshot_file("org/pipe", REV, "model_index.json")
    put_snapshot_file("org/pipe", REV, "unet/config.json")
    index = json.dumps({"weight_map": {
        "a": "diffusion_pytorch_model-00001-of-00002.safetensors",
        "b": "diffusion_pytorch_model-00002-of-00002.safetensors",
    }}).encode()
    put_snapshot_file("org/pipe", REV, "unet/diffusion_pytorch_model.safetensors.index.json",
                      content=index)
    put_snapshot_file("org/pipe", REV, "unet/diffusion_pytorch_model-00001-of-00002.safetensors")
    assert not settled(pipeline_manifest(), "")
    put_snapshot_file("org/pipe", REV, "unet/diffusion_pytorch_model-00002-of-00002.safetensors")
    assert settled(pipeline_manifest(), "")


def test_a_pipeline_component_named_in_model_index_must_exist():
    remote("org/pipe", REV, "text_encoder/config.json")
    remote("org/pipe", REV, "text_encoder/model.safetensors")
    index = json.dumps({"unet": ["diffusers", "UNet2DConditionModel"],
                        "text_encoder": ["transformers", "CLIPTextModel"]}).encode()
    put_snapshot_file("org/pipe", REV, "model_index.json", content=index)
    put_snapshot_file("org/pipe", REV, "unet/config.json")
    put_snapshot_file("org/pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors")
    complete_companions()
    assert not settled(pipeline_manifest(), "")


def test_lora_installed_only_when_its_named_file_exists():
    lora = Manifest(id="lora-test", name="L", capabilities=["text_to_image"],
                    source="org/pipe", source_revision=REV,
                    lora="org/lora/sdxl_lightning.safetensors", lora_revision=OTHER_REV)
    complete_pipeline()
    complete_categorizer()
    put_snapshot_file("org/lora", OTHER_REV, "other.safetensors")
    assert not settled(lora, "")
    put_snapshot_file("org/lora", OTHER_REV, "sdxl_lightning.safetensors")
    assert settled(lora, "")


def upscale_manifest(source: str, factors: list[int]) -> Manifest:
    return Manifest(
        id="up-test", name="U", capabilities=["upscale"], source=source,
        parameters={"properties": {"factor": {"enum": factors, "default": factors[0]}}},
    )


def test_upscaler_installed_when_its_weight_file_is_present(tmp_path):
    manifest = upscale_manifest("https://example.com/w/realesr-fast.pth", [2, 4])
    assert not settled(manifest, str(tmp_path))
    cache = tmp_path / ".cache" / "upscale" / "up-test"
    cache.mkdir(parents=True)
    (cache / "realesr-fast.pth").write_bytes(b"")
    assert not settled(manifest, str(tmp_path))
    (cache / "realesr-fast.pth").write_bytes(b"w")
    assert settled(manifest, str(tmp_path))


def test_release_upscaler_needs_a_file_for_every_factor(tmp_path):
    manifest = upscale_manifest("https://github.com/x/releases/download/v0", [2, 4])
    cache = tmp_path / ".cache" / "upscale" / "up-test"
    cache.mkdir(parents=True)
    (cache / "RealESRGAN_x2plus.pth").write_bytes(b"w")
    assert not settled(manifest, str(tmp_path))
    (cache / "RealESRGAN_x4plus.pth").write_bytes(b"w")
    assert settled(manifest, str(tmp_path))


PLAN_FILES = [
    ("model_index.json", 10),
    ("unet/config.json", 5),
    ("unet/diffusion_pytorch_model.safetensors", 100),
    ("unet/diffusion_pytorch_model.fp16.safetensors", 50),
    ("unet/diffusion_pytorch_model.bin", 90),
    ("unet/diffusion_pytorch_model.fp16.bin", 40),
    ("unet/diffusion_pytorch_model.non_ema.safetensors", 70),
    ("unet/model.onnx", 9),
    ("README.md", 3),
    (".gitattributes", 2),
    ("tokenizer/vocab.json", 4),
    ("text_encoder/model.safetensors", 30),
    ("text_encoder/model.fp16.safetensors", 15),
]


@pytest.mark.parametrize("variant, expected", [
    ("fp16", [
        ("model_index.json", 10),
        ("text_encoder/model.fp16.safetensors", 15),
        ("tokenizer/vocab.json", 4),
        ("unet/config.json", 5),
        ("unet/diffusion_pytorch_model.fp16.safetensors", 50),
    ]),
    ("", [
        ("model_index.json", 10),
        ("text_encoder/model.safetensors", 30),
        ("tokenizer/vocab.json", 4),
        ("unet/config.json", 5),
        ("unet/diffusion_pytorch_model.safetensors", 100),
    ]),
])
def test_plan_takes_the_requested_variant_and_safetensors(variant, expected):
    assert select_plan(PLAN_FILES, variant) == sorted(expected)


def test_plan_falls_back_to_the_default_when_the_variant_is_missing():
    files = [("unet/diffusion_pytorch_model.safetensors", 100), ("unet/config.json", 5)]
    assert select_plan(files, "fp16") == sorted(files)


def test_plan_keeps_every_shard_and_its_index_for_the_chosen_variant():
    files = [
        ("unet/diffusion_pytorch_model.fp16.safetensors.index.json", 1),
        ("unet/diffusion_pytorch_model.fp16-00001-of-00002.safetensors", 10),
        ("unet/diffusion_pytorch_model.fp16-00002-of-00002.safetensors", 10),
        ("unet/diffusion_pytorch_model.safetensors.index.json", 1),
        ("unet/diffusion_pytorch_model-00001-of-00002.safetensors", 10),
        ("unet/diffusion_pytorch_model-00002-of-00002.safetensors", 10),
    ]
    chosen = select_plan(files, "fp16")
    assert chosen == sorted(files[:3])


def fake_plan(*entries: tuple[str, str, str, int]):
    """plan_items stand-in: (repo, revision, name, size) entries become items."""
    items = [
        Item(path=snapshot_dir(repo, revision) / name, size=size, repo=repo,
             revision=revision, name=name, folder=repo_folder(repo))
        for repo, revision, name, size in entries
    ]
    return lambda *_args, **_kwargs: (items, False)


CATEGORIZER_ENTRIES = (
    (CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "config.json", 30),
    (CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "model.safetensors", 400),
)
B_ENTRIES = (
    ("org/b-pipe", REV, "model_index.json", MODEL_INDEX_SIZE),
    ("org/b-pipe", REV, "unet/config.json", 20),
    ("org/b-pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors", 1000),
)
PIPELINE_ENTRIES = (
    ("org/pipe", REV, "model_index.json", MODEL_INDEX_SIZE),
    ("org/pipe", REV, "unet/config.json", 20),
    ("org/pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors", 1000),
    ("org/vae", VAE_REV, "config.json", 30),
    ("org/vae", VAE_REV, "diffusion_pytorch_model.safetensors", 400),
) + CATEGORIZER_ENTRIES
PIPELINE_TOTAL = sum(entry[3] for entry in PIPELINE_ENTRIES)


def write_url_fetch(calls: list | None = None):
    """Upscaler fetch stand-in: writes the declared bytes to the item's own path."""
    def fetch(item: Item, on_bytes) -> None:
        if calls is not None:
            calls.append(item.path)
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_bytes(b"x" * item.size)
        on_bytes(item.size)
    return fetch


def write_fetch(calls: list | None = None):
    """Fetch stand-in: writes each item completely, reporting its bytes once."""
    def fetch(item: Item, on_bytes) -> None:
        if calls is not None:
            calls.append(item.name)
        put_snapshot_file(item.repo, item.revision, item.name, item.size)
        on_bytes(item.size)
    return fetch


def new_store(manifest: Manifest, models_dir: str = "") -> ModelStore:
    return ModelStore([manifest], FakeEngine(), models_dir)


async def wait_for_op(store, model_id: str, states: tuple[str, ...], ticks: int = 500) -> None:
    """Poll an operation until it reaches one of the states. A bounded wait, so a test that
    never gets there fails rather than hangs."""
    for _ in range(ticks):
        if store._ops[model_id].state in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{model_id} stayed {store._ops[model_id].state}, not {states}")


class FakeEngine(SimulatedEngine):
    def __init__(self, loaded=()):
        super().__init__(0.01)
        self.loaded = list(loaded)
        self.unloaded = []

    def loaded_models(self):
        return list(self.loaded)

    async def unload_model(self, model_id):
        self.unloaded.append(model_id)
        self.loaded = [m for m in self.loaded if m != model_id]

    def device_name(self):
        return "Test Card"


def test_size_is_refused_one_byte_under_the_margin(monkeypatch):
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, PIPELINE_TOTAL + DISK_MARGIN_BYTES - 1))
    calls: list = []
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch(calls))
    store = new_store(manifest)
    op = model_store.Operation("pipe-test")
    with pytest.raises(ModelStoreError) as refused:
        store._download(op, manifest)
    assert refused.value.reason == "disk"
    assert calls == []


def test_size_is_allowed_exactly_at_the_margin(monkeypatch):
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, PIPELINE_TOTAL + DISK_MARGIN_BYTES))
    calls: list = []
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch(calls))
    store = new_store(manifest)
    op = model_store.Operation("pipe-test")
    store._download(op, manifest)
    assert len(calls) == len(PIPELINE_ENTRIES)
    assert op.bytes_total == PIPELINE_TOTAL
    assert op.bytes_done == op.bytes_total  # shown size and written bytes agree


def test_a_cancel_keeps_the_files_it_completed(monkeypatch):
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    # Complete before the attempt: must survive a cancel of that attempt.
    put_snapshot_file("org/pipe", REV, "model_index.json", 10)
    store = new_store(manifest)
    op = model_store.Operation("pipe-test")

    def cancelling_fetch(item: Item, on_bytes) -> None:
        if item.name == "unet/config.json":
            put_snapshot_file(item.repo, item.revision, item.name, item.size)
            on_bytes(item.size)
            return
        partial = repo_folder("org/pipe") / "blobs" / "feed.incomplete"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"half")
        op.cancel.set()
        on_bytes(4)  # raises _Cancelled inside the progress hook, as the hub would

    monkeypatch.setattr(model_store, "_fetch_hub", cancelling_fetch)
    with pytest.raises(ModelStoreError) as cancelled:
        store._download(op, manifest)
    assert cancelled.value.reason == "cancelled"
    assert (snapshot_dir("org/pipe", REV) / "unet/config.json").exists()  # kept, not discarded
    # The hub resumes its own partial download, so cancelling leaves it in place.
    assert (repo_folder("org/pipe") / "blobs" / "feed.incomplete").exists()
    assert (snapshot_dir("org/pipe", REV) / "model_index.json").is_file()


def test_gated_repository_without_a_token_is_refused_before_any_fetch(monkeypatch):
    manifest = pipeline_manifest()
    items, _ = fake_plan(*PIPELINE_ENTRIES)()
    monkeypatch.setattr(model_store, "plan_items", lambda *_a, **_k: (items, True))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    calls: list = []
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch(calls))
    store = new_store(manifest)
    with pytest.raises(ModelStoreError) as refused:
        store._download(model_store.Operation("pipe-test"), manifest)
    assert refused.value.reason == "needs_token"
    assert calls == []


def test_progress_is_rate_limited_and_ends_in_done(monkeypatch):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.1)
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))

    def slow_fetch(item: Item, on_bytes) -> None:
        for step in range(1, 51):
            time.sleep(0.004)
            on_bytes(item.size * step // 50)
        put_snapshot_file(item.repo, item.revision, item.name, item.size)

    monkeypatch.setattr(model_store, "_fetch_hub", slow_fetch)
    sent: list[tuple[float, dict]] = []

    async def send(message):
        sent.append((time.monotonic(), message))

    async def scenario():
        store = new_store(manifest)
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "pipe-test", "op_id": "install-id"})
        await wait_for_op(store, "pipe-test", ("done", "failed"))
        await asyncio.sleep(0.25)
        await store.close()

    asyncio.run(scenario())
    statuses = [(at, m) for at, m in sent if m["type"] == "store_status"]
    assert len(statuses) >= 2
    gaps = [later[0] - earlier[0] for earlier, later in zip(statuses, statuses[1:])]
    assert min(gaps) >= 0.09
    assert statuses[-1][1]["downloads"][0]["state"] == "done"
    assert all(m["downloads"][0]["kind"] == "install" for _, m in statuses)
    assert all(m["downloads"][0]["op_id"] == "install-id" for _, m in statuses)
    assert any(m["type"] == "models_changed" for _at, m in sent)


def test_failure_ends_in_failed_with_a_reason(monkeypatch):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.05)
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 0))
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store = new_store(manifest)
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "pipe-test", "op_id": "disk-id"})
        await wait_for_op(store, "pipe-test", ("done", "failed"))
        await asyncio.sleep(0.2)
        await store.close()

    asyncio.run(scenario())
    final = [m for m in sent if m["type"] == "store_status"][-1]["downloads"][0]
    assert final == {"model_id": "pipe-test", "state": "failed", "bytes_done": 0,
                     "bytes_total": PIPELINE_TOTAL, "error": "disk", "op_id": "disk-id", "kind": "install"}


def test_cancelling_a_queued_install_fetches_nothing(monkeypatch):
    manifest = pipeline_manifest()
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*PIPELINE_ENTRIES))
    calls: list = []
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch(calls))

    async def scenario():
        store = new_store(manifest)
        store.attach(lambda _m: _noop(), lambda _m: False)
        store.handle({"type": "install_model", "model_id": "pipe-test"})
        store.handle({"type": "cancel_install", "model_id": "pipe-test", "op_id": "cancel-id"})
        await store.shutdown()
        return store._ops["pipe-test"]

    op = asyncio.run(scenario())
    assert (op.state, op.error, op.op_id) == ("failed", "cancelled", "cancel-id")
    assert calls == []


async def _noop():
    return None


def test_remove_keeps_a_repository_another_installed_model_reads():
    complete_pipeline("org/pipe", REV)
    complete_companions()
    complete_pipeline("org/pipe2", OTHER_REV)
    kept = pipeline_manifest("pipe2", source="org/pipe2", vae="org/vae")
    kept.source_revision = OTHER_REV
    removed = pipeline_manifest()
    weight = snapshot_dir("org/pipe", REV) / "unet/diffusion_pytorch_model.fp16.safetensors"
    blob = Path(os.path.realpath(weight))
    assert blob.is_file()
    _delete_model_files(removed, "", [kept])
    assert not snapshot_dir("org/pipe", REV).exists()
    assert not blob.exists()  # the blob under unet/ is collected, not just the top-level links
    assert snapshot_dir("org/vae", VAE_REV).is_dir()
    assert settled(kept, "")


def test_remove_is_refused_while_the_model_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.05)
    complete_pipeline()
    complete_companions()
    manifest = pipeline_manifest()
    store = new_store(manifest, str(tmp_path))
    asyncio.run(_scan(store))
    assert "pipe-test" in store.installed
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store.attach(send, lambda _m: True)
        store.handle({"type": "remove_model", "model_id": "pipe-test", "op_id": "first"})
        await asyncio.sleep(0.1)
        store.handle({"type": "remove_model", "model_id": "pipe-test", "op_id": "second"})
        await asyncio.sleep(0.2)
        await store.close()

    asyncio.run(scenario())
    assert "pipe-test" in store.installed
    assert settled(manifest, "")
    final = [m for m in sent if m["type"] == "store_status"][-1]["downloads"][0]
    assert (final["state"], final["error"], final["op_id"], final["kind"]) == ("failed", "in_use", "second", "remove")


async def _scan(store):
    await store.scan()


def test_remove_unloads_a_loaded_model_then_deletes_its_files():
    complete_pipeline()
    complete_companions()
    manifest = pipeline_manifest()
    engine = FakeEngine(loaded=["pipe-test"])
    store = ModelStore([manifest], engine, "")
    store.installed["pipe-test"] = manifest
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store.attach(send, lambda _m: False)
        store.handle({"type": "remove_model", "model_id": "pipe-test"})
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()

    asyncio.run(scenario())
    assert engine.unloaded == ["pipe-test"]
    assert "pipe-test" not in store.installed
    assert not snapshot_dir("org/pipe", REV).exists()
    models = [m for m in sent if m["type"] == "models_changed"]
    assert models and models[-1]["models"] == []


def test_hello_registers_with_no_installed_model_and_carries_the_catalog(tmp_path):
    manifest = pipeline_manifest()
    engine = FakeEngine()

    class RegisteredSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return json.dumps({"type": "registered", "model_store": True})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = RegisteredSocket()
    settings = Settings(worker_id="w-store", models_dir=str(tmp_path))
    asyncio.run(serve_connection(socket, settings, [manifest], engine))
    hello = json.loads(socket.sent[0])
    assert hello["models"] == []
    assert "model_store" in hello["capabilities"]
    assert "hardware" not in hello and "catalog" not in hello  # not in hello (N-1)
    inventory = next(json.loads(item) for item in socket.sent
                     if '"store_inventory"' in item)
    assert inventory["hardware"]["gpu_name"] == "Test Card"
    assert inventory["hardware"]["disk_free_bytes"] > 0
    (row,) = inventory["catalog"]
    assert row["id"] == "pipe-test"
    assert row["installed"] is False
    assert row["download_bytes"] is None
    assert row["needs_token"] is False
    assert "source" not in row


def test_hello_lists_installed_models_only_and_a_simulated_engine_has_no_store(tmp_path):
    complete_pipeline()
    complete_companions()
    installed = pipeline_manifest()
    missing = pipeline_manifest("not-here", source="org/other")

    class Once:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return json.dumps({"type": "registered"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = Once()
    settings = Settings(worker_id="w-store", models_dir=str(tmp_path))
    asyncio.run(serve_connection(socket, settings, [installed, missing], FakeEngine()))
    hello = json.loads(socket.sent[0])
    assert [m["id"] for m in hello["models"]] == ["pipe-test"]

    plain = Once()
    asyncio.run(serve_connection(plain, Settings(worker_id="w-plain"),
                                 [installed], SimulatedEngine(0.01)))
    simulated_hello = json.loads(plain.sent[0])
    assert "model_store" not in simulated_hello["capabilities"]
    assert "catalog" not in simulated_hello
    assert "hardware" not in simulated_hello


def test_store_messages_are_closed_shapes_for_protocol_six():
    worker_id = "w-proto"
    incarnation = uuid.uuid4()
    identity = {"worker_id": worker_id, "incarnation": str(incarnation)}
    validate_message({"type": "store_status", "downloads": [], "disk_free_bytes": 1,
                      **identity}, expected_worker_id=worker_id,
                     expected_incarnation=incarnation)
    validate_message({"type": "models_changed", "models": [], "catalog": [],
                      **identity}, expected_worker_id=worker_id,
                     expected_incarnation=incarnation)
    for kind in ("install_model", "cancel_install", "remove_model"):
        validate_message({"type": kind, "model_id": "pipe-test", **identity},
                         expected_worker_id=worker_id, expected_incarnation=incarnation)
    with pytest.raises(Protocol6Error):
        validate_message({"type": "install_model", "model_id": "x", "extra": 1,
                          **identity}, expected_worker_id=worker_id,
                         expected_incarnation=incarnation)



def test_store_status_requires_valid_kind():
    import uuid

    identity = {"worker_id": "w-shape", "incarnation": str(uuid.uuid4())}
    for kind in ("install", "remove", "cancel"):
        validate_message({"type": "store_status", "downloads": [{"kind": kind}],
                          "disk_free_bytes": 0, **identity})
    for entry in ({}, {"kind": "other"}, {"kind": None}, {"kind": []}):
        with pytest.raises(ValueError):
            validate_message({"type": "store_status", "downloads": [entry],
                              "disk_free_bytes": 0, **identity})


def test_store_operation_id_protocol_types():
    import uuid

    identity = {"worker_id": "w-shape", "incarnation": str(uuid.uuid4())}
    for value in (None, "attempt"):
        validate_message({"type": "install_model", "model_id": "x", "op_id": value, **identity})
        validate_message({"type": "store_status", "downloads": [{"op_id": value, "kind": "install"}],
                          "disk_free_bytes": 0, **identity})
    for message in ({"type": "install_model", "model_id": "x", "op_id": 42, **identity},
                    {"type": "store_status", "downloads": [{"op_id": 42, "kind": "install"}],
                     "disk_free_bytes": 0, **identity}):
        with pytest.raises(ValueError):
            validate_message(message)

def test_a_live_session_or_a_running_job_makes_a_model_busy():
    manifest = pipeline_manifest()
    sessions = SessionManager(None, FakeEngine(), {manifest.id: manifest})
    jobs: dict = {}
    busy = model_busy(sessions, jobs)
    assert not busy("pipe-test")
    sessions._runners[uuid.uuid4()] = SimpleNamespace(_manifest=manifest)
    assert busy("pipe-test")
    sessions._runners.clear()
    assert not busy("pipe-test")
    jobs[object()] = JobRun("job-1", "token", model_id="pipe-test")
    assert busy("pipe-test")
    assert not busy("another-model")


def signed(command: dict) -> dict:
    source = {key: value for key, value in command.items() if key != "body_hash"}
    return {**command, "body_hash": hashlib.sha256(canonical_bytes(source)).hexdigest()}


class ScriptedFleet:
    """A fleet API for worker tests: registers the worker, sends scripted controls and records replies.

    Each script step is (pause_seconds, build); build(incarnation) returns the control.
    Ledger commands are signed as the API signs them. After the script the stream
    waits up to three seconds for a reply that satisfies `settle_on`, then ends.
    """

    LEDGER = frozenset({"open_session", "dispatch_job"})

    def __init__(self, worker_id, script, *, protocol_version=6, settle_on=None,
                 signal_store=True):
        self.worker_id = worker_id
        self.script = list(script)
        self.protocol_version = protocol_version
        self.settle_on = settle_on
        self.signal_store = signal_store
        self.sent = []
        self.closed = []
        self._incarnation = None
        self._granted = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed.append(code)

    async def recv(self):
        hello = json.loads(self.sent[0])
        self._incarnation = hello.get("incarnation")
        signal = {"model_store": True} if self.signal_store else {}
        if self.protocol_version == 5:
            return json.dumps({"type": "registered", **signal})
        return json.dumps({
            "type": "registered", "protocol_version": 6, "worker_id": self.worker_id,
            "incarnation": self._incarnation, "transport_owner_id": str(uuid.uuid4()),
            "region": "local", "grant_nonce": hello["grant_nonce"],
            "lease_id": str(uuid.uuid4()), "owner_epoch": 1,
            "lease_expires_at": "2026-12-31T00:00:00Z", "ready": False, "remaining_ms": 0,
            **signal,
        })

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.protocol_version == 6 and not self._granted:
            self._granted = True
            return json.dumps({
                "type": "work_grant", "worker_id": self.worker_id,
                "incarnation": self._incarnation, "region": "local",
                "grant_nonce": str(uuid.uuid4()), "lease_id": str(uuid.uuid4()),
                "owner_epoch": 1, "lease_expires_at": "2026-12-31T00:00:00Z",
                "ready": True, "remaining_ms": 10_000,
            })
        if not self.script:
            await self._settle()
            raise StopAsyncIteration
        pause, build = self.script.pop(0)
        await asyncio.sleep(pause)
        command = build(self._incarnation)
        if self.protocol_version == 6 and command["type"] in self.LEDGER:
            command = signed(command)
        return json.dumps(command)

    async def _settle(self):
        if self.settle_on is None:
            return
        for _ in range(300):
            if any(isinstance(item, str) and self.settle_on(item) for item in self.sent):
                return
            await asyncio.sleep(0.01)


def test_protocol_six_worker_takes_an_install_and_reports_with_its_identity(tmp_path):
    manifest = pipeline_manifest()
    fleet = ScriptedFleet(
        "w-store-v6",
        [(0, lambda incarnation: {"type": "install_model", "model_id": "pipe-test",
                                  "worker_id": "w-store-v6", "incarnation": incarnation})],
        settle_on=lambda raw: '"failed"' in raw,
    )
    settings = Settings(worker_id="w-store-v6", models_dir=str(tmp_path))
    asyncio.run(serve_connection(fleet, settings, [manifest], FakeEngine()))

    hello = decode_control(fleet.sent[0])
    validate_message(hello, expected_worker_id="w-store-v6",
                     expected_incarnation=uuid.UUID(hello["incarnation"]))
    assert "model_store" in hello["capabilities"]
    assert "catalog" not in hello

    inventory = [decode_control(item) for item in fleet.sent
                 if isinstance(item, str) and '"store_inventory"' in item]
    assert inventory and inventory[0]["catalog"][0]["id"] == "pipe-test"

    reports = [decode_control(item) for item in fleet.sent
               if isinstance(item, str) and '"store_status"' in item]
    assert reports, "the install produced no store_status"
    final = reports[-1]["downloads"][0]
    assert (final["model_id"], final["state"], final["error"]) == (
        "pipe-test", "failed", "unavailable")
    assert reports[-1]["worker_id"] == "w-store-v6"
    assert reports[-1]["incarnation"] == fleet._incarnation


def test_a_removal_is_announced_before_a_slow_download_finishes(tmp_path, monkeypatch):
    complete_pipeline("org/a-pipe", REV)
    complete_categorizer()
    installed = pipeline_manifest("model-a", source="org/a-pipe", vae="")
    incoming = pipeline_manifest("model-b", source="org/b-pipe", vae="")
    entries = (
        ("org/b-pipe", REV, "model_index.json", 10),
        ("org/b-pipe", REV, "unet/config.json", 20),
        ("org/b-pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors", 1000),
    ) + CATEGORIZER_ENTRIES
    planned = fake_plan(*entries)

    def plan(manifest, models_dir, device="cuda"):
        # Only the incoming model's plan is invented; the installed one plans from the hub.
        if manifest.id == "model-b":
            return planned(manifest, models_dir)
        return registry_plan(manifest, models_dir)

    monkeypatch.setattr(model_store, "plan_items", plan)
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    release = threading.Event()

    def blocked_fetch(item: Item, on_bytes) -> None:
        assert release.wait(timeout=5)
        put_snapshot_file(item.repo, item.revision, item.name, item.size)
        on_bytes(item.size)

    monkeypatch.setattr(model_store, "_fetch_hub", blocked_fetch)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store = ModelStore([installed, incoming], FakeEngine(), str(tmp_path))
        await store.scan()
        assert "model-a" in store.installed
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "model-b"})
        await wait_for_op(store, "model-b", ("downloading",))
        store.handle({"type": "remove_model", "model_id": "model-a"})
        for _ in range(300):
            if any(m["type"] == "models_changed" and m["models"] == [] for m in sent):
                break
            await asyncio.sleep(0.01)
        # The announcement must arrive while the download is still running, not after it.
        announced = any(m["type"] == "models_changed" and m["models"] == [] for m in sent)
        still_downloading = store._ops["model-b"].state == "downloading"
        release.set()
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()
        return announced and still_downloading

    assert asyncio.run(scenario()), "the removal was not announced before the download finished"
    assert any(m["type"] == "models_changed" and m["models"] == [] for m in sent)


def test_a_downloaded_url_file_is_written_whole_and_leaves_no_partial_file(monkeypatch):
    class InterruptedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise OSError("connection interrupted")

    monkeypatch.setattr(model_store.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: InterruptedResponse())
    item = Item(path=model_store.hub_cache_root() / "weights.pth", size=7,
                url="https://example.test/weights.pth", root=model_store.hub_cache_root())
    with pytest.raises(OSError, match="connection interrupted"):
        model_store._fetch_url(item, lambda _n: None)
    assert not item.path.exists()
    assert not list(item.path.parent.glob("*.tmp"))


def test_readers_cannot_observe_a_partial_url_download(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    partial_written = threading.Event()
    finish_download = threading.Event()

    class Response:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                return b"complete"
            if self.reads == 2:
                partial_written.set()
                assert finish_download.wait(timeout=3)
            return b""

    monkeypatch.setattr(model_store.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response())
    item = Item(path=model_store.hub_cache_root() / "weights.pth", size=8,
                url="https://example.test/weights.pth", root=model_store.hub_cache_root())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(model_store._fetch_url, item, lambda _n: None)
        assert partial_written.wait(timeout=3)
        assert not item.path.exists()
        finish_download.set()
        future.result(timeout=3)
    assert item.path.read_bytes() == b"complete"


def test_a_completed_url_download_keeps_another_writers_temporary_file(monkeypatch):
    other = model_store.hub_cache_root() / "weights.pth.other-writer.tmp"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"another writer is still downloading")
    monkeypatch.setattr(model_store.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: io.BytesIO(b"complete weights"))
    item = Item(path=model_store.hub_cache_root() / "weights.pth", size=16,
                url="https://example.test/weights.pth", root=model_store.hub_cache_root())

    model_store._fetch_url(item, lambda _n: None)

    assert item.path.read_bytes() == b"complete weights"
    assert other.read_bytes() == b"another writer is still downloading"


def test_protocol_five_refuses_a_removed_model_and_keeps_the_socket(tmp_path):
    complete_pipeline()
    complete_companions()
    manifest = pipeline_manifest()
    engine = FakeEngine()
    settings = Settings(worker_id="w-race-v5", models_dir=str(tmp_path))
    fleet = ScriptedFleet(
        "w-race-v5",
        [
            (0, lambda _i: {"type": "remove_model", "model_id": "pipe-test"}),
            (0.1, lambda _i: {"type": "dispatch_job", "job_id": "job-race",
                              "model_id": "pipe-test", "dispatch_token": "tok-race",
                              "params": {"prompt": "x"},
                              "upload": {"url": "http://127.0.0.1/put", "headers": {}}}),
            (0, lambda _i: {"type": "open_session", "session_id": str(uuid.uuid4()),
                            "model_id": "pipe-test", "control_generation": 1,
                            "params": {"prompt": "x"}}),
        ],
        protocol_version=5,
        settle_on=lambda raw: '"session_refused"' in raw,
    )

    asyncio.run(serve_connection(fleet, settings, [manifest], engine))
    assert fleet.closed == [], "a removed model closed the fleet socket"
    replies = [json.loads(item) for item in fleet.sent[1:]]
    kinds = [reply["type"] for reply in replies]
    assert "models_changed" in kinds and "job_failed" in kinds, kinds
    assert kinds.index("models_changed") < kinds.index("job_failed"), kinds
    job = next(reply for reply in replies if reply["type"] == "job_failed")
    assert (job["job_id"], job["dispatch_token"], job["reason"]) == (
        "job-race", "tok-race", "model_unavailable")
    refused = next(reply for reply in replies if reply["type"] == "session_refused")
    assert refused["reason"] == "not_resident"


def test_protocol_six_refuses_a_removed_model_only_after_its_ack(tmp_path):
    complete_pipeline()
    complete_companions()
    manifest = pipeline_manifest()
    settings = Settings(worker_id="w-race-v6", models_dir=str(tmp_path))
    range_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    def lease(incarnation):
        return {"worker_id": "w-race-v6", "incarnation": incarnation, "region": "local",
                "owner_epoch": 1, "lease_id": str(uuid.uuid4()),
                "lease_expires_at": "2026-12-31T00:00:00Z", "grant_nonce": str(uuid.uuid4())}

    fleet = ScriptedFleet(
        "w-race-v6",
        [
            (0, lambda incarnation: {"type": "remove_model", "model_id": "pipe-test",
                                     "worker_id": "w-race-v6", "incarnation": incarnation}),
            (0.1, lambda incarnation: {
                "type": "dispatch_job", "command_sequence": 1, "command_id": str(uuid.uuid4()),
                **lease(incarnation), "job_id": job_id, "model_id": "pipe-test",
                "dispatch_sequence": 1, "dispatch_token": "tok-race",
                "params": {"prompt": "x"},
                "upload": {"url": "http://127.0.0.1/put", "headers": {}},
                "work_budget": {"range_id": range_id, "gpu_ms_limit": None}}),
            (0, lambda incarnation: {
                "type": "open_session", "command_sequence": 2, "command_id": str(uuid.uuid4()),
                **lease(incarnation), "session_id": str(uuid.uuid4()),
                "attempt_id": str(uuid.uuid4()), "control_generation": 1,
                "model_id": "pipe-test", "params": {"prompt": "x"},
                "work_budget": {"range_id": str(uuid.uuid4()), "gpu_ms_limit": None}}),
        ],
        settle_on=lambda raw: '"session_refused"' in raw,
    )

    asyncio.run(serve_connection(fleet, settings, [manifest], FakeEngine()))
    assert fleet.closed == [], "a removed model closed the fleet socket"
    replies = [decode_control(item) for item in fleet.sent if isinstance(item, str)]
    acks = {reply["command_sequence"]: index for index, reply in enumerate(replies)
            if reply["type"] == "command_ack"}
    failed = next(i for i, reply in enumerate(replies) if reply["type"] == "job_failed")
    refused = next(i for i, reply in enumerate(replies) if reply["type"] == "session_refused")
    assert acks[1] < failed and acks[2] < refused
    assert (replies[failed]["job_id"], replies[failed]["dispatch_token"]) == (job_id, "tok-race")
    assert replies[failed]["failure_code"] == "model_unavailable"
    assert replies[refused]["code"] == "model_unavailable"


SIGLIP_FILES = [
    ("config.json", 3), ("model.safetensors", 400), ("preprocessor_config.json", 2),
    ("special_tokens_map.json", 2), ("tokenizer.json", 4), ("tokenizer_config.json", 2),
]
SDXL_BASE_INDEX = {
    "_class_name": "StableDiffusionXLPipeline",
    "force_zeros_for_empty_prompt": True,
    "add_watermarker": None,
    "scheduler": ["diffusers", "EulerDiscreteScheduler"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "text_encoder_2": ["transformers", "CLIPTextModelWithProjection"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "tokenizer_2": ["transformers", "CLIPTokenizer"],
    "unet": ["diffusers", "UNet2DConditionModel"],
    "vae": ["diffusers", "AutoencoderKL"],
}
# Shaped like stabilityai/stable-diffusion-xl-base-1.0: weights at the root, and
# fp16 and full-precision variants in each component folder.
SDXL_BASE_FILES = [
    (".gitattributes", 1), ("README.md", 1), ("model_index.json", 10),
    ("sd_xl_base_1.0.safetensors", 6900), ("sd_xl_base_1.0_0.9vae.safetensors", 335),
    ("sd_xl_offset_example-lora_1.0.safetensors", 47),
    ("scheduler/scheduler_config.json", 2),
    ("text_encoder/config.json", 3), ("text_encoder/model.fp16.safetensors", 246),
    ("text_encoder/model.safetensors", 492),
    ("text_encoder_2/config.json", 3), ("text_encoder_2/model.fp16.safetensors", 1390),
    ("text_encoder_2/model.safetensors", 2780),
    ("tokenizer/merges.txt", 2), ("tokenizer/special_tokens_map.json", 2),
    ("tokenizer/tokenizer_config.json", 2), ("tokenizer/vocab.json", 2),
    ("tokenizer_2/merges.txt", 2), ("tokenizer_2/special_tokens_map.json", 2),
    ("tokenizer_2/tokenizer_config.json", 2), ("tokenizer_2/vocab.json", 2),
    ("unet/config.json", 3), ("unet/diffusion_pytorch_model.fp16.safetensors", 1720),
    ("unet/diffusion_pytorch_model.safetensors", 5140),
    ("vae/config.json", 3), ("vae/diffusion_pytorch_model.fp16.safetensors", 168),
    ("vae/diffusion_pytorch_model.safetensors", 335),
]


def test_a_pipeline_plan_reads_only_what_the_pipeline_loads():
    chosen = model_store.select_pipeline_plan(SDXL_BASE_FILES, SDXL_BASE_INDEX, "fp16")
    assert [name for name, _size in chosen] == sorted(name for name, _size in chosen)
    assert {name for name, _size in chosen} == {
        "model_index.json",
        "scheduler/scheduler_config.json",
        "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
        "text_encoder_2/config.json", "text_encoder_2/model.fp16.safetensors",
        "tokenizer/merges.txt", "tokenizer/special_tokens_map.json",
        "tokenizer/tokenizer_config.json", "tokenizer/vocab.json",
        "tokenizer_2/merges.txt", "tokenizer_2/special_tokens_map.json",
        "tokenizer_2/tokenizer_config.json", "tokenizer_2/vocab.json",
        "unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
        "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors",
    }


def test_a_pipeline_install_reads_the_index_and_plans_only_its_components(monkeypatch):
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    listings = {"org/pipe": SDXL_BASE_FILES, CATEGORIZER_SOURCE: SIGLIP_FILES}
    fetched: list[tuple[str, str]] = []

    def listing(repo, _revision):
        return listings[repo], False, {}

    def index_text(repo, _revision, name):
        fetched.append((repo, name))
        return json.dumps(SDXL_BASE_INDEX)

    monkeypatch.setattr(model_store, "hub_listing", listing)
    monkeypatch.setattr(model_store, "hub_file_text", index_text)

    items, _gated = model_store.plan_items(pipeline_manifest(vae=""), "")

    planned = {item.name for item in items if item.repo == "org/pipe"}
    assert fetched == [("org/pipe", "model_index.json")]
    assert "sd_xl_base_1.0.safetensors" not in planned
    assert "unet/diffusion_pytorch_model.fp16.safetensors" in planned
    assert {item.name for item in items if item.repo == CATEGORIZER_SOURCE} == {
        name for name, _size in SIGLIP_FILES}


def test_every_diffusion_model_carries_the_categorizer_as_a_companion():
    refs = model_store.repo_refs(pipeline_manifest())
    assert (CATEGORIZER_SOURCE, CATEGORIZER_REVISION) in [
        (ref.repo, ref.revision) for ref in refs]
    assert model_store.repo_refs(upscale_manifest("https://example.test/weights.pth", [2])) == []


def test_the_categorizer_stays_while_an_installed_model_needs_it():
    complete_pipeline()
    complete_categorizer()
    complete_pipeline("org/pipe2", OTHER_REV)
    other = pipeline_manifest("model-b", source="org/pipe2", vae="")
    other.source_revision = OTHER_REV

    _delete_model_files(pipeline_manifest(vae=""), "", [other])
    assert snapshot_dir(CATEGORIZER_SOURCE, CATEGORIZER_REVISION).is_dir()

    _delete_model_files(other, "", [])
    assert not snapshot_dir(CATEGORIZER_SOURCE, CATEGORIZER_REVISION).exists()


def held_fetch_until(release: threading.Event):
    """Fetch stand-in: holds each file until `release` is set, or the attempt is cancelled.

    Bounded, so a broken store fails the test instead of hanging the thread pool at exit.
    """
    def fetch(item: Item, on_bytes) -> None:
        deadline = time.monotonic() + 10
        while not release.wait(0.01):
            if time.monotonic() > deadline:
                raise OSError("the fetch was held past the test's deadline")
            on_bytes(0)
        put_snapshot_file(item.repo, item.revision, item.name, item.size)
        on_bytes(item.size)
    return fetch


def test_detaching_keeps_a_download_running_and_the_next_attach_reports_it(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES, *CATEGORIZER_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    release = threading.Event()
    monkeypatch.setattr(model_store, "_fetch_hub", held_fetch_until(release))
    first, second = [], []

    async def send_first(message):
        first.append(message)

    async def send_second(message):
        second.append(message)

    async def scenario():
        try:
            return await run_held()
        finally:
            release.set()

    async def run_held():
        store = ModelStore([pipeline_manifest("model-b", source="org/b-pipe", vae="")],
                           FakeEngine(), str(tmp_path))
        await store.scan()
        store.attach(send_first, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "model-b"})
        await wait_for_op(store, "model-b", ("downloading",))
        await asyncio.wait_for(store.close(), timeout=2)  # the connection drops
        await asyncio.sleep(0.1)
        still_running = store._ops["model-b"].state
        release.set()
        await wait_for_op(store, "model-b", ("done", "failed"))
        store.attach(send_second, lambda _m: False)
        await asyncio.sleep(0.2)
        await asyncio.wait_for(store.close(), timeout=2)
        return still_running, store.installed

    still_running, installed = asyncio.run(scenario())
    assert still_running == "downloading"
    assert "model-b" in installed
    reports = [m for m in second if m["type"] == "store_status"]
    assert reports and reports[-1]["downloads"][0]["state"] == "done"


def test_shutdown_stops_a_running_download_and_keeps_what_it_completed(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES, *CATEGORIZER_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))

    def held_fetch(item: Item, on_bytes) -> None:
        if item.name == "model_index.json":
            put_snapshot_file(item.repo, item.revision, item.name, item.size)
            on_bytes(item.size)
            return
        deadline = time.monotonic() + 10
        while True:  # held until the process stops the attempt, or the deadline
            if time.monotonic() > deadline:
                raise OSError("the fetch was held past the test's deadline")
            time.sleep(0.01)
            on_bytes(0)

    monkeypatch.setattr(model_store, "_fetch_hub", held_fetch)

    async def send(_message):
        return None

    async def scenario():
        store = ModelStore([pipeline_manifest("model-b", source="org/b-pipe", vae="")],
                           FakeEngine(), str(tmp_path))
        await store.scan()
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "model-b"})
        await wait_for_op(store, "model-b", ("downloading",))
        await asyncio.wait_for(store.shutdown(), timeout=5)
        return store._ops["model-b"]

    op = asyncio.run(scenario())
    assert (op.state, op.error) == ("failed", "cancelled")
    assert (snapshot_dir("org/b-pipe", REV) / "model_index.json").exists()


def test_a_reconnect_keeps_the_download_and_reports_it_to_the_next_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES, *CATEGORIZER_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    release = threading.Event()
    monkeypatch.setattr(model_store, "_fetch_hub", held_fetch_until(release))
    manifest = pipeline_manifest("model-b", source="org/b-pipe", vae="")
    settings = Settings(worker_id="w-reconnect", models_dir=str(tmp_path))

    async def scenario():
        try:
            return await run_held()
        finally:
            release.set()

    async def run_held():
        store = ModelStore([manifest], FakeEngine(), str(tmp_path))
        await store.scan()
        first = ScriptedFleet(
            "w-reconnect",
            [(0, lambda _i: {"type": "install_model", "model_id": "model-b"})],
            protocol_version=5,
        )
        await serve_connection(first, settings, [manifest], FakeEngine(), store)
        asyncio.get_running_loop().call_later(0.3, release.set)
        second = ScriptedFleet("w-reconnect", [], protocol_version=5,
                               settle_on=lambda raw: '"done"' in raw)
        await asyncio.wait_for(serve_connection(second, settings, [manifest],
                                                FakeEngine(), store), timeout=10)
        return second, store

    second, store = asyncio.run(scenario())
    assert "model-b" in store.installed
    hello = json.loads(second.sent[0])
    assert hello["models"] == []  # not installed yet when this connection registered
    states = [json.loads(item)["downloads"][0]["state"] for item in second.sent[1:]
              if isinstance(item, str) and '"store_status"' in item]
    assert states[0] == "downloading" and states[-1] == "done"


IRIS_MANIFEST = Path(__file__).resolve().parents[1] / "models" / "iris-3b.json"
QWEN_REVISION = "ebb281ec70b05090aa6165b016eac8ec08e71b17"


def test_iris_loads_its_plain_repository_and_declares_its_text_encoder():
    iris = Manifest.model_validate_json(IRIS_MANIFEST.read_text())
    refs = {(ref.repo, ref.revision, ref.kind) for ref in model_store.repo_refs(iris)}
    assert (iris.source, iris.source_revision, "component") in refs
    assert ("Qwen/Qwen3-VL-4B-Instruct", QWEN_REVISION, "component") in refs
    assert "worker_repos" not in iris.wire()


def test_a_plain_repository_with_only_its_config_is_not_installed():
    remote("org/plain", REV, "model.safetensors")
    manifest = Manifest(id="plain-test", name="Plain", capabilities=["text_to_image"],
                        source="org/plain", source_revision=REV, pipeline="Iris3B",
                        dtype="bfloat16", vae="")
    complete_categorizer()
    put_snapshot_file("org/plain", REV, "config.yaml")
    assert not settled(manifest, "")
    put_snapshot_file("org/plain", REV, "model.safetensors")
    assert settled(manifest, "")


def test_install_blocking_reports_progress_and_verifies_the_result(monkeypatch, tmp_path):
    manifest = pipeline_manifest("model-b", source="org/b-pipe", vae="")
    entries = B_ENTRIES + CATEGORIZER_ENTRIES
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*entries))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch())
    seen: list[tuple[int, int]] = []

    model_store.install_blocking(manifest, str(tmp_path),
                                 progress=lambda done, total: seen.append((done, total)))

    total = sum(entry[3] for entry in entries)
    assert seen[0] == (0, total)
    assert seen[-1] == (total, total)
    assert [done for done, _total in seen] == sorted(done for done, _total in seen)
    assert settled(manifest, str(tmp_path))


def test_install_blocking_refuses_a_fetch_that_leaves_the_model_incomplete(monkeypatch, tmp_path):
    manifest = pipeline_manifest("model-b", source="org/b-pipe", vae="")
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES, *CATEGORIZER_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))

    def fetch_nothing(item: Item, on_bytes) -> None:
        on_bytes(item.size)  # reports its bytes and writes no file

    monkeypatch.setattr(model_store, "_fetch_hub", fetch_nothing)
    with pytest.raises(ModelStoreError) as refused:
        model_store.install_blocking(manifest, str(tmp_path))
    assert refused.value.reason == "incomplete"
    assert not settled(manifest, str(tmp_path))


def test_ensure_installed_skips_an_installed_model_and_names_a_refusal(monkeypatch, tmp_path):
    complete_pipeline()
    complete_companions()

    def no_fetch(*_args):
        raise AssertionError("an installed model was fetched again")

    monkeypatch.setattr(model_store, "_fetch_hub", no_fetch)
    model_store.ensure_installed(pipeline_manifest(), str(tmp_path), out=io.StringIO())

    monkeypatch.setattr(model_store, "plan_items", fake_plan(
        ("org/c-pipe", REV, "model_index.json", 10),
        ("org/c-pipe", REV, "unet/config.json", 20),
        ("org/c-pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors", 1000),
    ))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 0))
    with pytest.raises(RuntimeError, match="disk"):
        model_store.ensure_installed(pipeline_manifest("model-c", source="org/c-pipe", vae=""),
                                     str(tmp_path), out=io.StringIO())


def test_every_script_that_loads_a_model_installs_it_first():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("compile_ab.py", "profile-candidates.py", "profile-realtime-frame.py",
                 "prototype-batch-sweep.py"):
        assert "ensure_installed(" in (scripts / name).read_text(), name
    assert "ensure_remote_installed(" in (scripts / "benchmark.py").read_text()


def load_benchmark_script():
    import importlib.util
    import sys

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("benchmark_script", scripts / "benchmark.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    return module


class AdminRows:
    """Stand-in for the admin API: serves the given catalog states in order, records installs."""

    def __init__(self, states):
        self.states = list(states)
        self.installs = []

    def get(self, _url, **_kwargs):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return FakeResponse([{"worker_id": "w-bench", "models": [{
            "id": "pipe-test", "installed": state.get("installed", False),
            "needs_token": False, "download": state.get("download")}]}])

    def post(self, url, **_kwargs):
        self.installs.append(url)
        return FakeResponse({"op_id": "002"}, status=202)


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def test_remote_install_waits_for_the_download_and_names_a_refusal(monkeypatch):
    benchmark = load_benchmark_script()
    monkeypatch.setattr(benchmark.time, "sleep", lambda _s: None)
    client = AdminRows([
        {"installed": False},
        {"download": {"op_id": "002", "state": "downloading", "bytes_done": 2**30, "bytes_total": 4 * 2**30}},
        {"installed": True, "download": {"op_id": "002"}},
    ])
    benchmark.ensure_remote_installed(client, "http://api", "pipe-test")
    assert client.installs == ["http://api/api/v1/admin/workers/w-bench/models/pipe-test/install"]

    refused = AdminRows([{"download": {"op_id": "002", "state": "failed", "error": "disk"}}])
    with pytest.raises(RuntimeError, match="disk"):
        benchmark.ensure_remote_installed(refused, "http://api", "pipe-test")


# ---- Sol 6.1 review of 4f52d32: one failing test per ruling ----------------------

ORIGIN_PROTOCOL6 = Path(__file__).resolve().parent / "fixtures" / "origin_main_protocol6.py"


def load_origin_protocol6():
    import importlib.util

    spec = importlib.util.spec_from_file_location("origin_main_protocol6", ORIGIN_PROTOCOL6)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hello_is_accepted_by_the_origin_main_validator(tmp_path):
    origin = load_origin_protocol6()
    manifest = pipeline_manifest()
    fleet = ScriptedFleet("w-n1", [], settle_on=None)
    settings = Settings(worker_id="w-n1", models_dir=str(tmp_path))
    asyncio.run(serve_connection(fleet, settings, [manifest], FakeEngine()))
    hello = origin.decode_control(fleet.sent[0])
    origin.validate_message(hello, expected_worker_id="w-n1",
                            expected_incarnation=uuid.UUID(hello["incarnation"]))
    assert "model_store" in hello["capabilities"]
    assert "catalog" not in hello and "hardware" not in hello


def test_store_messages_wait_for_the_api_signal(tmp_path):
    manifest = pipeline_manifest()
    fleet = ScriptedFleet(
        "w-n1",
        [(0, lambda incarnation: {"type": "install_model", "model_id": "pipe-test",
                                  "worker_id": "w-n1", "incarnation": incarnation})],
        settle_on=lambda raw: '"store_' in raw,
        signal_store=False,
    )
    settings = Settings(worker_id="w-n1", models_dir=str(tmp_path))
    asyncio.run(serve_connection(fleet, settings, [manifest], FakeEngine()))
    sent_types = [json.loads(item)["type"] for item in fleet.sent[1:] if isinstance(item, str)]
    assert not {"store_inventory", "store_status", "models_changed"} & set(sent_types)


def test_a_capable_api_gets_the_inventory_after_registration(tmp_path):
    manifest = pipeline_manifest()
    fleet = ScriptedFleet("w-n1", [], settle_on=lambda raw: '"store_inventory"' in raw)
    settings = Settings(worker_id="w-n1", models_dir=str(tmp_path))
    asyncio.run(serve_connection(fleet, settings, [manifest], FakeEngine()))
    inventory = [decode_control(item) for item in fleet.sent
                 if isinstance(item, str) and '"store_inventory"' in item]
    assert inventory, "no store_inventory after a registration that signalled model_store"
    assert inventory[0]["catalog"][0]["id"] == "pipe-test"
    assert inventory[0]["hardware"]["disk_free_bytes"] > 0


def test_each_destination_volume_needs_its_own_margin(monkeypatch, tmp_path):
    models_dir = tmp_path / "models"
    hub = model_store.hub_cache_root()

    def volume(path):
        under_models = str(path).startswith(str(models_dir))
        return (2 if under_models else 1), (hub if not under_models else models_dir)

    def free(probe):
        return (10**12, 1000 if str(probe).startswith(str(models_dir)) else 10**12)

    monkeypatch.setattr(model_store, "volume_of", volume)
    monkeypatch.setattr(model_store, "free_bytes", free)
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch())
    monkeypatch.setattr(model_store, "_fetch_url", write_fetch())
    items = [
        Item(path=snapshot_dir("org/pipe", REV) / "model_index.json", size=10,
             repo="org/pipe", revision=REV, name="model_index.json", folder=repo_folder("org/pipe")),
        Item(path=models_dir / ".cache" / "upscale" / "up" / "w.pth", size=5000,
             url="https://example.test/w.pth"),
    ]
    monkeypatch.setattr(model_store, "plan_items", lambda *_a, **_k: (items, False))
    manifest = pipeline_manifest()
    with pytest.raises(ModelStoreError) as refused:
        model_store.install_blocking(manifest, str(models_dir))
    assert refused.value.reason == "disk"


def test_a_cancelled_install_between_removals_does_not_let_a_later_install_overtake(
        monkeypatch, tmp_path):
    """Sol round 2, interleaving 1: removal R1, install I1, removal R2, cancel I1, install I2.
    The queue keeps its order, so I2 runs after R1 and R2 and leaves the model installed."""
    models = tmp_path / "models"
    cache = models / ".cache" / "upscale" / "up-test"
    cache.mkdir(parents=True)
    (cache / "w.pth").write_bytes(b"x" * 10)
    monkeypatch.setattr(model_store, "url_size", lambda _url: 10)
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    monkeypatch.setattr(model_store, "_fetch_url", write_url_fetch())
    manifest = upscale_manifest("https://example.test/w.pth", [2])

    async def send(_message):
        return None

    async def scenario():
        store = ModelStore([manifest], FakeEngine(), str(models))
        await store.scan()
        assert "up-test" in store.installed
        store.attach(send, lambda _m: False)
        store.handle({"type": "remove_model", "model_id": "up-test"})
        store.handle({"type": "install_model", "model_id": "up-test"})
        store.handle({"type": "remove_model", "model_id": "up-test"})
        store.handle({"type": "cancel_install", "model_id": "up-test"})
        store.handle({"type": "install_model", "model_id": "up-test"})
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()
        return store

    store = asyncio.run(scenario())
    assert ("up-test" in store.installed) == is_installed(manifest, str(models))
    assert "up-test" in store.installed
    assert (cache / "w.pth").is_file()


def test_install_remove_install_ends_installed(monkeypatch, tmp_path):
    """Sol round 2, interleaving 2: install, remove, install. The last request wins."""
    models = tmp_path / "models"
    cache = models / ".cache" / "upscale" / "up-test"
    monkeypatch.setattr(model_store, "url_size", lambda _url: 10)
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    monkeypatch.setattr(model_store, "_fetch_url", write_url_fetch())
    manifest = upscale_manifest("https://example.test/w.pth", [2])

    async def send(_message):
        return None

    async def scenario():
        store = ModelStore([manifest], FakeEngine(), str(models))
        await store.scan()
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "up-test"})
        store.handle({"type": "remove_model", "model_id": "up-test"})
        store.handle({"type": "install_model", "model_id": "up-test"})
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()
        return store

    store = asyncio.run(scenario())
    assert "up-test" in store.installed
    assert is_installed(manifest, str(models))
    assert (cache / "w.pth").is_file()


def test_an_interrupted_download_is_never_adopted(tmp_path):
    complete_pipeline()
    complete_companions()
    model_store._write_marker("pipe-test", {"state": "installing"})
    store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
    asyncio.run(store.scan())
    assert "pipe-test" not in store.installed
    assert not settled(pipeline_manifest(), str(tmp_path))


def test_a_legacy_complete_cache_is_adopted_on_scan(tmp_path):
    complete_pipeline()
    complete_companions()
    store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
    asyncio.run(store.scan())
    assert "pipe-test" in store.installed
    assert model_store._read_marker("pipe-test")["state"] == "installed"


def test_a_marker_is_not_trusted_once_a_file_changes_size(tmp_path):
    complete_pipeline()
    complete_companions()
    store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
    asyncio.run(store.scan())
    assert settled(pipeline_manifest(), str(tmp_path))
    weight = snapshot_dir("org/pipe", REV) / "unet/diffusion_pytorch_model.fp16.safetensors"
    weight.write_bytes(b"x")  # truncated, still non-empty
    assert not settled(pipeline_manifest(), str(tmp_path))


def test_a_cancel_keeps_the_partial_download_for_the_hub_to_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    cancel = threading.Event()
    partial = repo_folder("org/b-pipe") / "blobs" / "resume-me.incomplete"

    def cancelling(item, on_bytes):
        if item.name == "model_index.json":
            put_snapshot_file(item.repo, item.revision, item.name, item.size)
            on_bytes(item.size)
            return
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"half")
        cancel.set()
        on_bytes(4)

    monkeypatch.setattr(model_store, "_fetch_hub", cancelling)
    with pytest.raises(ModelStoreError):
        model_store.install_blocking(pipeline_manifest("model-b", source="org/b-pipe", vae=""),
                                     str(tmp_path), cancel=cancel)
    assert partial.exists()


def test_a_cancel_never_removes_a_file_another_writer_completed(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", fake_plan(*B_ENTRIES, *CATEGORIZER_ENTRIES))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    cancel = threading.Event()
    other_writer_file = snapshot_dir(CATEGORIZER_SOURCE, CATEGORIZER_REVISION) / "config.json"

    def fetch(item, on_bytes):
        if item.name != "unet/diffusion_pytorch_model.fp16.safetensors":
            put_snapshot_file(item.repo, item.revision, item.name, item.size)
            on_bytes(item.size)
            return
        put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "config.json", 3)
        cancel.set()
        on_bytes(1)

    monkeypatch.setattr(model_store, "_fetch_hub", fetch)
    with pytest.raises(ModelStoreError):
        model_store.install_blocking(pipeline_manifest("model-b", source="org/b-pipe", vae=""),
                                     str(tmp_path), cancel=cancel)
    assert other_writer_file.is_symlink() or other_writer_file.exists()


def test_a_removal_collects_blobs_under_component_folders(tmp_path):
    complete_pipeline("org/rm", REV)
    weight = snapshot_dir("org/rm", REV) / "unet/diffusion_pytorch_model.fp16.safetensors"
    blob = Path(os.path.realpath(weight))
    assert blob.is_file()
    manifest = pipeline_manifest("rm-test", source="org/rm", vae="")
    model_store._delete_model_files(manifest, str(tmp_path), [])
    assert not weight.exists()
    assert not blob.exists()


def test_an_upscaler_without_a_declared_size_is_refused(monkeypatch):
    monkeypatch.setattr(model_store, "url_size", REAL_URL_SIZE)
    class NoLength:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(model_store.urllib.request, "urlopen", lambda *_a, **_k: NoLength())
    with pytest.raises(ModelStoreError) as refused:
        model_store.url_size("https://example.test/w.pth")
    assert refused.value.reason == "download_failed"


def test_a_truncated_upscaler_download_is_never_published(monkeypatch, tmp_path):
    import io

    monkeypatch.setattr(model_store.urllib.request, "urlopen",
                        lambda *_a, **_k: io.BytesIO(b"only 7 b"))
    item = Item(path=tmp_path / "upscale" / "w.pth", size=16, url="https://example.test/w.pth",
                root=tmp_path / "upscale")
    with pytest.raises(ModelStoreError) as refused:
        model_store._fetch_url(item, lambda _n: None)
    assert refused.value.reason == "download_failed"
    assert not item.path.exists()


def test_factors_that_share_one_file_are_planned_once(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    monkeypatch.setattr(model_store, "url_size", lambda _url: 10)
    manifest = upscale_manifest("https://example.test/w.pth", [2, 4])
    items, _gated = model_store.plan_items(manifest, str(tmp_path))
    assert len(items) == 1
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    monkeypatch.setattr(model_store, "_fetch_url", write_url_fetch())
    seen = []
    model_store.install_blocking(manifest, str(tmp_path),
                                 progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (10, 10)


def test_a_gated_model_keeps_its_token_flag_when_size_discovery_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    manifest = pipeline_manifest()

    def gated_listing(repo, _revision):
        return [], True, {}

    def refused_index(_repo, _revision, _name):
        raise ModelStoreError("needs_token")

    monkeypatch.setattr(model_store, "hub_listing", gated_listing)
    monkeypatch.setattr(model_store, "hub_file_text", refused_index)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert model_store.probe_model(manifest, str(tmp_path)) == (None, True)


def test_an_upscaler_path_cannot_leave_its_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "url_size", lambda _url: 10)
    calls = []
    monkeypatch.setattr(model_store, "_fetch_url", write_url_fetch(calls))
    # A model id of ".." bypasses the slug rule only if the caller skips validation.
    manifest = Manifest.model_construct(
        **{**upscale_manifest("https://example.test/w.pth", [2]).model_dump(), "id": ".."})
    assert not model_store.contained(tmp_path / ".cache" / "upscale" / "up-test" / ".." / "w.pth",
                                     tmp_path / ".cache" / "upscale" / "up-test")
    with pytest.raises(ModelStoreError) as refused:
        model_store.install_blocking(manifest, str(tmp_path))
    assert refused.value.reason == "download_failed"
    assert calls == []


MOSS_INDEX = {
    "_class_name": "MOSSImagePipeline",
    "custom_pipeline": "pipeline_moss_image",
    "transformer": ["diffusers", "MossTransformer"],
    "vae": ["diffusers", "AutoencoderKL"],
    "text_encoder": ["transformers", "T5EncoderModel"],
}
MOSS_FILES = [
    (".gitattributes", 1), ("README.md", 1), ("model_index.json", 10),
    ("pipeline_moss_image.py", 20), ("modeling_moss.py", 30),
    ("model.safetensors", 5000),
    ("transformer/config.json", 3), ("transformer/diffusion_pytorch_model.fp16.safetensors", 100),
    ("vae/config.json", 3), ("vae/diffusion_pytorch_model.fp16.safetensors", 50),
    ("text_encoder/config.json", 3), ("text_encoder/model.fp16.safetensors", 40),
]


def test_a_moss_shaped_pipeline_plans_its_root_modules_and_no_root_weights():
    chosen = {name for name, _size in
              model_store.select_pipeline_plan(MOSS_FILES, MOSS_INDEX, "fp16")}
    assert chosen == {
        "model_index.json", "pipeline_moss_image.py", "modeling_moss.py",
        "transformer/config.json", "transformer/diffusion_pytorch_model.fp16.safetensors",
        "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors",
        "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
    }


def test_a_snapshots_folder_linked_outside_the_cache_is_never_removed(tmp_path):
    outside = tmp_path / "outside" / "snapshots" / REV
    outside.mkdir(parents=True)
    keep = outside / "unet.bin"
    keep.write_bytes(b"not the cache's")
    folder = repo_folder("org/esc")
    folder.mkdir(parents=True)
    (folder / "snapshots").symlink_to(tmp_path / "outside" / "snapshots")
    manifest = pipeline_manifest("esc-test", source="org/esc", vae="")
    with pytest.raises(ModelStoreError) as refused:
        model_store._delete_model_files(manifest, str(tmp_path), [])
    assert refused.value.reason == "remove_failed"
    assert keep.read_bytes() == b"not the cache's"


def test_an_empty_tokenizer_folder_is_not_adopted_but_a_complete_cache_is(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    listings = {
        "org/pipe": [("model_index.json", MODEL_INDEX_SIZE), ("unet/config.json", 3),
                     ("unet/diffusion_pytorch_model.fp16.safetensors", 1000),
                     ("tokenizer/vocab.json", 4)],
        CATEGORIZER_SOURCE: [("config.json", 3), ("model.safetensors", 400)],
    }
    index = {"unet": ["diffusers", "UNet2DConditionModel"],
             "tokenizer": ["transformers", "CLIPTokenizer"]}
    monkeypatch.setattr(model_store, "hub_listing",
                        lambda repo, _revision: (listings[repo], False, {}))
    monkeypatch.setattr(model_store, "hub_file_text", lambda *_args: json.dumps(index))
    manifest = pipeline_manifest(vae="")
    put_snapshot_file("org/pipe", REV, "model_index.json")
    put_snapshot_file("org/pipe", REV, "unet/config.json", 3)
    put_snapshot_file("org/pipe", REV, "unet/diffusion_pytorch_model.fp16.safetensors", 1000)
    (snapshot_dir("org/pipe", REV) / "tokenizer").mkdir(parents=True)  # empty: nothing downloaded
    put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "config.json", 3)
    put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "model.safetensors", 400)

    assert not model_store.adopt_legacy(manifest, str(tmp_path))
    assert not is_installed(manifest, str(tmp_path))

    put_snapshot_file("org/pipe", REV, "tokenizer/vocab.json", 4)
    assert model_store.adopt_legacy(manifest, str(tmp_path))
    assert is_installed(manifest, str(tmp_path))


def test_no_metadata_means_no_adoption(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    complete_pipeline()
    complete_companions()
    assert not model_store.adopt_legacy(pipeline_manifest(), str(tmp_path))  # hub offline


def test_a_missing_pointer_over_a_complete_blob_costs_nothing(monkeypatch, tmp_path):
    folder = repo_folder("org/pipe")
    blob = folder / "blobs" / ("a" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"w" * 1000)
    item = model_store.Item(
        path=snapshot_dir("org/pipe", REV) / "unet/weights.safetensors", size=1000,
        repo="org/pipe", revision=REV, name="unet/weights.safetensors", folder=folder,
        root=model_store.hub_cache_root(), blob_key="a" * 64)
    monkeypatch.setattr(model_store, "plan_items", lambda *_a, **_k: ([item], False))
    # Free space is one byte short of what fetching 1000 bytes would need, with the margin.
    monkeypatch.setattr(model_store, "free_bytes",
                        lambda _p: (10**12, 1000 + DISK_MARGIN_BYTES - 1))
    seen = []
    model_store.install_blocking(pipeline_manifest("cache-test", source="org/pipe", vae=""),
                                 str(tmp_path), progress=lambda done, total: seen.append((done, total)))
    assert item.path.is_symlink() and item.path.read_bytes() == b"w" * 1000
    assert seen[0] == (0, 0)  # the shown transfer is zero bytes


def test_a_failed_removal_leaves_the_model_installed_and_retryable(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.05)
    complete_pipeline()
    complete_companions()
    manifest = pipeline_manifest()
    root = snapshot_dir("org/pipe", REV)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store = ModelStore([manifest], FakeEngine(), str(tmp_path))
        await store.scan()
        assert "pipe-test" in store.installed
        store.attach(send, lambda _m: False)
        # Every directory of the snapshot is read-only, so the first deletion is refused
        # before any file is removed and the model stays complete.
        locked = [dirpath for dirpath, _dirs, _names in os.walk(root)]
        for dirpath in locked:
            os.chmod(dirpath, 0o500)
        try:
            store.handle({"type": "remove_model", "model_id": "pipe-test", "op_id": "remove-fail"})
            await asyncio.wait_for(store._queue.join(), timeout=10)
            await asyncio.sleep(0.2)
            still_served = "pipe-test" in store.installed
            still_marked = is_installed(manifest, str(tmp_path))
        finally:
            for dirpath in locked:
                os.chmod(dirpath, 0o755)
        failures = [entry for message in sent if message["type"] == "store_status"
                    for entry in message["downloads"] if entry["state"] == "failed"]
        store.handle({"type": "remove_model", "model_id": "pipe-test"})
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()
        return still_served, still_marked, failures, store

    still_served, still_marked, failures, store = asyncio.run(scenario())
    assert still_served and still_marked
    assert [(entry["error"], entry["op_id"]) for entry in failures][-1:] == [("remove_failed", "remove-fail")]
    assert "pipe-test" not in store.installed
    assert not root.exists()


def test_a_cancelled_queued_install_touches_no_disk(monkeypatch, tmp_path):
    """A cancelled queued install is skipped whole when the drainer reaches it: nothing is
    marked, linked or fetched, even though the cache could complete the pointer."""
    folder = repo_folder("org/pipe")
    blob = folder / "blobs" / ("a" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"w" * 1000)
    pointer = model_store.Item(
        path=snapshot_dir("org/pipe", REV) / "unet/cached.bin", size=1000,
        repo="org/pipe", revision=REV, name="unet/cached.bin", folder=folder,
        root=model_store.hub_cache_root(), blob_key="a" * 64)
    monkeypatch.setattr(model_store, "plan_items", lambda *_a, **_k: ([pointer], False))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    calls: list = []
    monkeypatch.setattr(model_store, "_fetch_hub", write_fetch(calls))
    manifest = pipeline_manifest("cancel-test", source="org/pipe", vae="")

    async def scenario():
        store = new_store(manifest, str(tmp_path))
        store.attach(lambda _m: _noop(), lambda _m: False)
        store.handle({"type": "install_model", "model_id": "cancel-test"})
        store.handle({"type": "cancel_install", "model_id": "cancel-test"})
        await asyncio.wait_for(store._queue.join(), timeout=10)
        await store.close()
        return store._ops["cancel-test"]

    op = asyncio.run(scenario())
    assert (op.state, op.error) == ("failed", "cancelled")
    assert not pointer.path.is_symlink()
    assert not model_store._marker_path("cancel-test").exists()
    assert calls == []


def test_an_upscaler_weight_outside_its_cache_is_never_removed(monkeypatch, tmp_path):
    outside = tmp_path / "outside" / "w.pth"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not the cache's")
    manifest = upscale_manifest("https://example.test/w.pth", [2])
    monkeypatch.setattr(model_store, "upscale_targets",
                        lambda _manifest, _models_dir: [("https://example.test/w.pth", outside)])
    with pytest.raises(ModelStoreError) as refused:
        model_store._delete_model_files(manifest, str(tmp_path / "models"), [])
    assert refused.value.reason == "remove_failed"
    assert outside.read_bytes() == b"not the cache's"


def test_a_pointer_target_outside_the_hub_cache_is_never_unlinked(monkeypatch, tmp_path):
    victim = tmp_path / "outside" / "config.json"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"not the cache's")
    folder = repo_folder("org/pipe")
    blob = folder / "blobs" / ("a" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"w" * 1000)
    item = model_store.Item(
        path=victim, size=1000, repo="org/pipe", revision=REV, name="config.json",
        folder=folder, root=model_store.hub_cache_root(), blob_key="a" * 64)
    monkeypatch.setattr(model_store, "plan_items", lambda *_a, **_k: ([item], False))
    monkeypatch.setattr(model_store, "free_bytes", lambda _p: (10**12, 10**12))
    manifest = pipeline_manifest("link-test", source="org/pipe", vae="")
    with pytest.raises(ModelStoreError) as refused:
        model_store.install_blocking(manifest, str(tmp_path / "models"))
    assert refused.value.reason == "download_failed"
    assert victim.read_bytes() == b"not the cache's"
    assert not victim.is_symlink()


def test_running_cancel_reports_cancel_operation_id(monkeypatch, tmp_path):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.01)
    manifest = pipeline_manifest()
    entered = threading.Event()
    sent = []

    def download(op, _manifest):
        entered.set()
        op.cancel.wait(5)
        raise ModelStoreError("cancelled")

    async def send(message):
        sent.append(message)

    async def scenario():
        store = new_store(manifest, str(tmp_path))
        monkeypatch.setattr(store, "_download", download)
        store.attach(send, lambda _m: False)
        store.handle({"type": "install_model", "model_id": "pipe-test", "op_id": "install"})
        await asyncio.to_thread(entered.wait, 5)
        store.handle({"type": "cancel_install", "model_id": "pipe-test", "op_id": "cancel"})
        await store._queue.join()
        await asyncio.sleep(0.05)
        await store.shutdown()

    asyncio.run(scenario())
    records = [r for message in sent if message["type"] == "store_status"
               for r in message["downloads"]]
    assert records[-1]["error"] == "cancelled"
    assert records[-1]["op_id"] == "cancel"
    assert records[-1]["kind"] == "cancel"


def test_marker_temp_symlink_is_not_followed(tmp_path):
    victim = tmp_path / 'victim'
    victim.write_text('safe')
    path = model_store._marker_path('pipe-test')
    path.parent.mkdir(parents=True)
    path.with_name(path.name + '.tmp').symlink_to(victim)
    model_store._write_marker('pipe-test', {'state': 'installed'})
    assert victim.read_text() == 'safe'
    assert json.loads(path.read_text())['state'] == 'installed'


def test_planning_metadata_does_not_write_hub_cache(monkeypatch, tmp_path, offline_hub):
    import huggingface_hub

    def fake_download(**kwargs):
        target = Path(kwargs['cache_dir']) / 'index'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}')
        return str(target)

    monkeypatch.setattr(huggingface_hub, 'hf_hub_download', fake_download)
    before = list(offline_hub.rglob('*'))
    assert model_store.hub_file_text('org/repo', REV, 'model_index.json') == '{}'
    assert list(offline_hub.rglob('*')) == before


def test_hub_volume_checks_blob_device(monkeypatch, tmp_path):
    folder = repo_folder('org/pipe')
    item = Item(path=folder / 'snapshots' / REV / 'weights', size=100,
                repo='org/pipe', folder=folder)
    seen = []
    monkeypatch.setattr(model_store, 'volume_of', lambda path: (seen.append(path) or (1, path)))
    monkeypatch.setattr(model_store, 'free_bytes', lambda _path: (10**12, 10**12))
    model_store._check_volumes([item])
    assert seen == [(folder / 'blobs').resolve()]


def test_cancel_without_active_install_has_result(monkeypatch):
    monkeypatch.setattr(model_store, "STATUS_INTERVAL_SECONDS", 0.01)
    sent = []
    async def scenario():
        store = new_store(pipeline_manifest())
        async def send(message):
            sent.append(message)
        store.attach(send, lambda _model: False)
        store.handle({'type': 'cancel_install', 'model_id': 'pipe-test', 'op_id': 'cancel'})
        await store._queue.join()
        await asyncio.sleep(0.05)
        result = store._ops['pipe-test']
        await store.shutdown()
        return result

    op = asyncio.run(scenario())
    assert (op.op_id, op.state, op.error) == ('cancel', 'failed', 'not_running')
    assert [m for m in sent if m['type'] == 'store_status'][-1]['downloads'][0]['kind'] == 'cancel'


def test_queued_removal_suppresses_install_publication(monkeypatch):
    monkeypatch.setattr(model_store, 'STATUS_INTERVAL_SECONDS', 0.01)
    entered = threading.Event()
    release = threading.Event()
    sent = []

    def download(_op, _manifest):
        entered.set()
        release.wait(5)

    async def send(message):
        sent.append(message)

    async def scenario():
        store = new_store(pipeline_manifest())
        monkeypatch.setattr(store, '_download', download)
        monkeypatch.setattr(model_store, '_delete_model_files', lambda *_args: None)
        store.attach(send, lambda _model: False)
        store.handle({'type': 'install_model', 'model_id': 'pipe-test', 'op_id': '1'})
        await asyncio.to_thread(entered.wait, 5)
        store.handle({'type': 'remove_model', 'model_id': 'pipe-test', 'op_id': '2'})
        await asyncio.sleep(0.05)
        release.set()
        await store._queue.join()
        await store.shutdown()
        assert 'pipe-test' not in store.installed

    asyncio.run(scenario())
    assert all(not message['models'] for message in sent if message['type'] == 'models_changed')
    queued = [r for m in sent if m['type'] == 'store_status' for r in m['downloads']
              if r['state'] == 'queued' and r['op_id'] == '2']
    assert queued and queued[-1]['kind'] == 'remove'


def test_operation_refreshes_shared_catalog_probes(monkeypatch):
    manifest = pipeline_manifest()
    _PROBES[manifest.id] = (123, False)
    monkeypatch.setattr(model_store, 'probe_model', lambda *_args: (7, False))
    monkeypatch.setattr(model_store, '_delete_model_files', lambda *_args: None)
    sent = []

    async def send(message):
        sent.append(message)

    async def scenario():
        store = new_store(manifest)
        store.attach(send, lambda _model: False)
        store.handle({'type': 'remove_model', 'model_id': manifest.id})
        await store._queue.join()
        await asyncio.gather(*(task for task in store._tasks if task is not store._drainer))
        await store.shutdown()

    asyncio.run(scenario())
    assert any(message['catalog'][0]['download_bytes'] == 7
               for message in sent if message['type'] == 'models_changed')


def test_registration_reconciles_change_after_hello(tmp_path):
    manifest = pipeline_manifest()
    store = ModelStore([manifest], FakeEngine(), str(tmp_path))

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

        async def recv(self):
            store.installed[manifest.id] = manifest
            return json.dumps({'type': 'registered', 'model_store': True})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    socket = Socket()
    asyncio.run(serve_connection(socket, Settings(worker_id='w-race'), [manifest],
                                 FakeEngine(), store))
    assert socket.sent[0]['models'] == []
    assert any(message['type'] == 'models_changed' and
               [row['id'] for row in message['models']] == [manifest.id]
               for message in socket.sent)


def test_benchmark_ignores_older_failure_and_other_workers(monkeypatch):
    benchmark = load_benchmark_script()
    monkeypatch.setattr(benchmark.time, 'sleep', lambda _seconds: None)

    class Client(AdminRows):
        def __init__(self):
            super().__init__([{}])
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            other = {'worker_id': 'other', 'models': [{'id': 'pipe-test', 'installed': True,
                                                       'needs_token': False}]}
            download = {'op_id': '001', 'state': 'failed', 'error': 'disk'}
            if self.calls > 2:
                download = {'op_id': '002', 'state': 'done'}
            selected = {'worker_id': 'w-bench', 'models': [{'id': 'pipe-test',
                        'installed': self.calls > 2, 'needs_token': False, 'download': download}]}
            return FakeResponse([selected, other] if self.calls == 1 else [other, selected])

    client = Client()
    benchmark.ensure_remote_installed(client, 'http://api', 'pipe-test')
    assert client.calls == 3


def test_cancel_too_late_reports_real_done_result(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def download(_op, _manifest):
        entered.set()
        release.wait(5)

    async def scenario():
        store = new_store(pipeline_manifest())
        monkeypatch.setattr(store, '_download', download)
        store.attach(lambda _message: _noop(), lambda _model: False)
        store.handle({'type': 'install_model', 'model_id': 'pipe-test', 'op_id': '1'})
        await asyncio.to_thread(entered.wait, 5)
        store.handle({'type': 'cancel_install', 'model_id': 'pipe-test', 'op_id': '2'})
        release.set()
        await store._queue.join()
        op = store._ops['pipe-test']
        await store.shutdown()
        return op

    op = asyncio.run(scenario())
    assert (op.op_id, op.state, op.error) == ('2', 'done', None)


def test_blob_devices_aggregate_requirements(monkeypatch):
    folder = repo_folder('org/pipe')
    items = [Item(path=folder / 'snapshots' / REV / name, size=60,
                  repo='org/pipe', folder=folder) for name in ('a', 'b')]
    blob_dir = (folder / 'blobs').resolve()
    monkeypatch.setattr(model_store, 'volume_of',
                        lambda path: (2, path) if path == blob_dir else (1, path))
    monkeypatch.setattr(model_store, 'free_bytes',
                        lambda path: (10**12, DISK_MARGIN_BYTES + (100 if path == blob_dir else 1000)))
    with pytest.raises(ModelStoreError, match='disk'):
        model_store._check_volumes(items)


def test_refused_remove_waits_for_earlier_cancel(tmp_path, monkeypatch):
    first = pipeline_manifest('blocking')
    second = pipeline_manifest()
    entered, release = threading.Event(), threading.Event()
    store = ModelStore([first, second], FakeEngine(), str(tmp_path))
    store.installed[second.id] = second

    def download(_op, _manifest):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(store, '_download', download)

    async def scenario():
        async def send(_message):
            pass
        store.attach(send, lambda model: model == second.id)
        store.handle({'type': 'install_model', 'model_id': first.id, 'op_id': '1'})
        assert await asyncio.to_thread(entered.wait, 5)
        store.handle({'type': 'cancel_install', 'model_id': second.id, 'op_id': '2'})
        store.handle({'type': 'remove_model', 'model_id': second.id, 'op_id': '3'})
        assert store._ops[second.id].state == 'queued'
        assert second.id in store.installed
        release.set()
        await store._queue.join()
        assert (store._ops[second.id].state, store._ops[second.id].error) == ('failed', 'in_use')
        await store.shutdown()

    asyncio.run(scenario())


def test_resume_counts_unique_remaining_blob_bytes(monkeypatch):
    folder = repo_folder('org/pipe')
    partial = folder / 'blobs' / ('a' * 64 + '.incomplete')
    partial.parent.mkdir(parents=True)
    with partial.open('wb') as out:
        out.truncate(9 * 2**30)
    item = model_store.Item(path=folder / 'snapshots' / REV / 'one', size=10 * 2**30,
                            repo='org/pipe', folder=folder, blob_key='a' * 64)
    other = model_store.Item(path=folder / 'snapshots' / REV / 'two', size=item.size,
                             repo=item.repo, folder=folder, blob_key=item.blob_key)
    monkeypatch.setattr(model_store, 'free_bytes',
                        lambda _p: (100 * 2**30, model_store.DISK_MARGIN_BYTES + 2 * 2**30))
    assert model_store.remaining_bytes([item, other]) == 10 * 2**30
    with pytest.raises(ModelStoreError, match='disk'):
        model_store._check_volumes([item, other])


def test_cancel_then_remove_deletes_partial(tmp_path, monkeypatch):
    manifest = pipeline_manifest()
    folder = repo_folder('org/pipe')
    partial = folder / 'blobs' / ('b' * 64 + '.incomplete')
    item = model_store.Item(path=folder / 'snapshots' / REV / 'file', size=100,
                            repo='org/pipe', folder=folder, blob_key='b' * 64)
    monkeypatch.setattr(model_store, 'plan_items', lambda *_: ([item], False))
    monkeypatch.setattr(model_store, 'free_bytes', lambda _p: (10**12, 10**12))

    def fetch(_item, _progress):
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b'partial')
        raise model_store._Cancelled()

    monkeypatch.setattr(model_store, '_fetch_hub', fetch)
    with pytest.raises(ModelStoreError, match='cancelled'):
        model_store.install_blocking(manifest, str(tmp_path))
    assert partial.exists()
    model_store._delete_model_files(manifest, str(tmp_path), [])
    assert not partial.exists()


def test_protocol6_registration_reconciles_change_after_hello(tmp_path):
    manifest = pipeline_manifest()
    store = ModelStore([manifest], FakeEngine(), str(tmp_path))

    class Fleet(ScriptedFleet):
        async def recv(self):
            store.installed[manifest.id] = manifest
            return await super().recv()

    fleet = Fleet('w-race-v6', [])
    asyncio.run(serve_connection(fleet, Settings(worker_id='w-race-v6'), [manifest],
                                 FakeEngine(), store))
    messages = [json.loads(raw) for raw in fleet.sent if isinstance(raw, str)]
    assert messages[0]['models'] == []
    assert any(message['type'] == 'models_changed' and
               [row['id'] for row in message['models']] == [manifest.id]
               for message in messages)


def test_remove_preserves_partial_planned_by_installed_model():
    removed = pipeline_manifest('removed')
    other = pipeline_manifest('other')
    folder = repo_folder('org/pipe')
    blob = folder / 'blobs' / ('c' * 64)
    partial = Path(str(blob) + '.incomplete')
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'partial')
    model_store._write_marker(removed.id, {'state': 'installing', 'blobs': [str(blob)]})
    model_store._write_marker(other.id, {'state': 'installed', 'blobs': [str(blob)]})
    model_store._delete_model_files(removed, '', [other])
    assert partial.exists()


def test_cpu_plan_matches_loader_and_marker_variant(monkeypatch, tmp_path):
    from worker.engine import DiffusersEngine

    manifest = pipeline_manifest(vae="")
    monkeypatch.setattr(model_store, "plan_items", REAL_PLAN_ITEMS)
    files = [("model_index.json", MODEL_INDEX_SIZE),
             ("unet/diffusion_pytorch_model.safetensors", 10),
             ("unet/diffusion_pytorch_model.fp16.safetensors", 5)]
    monkeypatch.setattr(model_store, "hub_listing",
                        lambda repo, rev: (files if repo == manifest.source else [("config.json", 1)], False, {}))
    monkeypatch.setattr(model_store, "hub_file_text", lambda *_: MODEL_INDEX)
    items, _ = model_store.plan_items(manifest, str(tmp_path), "cpu")
    names = {item.name for item in items}
    assert "unet/diffusion_pytorch_model.safetensors" in names
    assert "unet/diffusion_pytorch_model.fp16.safetensors" not in names
    torch = SimpleNamespace(float16=object(), float32=object())
    engine = object.__new__(DiffusersEngine)
    engine.device, engine.torch = "cpu", torch
    calls = []
    cls = SimpleNamespace(from_pretrained=lambda source, **kwargs: calls.append(kwargs))
    engine._from_pretrained(cls, manifest)
    assert calls[0]["torch_dtype"] is torch.float32
    assert "variant" not in calls[0]
    model_store._write_marker(manifest.id, {"state": "installed", "files": [],
                              "fingerprint": model_store._fingerprint(manifest, "cuda")})
    assert not model_store.is_installed(manifest, str(tmp_path), "cpu")


def test_remove_completed_blob_before_snapshot_publication(tmp_path):
    manifest = pipeline_manifest(vae="")
    folder = repo_folder(manifest.source)
    blob = folder / "blobs" / "completed"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"data")
    partial = Path(str(blob) + ".incomplete")
    partial.write_bytes(b"partial")
    model_store._write_marker(manifest.id, {"state": "installing", "blobs": [str(blob)]})
    _delete_model_files(manifest, str(tmp_path), [])
    assert not blob.exists()
    assert not partial.exists()


def test_shared_categorizer_does_not_probe_before_hello(monkeypatch, tmp_path):
    put_snapshot_file(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "config.json")
    calls = []
    monkeypatch.setattr(model_store, "plan_items", lambda *args: calls.append(args) or ([], False))
    fleet = ScriptedFleet("w-n1", [], settle_on=lambda raw: '"store_inventory"' in raw)
    original_send = fleet.send

    async def send(data):
        if json.loads(data)["type"] == "hello":
            assert calls == []
        await original_send(data)

    fleet.send = send
    asyncio.run(serve_connection(fleet, Settings(worker_id="w-n1", models_dir=str(tmp_path)),
                                 [pipeline_manifest(vae="")], FakeEngine()))
    assert fleet.sent


def test_inventory_reports_highest_retained_operation(tmp_path):
    async def scenario():
        store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
        store._ops["a"] = model_store.Operation("a", op_id="00000000000000005000-aaaa")
        store._ops["b"] = model_store.Operation("b", op_id="00000000000000004000-bbbb")
        messages = []

        async def send(message):
            messages.append(message)

        store.attach(send, lambda _: False)
        await store.send_inventory()
        assert messages[0]["op_id"] == "00000000000000005000-aaaa"
        await store.close()
    asyncio.run(scenario())


def test_read_only_cache_does_not_abort_scan(monkeypatch, tmp_path, caplog):
    complete_pipeline()
    complete_companions()
    hub = model_store.hub_cache_root()
    hub.chmod(0o555)
    monkeypatch.setattr(model_store, '_write_marker', lambda *_: (_ for _ in ()).throw(PermissionError()))
    store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
    try:
        asyncio.run(store.scan())
    finally:
        hub.chmod(0o755)
    assert not store.installed
    assert caplog.text.count('cannot adopt model') == 1


def test_attach_retries_legacy_adoption(monkeypatch, tmp_path):
    complete_pipeline()
    complete_companions()
    monkeypatch.setattr(model_store, 'plan_items', _offline)
    sent = []
    async def send(message):
        sent.append(message)
    async def scenario():
        store = ModelStore([pipeline_manifest()], FakeEngine(), str(tmp_path))
        await store.scan()
        assert not store.installed
        monkeypatch.setattr(model_store, 'plan_items', registry_plan)
        store.attach(send, lambda _: False)
        await store._probe_task
        await asyncio.gather(*list(store._tasks))
        assert 'pipe-test' in store.installed
        assert any(m['type'] == 'models_changed' and m['models'] for m in sent)
        await store.shutdown()
    asyncio.run(scenario())


def test_removal_preserves_outside_link_and_marker_blob(tmp_path):
    manifest = pipeline_manifest(vae='')
    outside = tmp_path / 'outside' / 'blobs' / 'file'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'keep')
    link = snapshot_dir(manifest.source, REV) / 'x'
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    marker_file = outside.with_name('marker')
    marker_file.write_bytes(b'keep')
    model_store._write_marker(manifest.id, {'blobs': [str(marker_file)]})
    _delete_model_files(manifest, str(tmp_path), [])
    assert outside.read_bytes() == b'keep'
    assert marker_file.read_bytes() == b'keep'


def test_oversized_upscale_response_stops_reading(monkeypatch, tmp_path):
    class Response:
        calls = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _size):
            self.calls += 1
            return b'xxxx' if self.calls == 1 else b''
    response = Response()
    monkeypatch.setattr(model_store.urllib.request, 'urlopen', lambda *a, **k: response)
    item = Item(path=tmp_path / 'file', size=3, url='https://example.com', root=tmp_path)
    with pytest.raises(ModelStoreError):
        model_store._fetch_url(item, lambda _: None)
    assert response.calls == 1
    assert not item.path.exists()


def test_uuid_partial_is_removed_and_not_resumed(tmp_path):
    manifest = pipeline_manifest(vae='')
    folder = repo_folder(manifest.source)
    blob = folder / 'blobs' / ('a' * 64)
    partial = blob.with_name(blob.name + '.1234abcd.incomplete')
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'partial')
    item = Item(path=folder / 'snapshots' / REV / 'x', size=100, folder=folder, blob_key=blob.name)
    assert model_store.remaining_bytes([item]) == 100
    model_store._write_marker(manifest.id, {'blobs': [str(blob)]})
    _delete_model_files(manifest, str(tmp_path), [])
    assert not partial.exists()


def test_queue_does_not_wait_for_catalog_refresh(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    started = []
    def probe(*_args):
        entered.set()
        release.wait(5)
        return (1, False)
    monkeypatch.setattr(model_store, 'probe_model', probe)
    monkeypatch.setattr(model_store, '_delete_model_files', lambda *_: started.append(True))
    async def scenario():
        store = new_store(pipeline_manifest())
        store.attach(lambda _: _noop(), lambda _: False)
        store.handle({'type': 'remove_model', 'model_id': 'pipe-test'})
        store.handle({'type': 'remove_model', 'model_id': 'pipe-test'})
        try:
            await asyncio.wait_for(store._queue.join(), 2)
            assert len(started) == 2
        finally:
            release.set()
            await store.shutdown()
    asyncio.run(scenario())


def test_probe_invalidation_only_covers_shared_repositories():
    first = pipeline_manifest('first')
    shared = pipeline_manifest('shared')
    unrelated = upscale_manifest('https://example.com/model', [2])
    store = ModelStore([first, shared, unrelated], FakeEngine(), '')
    _PROBES.update({m.id: (7, False) for m in (first, shared, unrelated)})
    store._invalidate_probes(first.id)
    assert set(_PROBES) == {unrelated.id}
