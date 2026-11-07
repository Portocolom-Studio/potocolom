"""Install, cancel and remove the weights behind the bundled models (issue #793).

Weights for a Hugging Face repository sit in the hub cache at the manifest's
pinned revision. Upscaler weights sit under <models_dir>/.cache/upscale/.

A model is installed when its marker says so: the marker is written only after
every planned file is on disk at its planned size, and it records the files and
the fingerprint of the pins they came from. A download in progress leaves an
"installing" marker, so a partial download is never installed. A cache filled
before markers existed is adopted when the exact hub plan matches the local
files; bundled metadata makes pinned plans available offline.

The worker process runs one queue of operations, in the order they were asked
for, and one task drains it. Only one operation (an install or a removal) runs
at a time, so a removal never deletes files under an install that is reading
them, and nothing can overtake an operation that was queued before it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from functools import lru_cache
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from worker.candidate_pipelines import CANDIDATE_PIPELINES, CUSTOM_PIPELINES, plain_repo_patterns
from worker.categorize import SOURCE as CATEGORIZER_SOURCE, SOURCE_REVISION as CATEGORIZER_REVISION
from worker.gpu_device import weight_config
from worker.manifests import Manifest
from worker.upscale import weight_cache_path, weight_url

logger = logging.getLogger("potocolom.worker")

# The capability string in hello. The API sends install, cancel and remove only
# to a worker that lists it, and a worker sends store messages only after the API
# signals model_store in its registration reply.
CAPABILITY = "model_store"
# Free space kept on each volume a download writes to, so a full volume cannot
# take the database, the logs or the next model down with it.
DISK_MARGIN_BYTES = 5 * 2**30
# At most one store_status per second while something changes.
STATUS_INTERVAL_SECONDS = 1.0
ADOPTION_RETRY_SECONDS = 5.0
UPSCALE_CHUNK_BYTES = 1 << 20
GIB = 2**30
WEIGHT_SUFFIXES = (".safetensors", ".bin")
# Files no load reads: documentation, images, and the framework formats the
# worker never loads (flax, onnx, tensorflow, checkpoint pickles).
IGNORED_SUFFIXES = (
    ".md", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".msgpack", ".h5", ".onnx",
    ".ot", ".ckpt", ".pb", ".tflite", ".npz", ".gguf",
)
_SHARD_SUFFIX = re.compile(r"-\d{5}-of-\d{5}$")
# Probes are invalidated after operations that can change shared files.
_PROBES: dict[str, tuple[int | None, bool]] = {}


class ModelStoreError(Exception):
    """A refused or failed operation. `reason` is a fixed token, never free text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Cancelled(Exception):
    """Raised inside the download thread when the operation was cancelled."""


@dataclass(frozen=True)
class RepoRef:
    """One hub repository a load reads, at the revision the manifest pins."""

    repo: str
    revision: str
    # pipeline: a diffusers directory (model_index.json); component: a repository
    # that loads weights from its root or folders; adapter: the one file a LoRA
    # load names.
    kind: str
    variant: str = ""
    filename: str = ""


@dataclass(frozen=True)
class Item:
    """One file a download must produce.

    `url` is set for upscaler weights. `root` is the directory the file must stay
    inside. `blob_key` is the hub's name for the file's content (sha256 or git
    sha1): when that blob is already complete, the file needs only a pointer.
    """

    path: Path
    size: int
    repo: str = ""
    revision: str = ""
    name: str = ""
    url: str = ""
    folder: Path | None = None
    root: Path | None = None
    blob_key: str = ""


@dataclass
class Operation:
    model_id: str
    op_id: str | None = None
    kind: str = "install"  # install, remove, cancel or refusal
    state: str = "queued"  # queued, downloading, done, failed
    bytes_done: int = 0
    bytes_total: int = 0
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    targets: list[Operation] = field(default_factory=list)


# Loaders that read a plain repository (weights and a config beside them), not a
# diffusers directory with a model_index.json.
PLAIN_REPO_PIPELINES = frozenset({"Iris3B"})


def is_hub_source(source: str) -> bool:
    return bool(source) and "://" not in source and not source.startswith((".", "/", "~"))


def is_local_source(source: str) -> bool:
    return bool(source) and "://" not in source and not is_hub_source(source)


def hub_cache_root() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE)


def repo_folder(repo: str) -> Path:
    return hub_cache_root() / f"models--{repo.replace('/', '--')}"


def snapshot_dir(repo: str, revision: str) -> Path:
    return repo_folder(repo) / "snapshots" / revision


def markers_root() -> Path:
    return hub_cache_root() / ".potocolom" / "installs"


def upscale_root(models_dir: str) -> Path:
    return Path(models_dir or ".") / ".cache" / "upscale"


def contained(path: Path, root: Path) -> bool:
    """True when `path` resolves to a location inside `root`, after symlinks and `..`."""
    real_root = os.path.realpath(root)
    return os.path.commonpath([os.path.realpath(path), real_root]) == real_root


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def volume_of(path: Path) -> tuple[int, Path]:
    """(device id, a directory on that volume) for the volume `path` will be written to."""
    probe = _nearest_existing(path)
    return os.stat(probe).st_dev, probe


def free_bytes(path: Path) -> tuple[int, int]:
    """(total, free) bytes on the volume that holds `path` or its nearest existing parent."""
    usage = shutil.disk_usage(_nearest_existing(path))
    return usage.total, usage.free


def repo_refs(manifest: Manifest, device: str = "cuda") -> list[RepoRef]:
    """Every hub repository the load of this manifest reads."""
    refs: list[RepoRef] = []
    # The pipeline asks for fp16 files only when it runs in fp16 (the GPU default);
    # the companion repositories load their default files.
    _dtype, variant = weight_config(manifest.dtype, device)
    if is_hub_source(manifest.source):
        kind = ("component" if manifest.pipeline in PLAIN_REPO_PIPELINES else
                "modular_pipeline" if manifest.pipeline == "Flux3Distilled" else "pipeline")
        refs.append(RepoRef(manifest.source, manifest.source_revision, kind, variant))
    if manifest.vae:
        refs.append(RepoRef(manifest.vae, manifest.vae_revision, "component"))
    if manifest.lora:
        repo, _, name = manifest.lora.rpartition("/")
        refs.append(RepoRef(repo, manifest.lora_revision, "adapter", filename=name))
    if manifest.t2i_adapter:
        refs.append(RepoRef(manifest.t2i_adapter, manifest.t2i_adapter_revision, "component"))
    if manifest.preview_decoder:
        refs.append(RepoRef(manifest.preview_decoder,
                            manifest.preview_decoder_revision, "component"))
    refs.extend(RepoRef(extra.repo, extra.revision, "component") for extra in manifest.worker_repos)
    if "upscale" not in manifest.capabilities:
        # The output categorizer loads this repository on a job's first output. It is
        # a companion of every diffusion model, so the store counts it in the size,
        # installs it with the first model and removes it with the last.
        refs.append(RepoRef(CATEGORIZER_SOURCE, CATEGORIZER_REVISION, "component"))
    return refs


def _upscale_dir(manifest: Manifest, models_dir: str) -> Path:
    """The directory a model's upscaler weights live in. It must be a direct child of the
    upscaler cache: an id such as ".." would name the cache's parent, and is refused."""
    root = upscale_root(models_dir)
    model_dir = root / manifest.id
    if not contained(model_dir, root) or model_dir.resolve().parent != root.resolve():
        raise ModelStoreError("download_failed")
    return model_dir


def upscale_targets(manifest: Manifest, models_dir: str) -> list[tuple[str, Path]]:
    """(url, cache path) for each factor the manifest offers, each inside the model's cache."""
    cache = _upscale_dir(manifest, models_dir)
    factor_spec = manifest.parameters.get("properties", {}).get("factor", {})
    factors = factor_spec.get("enum") or [int(factor_spec.get("default", 2))]
    targets = []
    for factor in factors:
        path = weight_cache_path(models_dir, manifest.id, factor, manifest.source)
        if not contained(path, cache):
            raise ModelStoreError("download_failed")
        targets.append((weight_url(manifest.source, factor), path))
    return targets


# ---- markers: what the store says is installed --------------------------------

def _fingerprint(manifest: Manifest, device: str = "cuda") -> str:
    """What a model's files depend on: its repositories at their pins, and its upscaler
    source and factors. A changed pin is a different model, whatever the disk holds."""
    factors = manifest.parameters.get("properties", {}).get("factor", {})
    refs = [[ref.repo, ref.revision, ref.kind, ref.variant, ref.filename]
            for ref in repo_refs(manifest, device)]
    shape = [manifest.source, manifest.source_revision,
             sorted(factors.get("enum") or []), factors.get("default")]
    return hashlib.sha256(json.dumps([refs, shape], sort_keys=True).encode()).hexdigest()


def _marker_path(model_id: str) -> Path:
    path = markers_root() / f"{model_id}.json"
    if not contained(path, markers_root()):
        raise ModelStoreError("download_failed")
    return path


def _read_marker(model_id: str) -> dict | None:
    try:
        data = json.loads(_marker_path(model_id).read_text())
    except (OSError, ValueError, ModelStoreError):
        return None
    return data if isinstance(data, dict) else None


def _write_marker(model_id: str, data: dict) -> None:
    path = _marker_path(model_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(data, sort_keys=True))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _remove_marker(model_id: str) -> None:
    _marker_path(model_id).unlink(missing_ok=True)


def _size_ok(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def _blob_ready(item: Item) -> bool:
    """True when the content the item needs is already complete in the hub cache."""
    if not item.blob_key or item.folder is None:
        return False
    return _size_ok(item.folder / "blobs" / item.blob_key, item.size)


def _transfer_size(item: Item) -> int:
    if _size_ok(item.path, item.size) or _blob_ready(item):
        return 0
    return item.size


def _transfer_key(item: Item) -> Path:
    if item.folder is not None and item.blob_key:
        return item.folder / "blobs" / item.blob_key
    return item.path


def remaining_bytes(items: list[Item]) -> int:
    transfers: dict[Path, int] = {}
    for item in items:
        key = _transfer_key(item)
        transfers[key] = max(transfers.get(key, 0), _transfer_size(item))
    return sum(transfers.values())


def is_installed(manifest: Manifest, models_dir: str, device: str = "cuda") -> bool:
    """True when the marker says installed for these pins and every file it lists is
    still on disk at its recorded size."""
    marker = _read_marker(manifest.id)
    if marker is None or marker.get("state") != "installed":
        return False
    if (marker.get("fingerprint") != _fingerprint(manifest, device)
            or marker.get("variant", weight_config(manifest.dtype, device)[1])
            != weight_config(manifest.dtype, device)[1]):
        return False
    if is_local_source(manifest.source) and not Path(manifest.source).expanduser().exists():
        return False
    try:
        files = [(Path(entry["path"]), int(entry["size"])) for entry in marker["files"]]
    except (KeyError, TypeError, ValueError):
        return False
    return all(_size_ok(path, size) for path, size in files)


def _any_local_copy(manifest: Manifest, models_dir: str) -> bool:
    """A cheap look, before any network call: is there anything on disk for this model?"""
    if "upscale" in manifest.capabilities:
        try:
            return any(path.exists() for _url, path in upscale_targets(manifest, models_dir))
        except ModelStoreError:
            return False
    return (is_hub_source(manifest.source)
            and snapshot_dir(manifest.source, manifest.source_revision).is_dir())


def adopt_legacy(manifest: Manifest, models_dir: str, device: str = "cuda") -> bool | None:
    """Adopt a cache filled before markers existed, once, when the exact hub plan an install
    would use is complete on disk. If metadata is unavailable nothing is adopted. Never adopts over
    a marker of any kind, since an interrupted download leaves one. Returns False for a
    definitively incomplete plan, and None when adoption might be possible later."""
    if _read_marker(manifest.id) is not None or not _any_local_copy(manifest, models_dir):
        return None
    try:
        items, _gated = plan_items(manifest, models_dir, device)
        if not all(_size_ok(item.path, item.size) for item in items):
            return False
        _write_marker(manifest.id, {
            "state": "installed",
            "fingerprint": _fingerprint(manifest, device),
            "variant": weight_config(manifest.dtype, device)[1],
            "files": [{"path": str(item.path), "size": item.size} for item in items],
        })
    except OSError:
        logger.warning("cannot adopt model %s: cache is inaccessible", manifest.id)
        return None
    except (ModelStoreError, ValueError):
        return None
    return True


# ---- planning ------------------------------------------------------------------

def _ignored(name: str) -> bool:
    if any(part.startswith(".") for part in name.split("/")):
        return True
    return name.lower().endswith(IGNORED_SUFFIXES)


def _weight_group(name: str) -> tuple[str, str, str, str] | None:
    """(directory, base name, variant, extension) for a weight file or its index."""
    weight_name = name[: -len(".index.json")] if name.endswith(".index.json") else name
    parts = weight_name.rsplit("/", 1)
    directory, filename = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    base_stem, dot, extension = filename.rpartition(".")
    if not dot or f".{extension}" not in WEIGHT_SUFFIXES:
        return None
    # Hub shards name the variant before the shard count: name.fp16-00001-of-00002.
    base_stem = _SHARD_SUFFIX.sub("", base_stem)
    root, dot, variant = base_stem.rpartition(".")
    if not dot:
        root, variant = base_stem, ""
    return directory, root, variant, f".{extension}"


# What diffusers' download (pipeline_utils.DiffusionPipeline.download) selects. A weight file is
# <prefix>[.<variant>][-NNNNN-of-NNNNN].<suffix>; its index is <prefix>.<suffix>.index.json.
_WEIGHT_PREFIXES = ("diffusion_pytorch_model", "model", "pytorch_model")
_WEIGHT_SUFFIXES = ("bin", "safetensors")
# The weight prefix a VAE repository's load reads (AutoencoderKL.from_pretrained).
_VAE_PREFIXES = ("diffusion_pytorch_model",)
# Endings that make a folder a model folder (filter_model_files).
_MODEL_FILE_ENDINGS = (".bin", ".safetensors", ".onnx", ".onnx_data")
# The ignore patterns _get_ignore_patterns returns for the engine's defaults (no onnx). The
# .bin patterns apply only when the safetensors set is complete; otherwise safetensors are ignored.
_IGNORED_WHEN_COMPLETE = ("*.bin", "*.msgpack", "*.onnx", "*.pb")
_IGNORED_WHEN_INCOMPLETE = ("*.safetensors", "*.msgpack", "*.onnx", "*.pb")
# Root files a pipeline load reads when they exist (diffusers' root allow list).
_ROOT_FILES = frozenset({
    "model_index.json", "config.json", "scheduler_config.json", "pipeline.py",
    "chat_template.jinja", "generation_config.json", "preprocessor_config.json",
    "processor_config.json", "tokenizer.json", "tokenizer_config.json",
})


def _diffusers_ignored(name: str, complete: bool) -> bool:
    """True when diffusers' ignore patterns drop the file, its index included."""
    patterns = _IGNORED_WHEN_COMPLETE if complete else _IGNORED_WHEN_INCOMPLETE
    return any(fnmatchcase(name, pattern) or fnmatchcase(name, f"{pattern}.index.*json")
               for pattern in patterns)


def _weight_names(names: list[str], variant: str,
                  prefixes: tuple[str, ...] = _WEIGHT_PREFIXES,
                  legacy: bool = False) -> tuple[set[str], int]:
    """The weight and index names one load reads, chosen the way diffusers chooses them
    (variant_compatible_siblings). Each folder takes its variant files when it has any, and
    its default files otherwise; the root counts as one folder. `legacy` adds the older
    shard layout diffusers reads for a variant (name-NNNNN-of-NNNNN.<variant>.<ext>, indexed
    by name.<ext>.<variant>.index.json); diffusers' is_safetensors_compatible does not read
    it, so that check passes legacy=False. Returns the names and how many variant files were
    found, so a caller can fall back to the default."""
    prefix_re = "|".join(prefixes)
    suffix_re = "|".join(_WEIGHT_SUFFIXES)
    default_file = re.compile(rf"({prefix_re})(-\d{{5}}-of-\d{{5}})?\.({suffix_re})$")
    default_index = re.compile(rf"({prefix_re})\.({suffix_re})\.index\.json")
    folders: dict[str, list[str]] = {}
    for name in names:
        parts = name.split("/")
        folders.setdefault(parts[0] if len(parts) == 2 else "", []).append(name)
    chosen: set[str] = set()
    variant_hits = 0
    for folder_names in folders.values():
        if variant:
            variant_file = re.compile(
                rf"({prefix_re})\.({variant}|{variant}-\d{{5}}-of-\d{{5}})\.({suffix_re})$")
            variant_index = re.compile(rf"({prefix_re})\.({suffix_re})\.index\.{variant}\.json$")
            found = {name for name in folder_names
                     if variant_file.match(name.rsplit("/", 1)[-1])}
            if found:
                variant_hits += len(found)
                chosen.update(found | {name for name in folder_names
                                       if variant_index.match(name.rsplit("/", 1)[-1])})
                continue
            if legacy:
                legacy_file = re.compile(rf".*-\d{{5}}-of-\d{{5}}\.{variant}\.[a-z]+$")
                legacy_index = re.compile(
                    rf"({prefix_re})\.({suffix_re})\.{variant}\.index\.json$")
                found = {name for name in folder_names
                         if legacy_file.match(name.rsplit("/", 1)[-1])}
                if found:
                    variant_hits += len(found)
                    chosen.update(found | {name for name in folder_names
                                           if legacy_index.match(name.rsplit("/", 1)[-1])})
                    continue
        chosen.update(name for name in folder_names
                      if default_file.match(name.rsplit("/", 1)[-1])
                      or default_index.match(name.rsplit("/", 1)[-1]))
    return chosen, variant_hits


def _model_folders(names: list[str], folders: set[str]) -> set[str]:
    """The component folders that hold weights (filter_model_files and folder_names)."""
    return {name.split("/")[0] for name in names
            if name.count("/") == 1 and name.endswith(_MODEL_FILE_ENDINGS)
            and name.split("/")[0] in folders}


def _safetensors_complete(names: list[str], folders: set[str], passed: frozenset[str],
                          variant: str, prefixes: tuple[str, ...] = _WEIGHT_PREFIXES) -> bool:
    """diffusers' is_safetensors_compatible: every component folder that holds weights, other
    than the passed ones, has a safetensors file among the weights a load reads. When there
    are model folders only those are checked; with no component folder, the root decides."""
    model = _model_folders(names, folders)
    scope = [name for name in names if name.endswith(_MODEL_FILE_ENDINGS)
             and (not model or (name.count("/") == 1 and name.split("/")[0] in model))]
    chosen, _hits = _weight_names(scope, variant, prefixes)
    components = {name.split("/")[0] for name in scope
                  if name.count("/") == 1 and name.split("/")[0] not in passed}
    if not components:
        return any(name.endswith(".safetensors") for name in chosen)
    return all(any(name.startswith(folder + "/") and name.endswith(".safetensors")
                   for name in chosen)
               for folder in components)


def _selected(names: list[str], folders: set[str], passed: frozenset[str],
              variant: str) -> tuple[set[str], set[str], int]:
    """The weight names one pipeline load reads for this variant, the names diffusers' ignore
    patterns keep, and how many variant files exist. The patterns depend on whether the
    safetensors set is complete for this variant."""
    complete = _safetensors_complete(names, folders, passed, variant)
    kept = {name for name in names if not _diffusers_ignored(name, complete)}
    chosen, hits = _weight_names(sorted(kept), variant, legacy=True)
    return chosen, kept, hits


def select_pipeline_plan(files: list[tuple[str, int]], index: dict, variant: str, *,
                         passed: frozenset[str] = frozenset(),
                         custom_pipeline: str = "") -> list[tuple[str, int]]:
    """The files DiffusionPipeline.from_pretrained reads, as the download plans them.

    model_index.json and the root files a load reads. The `_ignore_files` the index lists are
    dropped first. Each component folder the index names that is not `passed` (a component
    the caller hands in, such as the VAE): a folder with weights keeps its selected weights and
    config.json, and nothing else (a generation config it ships is not read); any other folder
    keeps every file. Also the custom pipeline module (`custom_pipeline`.py) and each
    component's own module (`<folder>/<module>.py`). A variant the repository lacks falls back
    to the default files, as the loader does."""
    sizes = dict(files)
    ignored = set(index.get("_ignore_files", []))
    names = [name for name in sizes if name not in ignored]
    folders = {key for key, value in index.items()
               if key not in ("_class_name", "_ignore_files") and isinstance(value, list)}
    chosen, kept, hits = _selected(names, folders, passed, variant)
    if variant and not hits:
        chosen, kept, _hits = _selected(names, folders, passed, "")
    model_folders = _model_folders(names, folders)
    wanted = set(chosen) | {name for name in _ROOT_FILES if name in kept}
    for folder in folders - passed:
        if folder in model_folders:
            wanted.add(f"{folder}/config.json")
        else:
            wanted.update(name for name in kept if name.startswith(folder + "/"))
        value = index[folder]
        if value and isinstance(value[0], str) and f"{folder}/{value[0]}.py" in sizes:
            wanted.add(f"{folder}/{value[0]}.py")
    if custom_pipeline and f"{custom_pipeline}.py" in kept:
        wanted.add(f"{custom_pipeline}.py")
    return sorted((name, sizes[name]) for name in wanted
                  if name in kept and not (name.count("/") == 1
                                           and name.split("/")[0] in passed))


def select_vae_plan(files: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The files AutoencoderKL.from_pretrained reads from a VAE repository: config.json and
    its diffusion weights at the root (the engine passes no variant). Safetensors when they
    are complete, else the pickle weights (ModelMixin's fallback). Weights under another
    prefix, such as a stray model.safetensors, are not read and are not planned."""
    sizes = dict(files)
    root = [name for name in sizes if "/" not in name]
    complete = _safetensors_complete(root, set(), frozenset(), "", _VAE_PREFIXES)
    kept = [name for name in root if not _diffusers_ignored(name, complete)]
    chosen, _hits = _weight_names(kept, "", _VAE_PREFIXES)
    keep = chosen | ({"config.json"} & set(kept))
    return sorted((name, sizes[name]) for name in keep)


def select_plain_plan(files: list[tuple[str, int]],
                      patterns: tuple[str, ...]) -> list[tuple[str, int]]:
    """The files a plain repository load reads: the names `patterns` lists, by fnmatch."""
    return sorted((name, size) for name, size in files
                  if any(fnmatchcase(name, pattern) for pattern in patterns))


def select_plan(files: list[tuple[str, int]], variant: str) -> list[tuple[str, int]]:
    """The files one load reads, chosen the way diffusers chooses them.

    Each weight group (directory and base name) takes the requested variant when
    the repository has one, else the default, and safetensors over bin. Index
    files travel with the group they index. Everything that is not a weight
    file, and is not ignored, is kept: configs, tokenizers and schedulers.
    """
    groups: dict[tuple[str, str], dict[str, dict[str, list[tuple[str, int]]]]] = {}
    plan: list[tuple[str, int]] = []
    for name, size in files:
        if _ignored(name):
            continue
        group = _weight_group(name)
        if group is None:
            plan.append((name, size))
            continue
        directory, base, file_variant, extension = group
        by_variant = groups.setdefault((directory, base), {})
        by_variant.setdefault(file_variant, {}).setdefault(extension, []).append((name, size))
    for by_variant in groups.values():
        if variant and variant in by_variant:
            chosen = by_variant[variant]
        elif "" in by_variant:
            chosen = by_variant[""]
        else:
            chosen = by_variant[sorted(by_variant)[0]]
        extension = ".safetensors" if ".safetensors" in chosen else ".bin"
        plan.extend(chosen[extension])
    return sorted(plan)


def hub_listing(repo: str, revision: str) -> tuple[list[tuple[str, int]], bool, dict[str, str]]:
    """(files with sizes, gated, blob key per file) for a repository at a revision, from hub
    metadata. A file's blob key is its LFS sha256, or its git blob id for a small file."""
    from huggingface_hub import HfApi

    try:
        info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    except Exception as error:
        logger.warning("hub metadata for %s unavailable (%s)", repo, type(error).__name__)
        raise ModelStoreError("unavailable") from error
    files: list[tuple[str, int]] = []
    blob_keys: dict[str, str] = {}
    for sibling in info.siblings or []:
        files.append((sibling.rfilename, sibling.size or 0))
        lfs = getattr(sibling, "lfs", None)
        key = getattr(lfs, "sha256", None) or getattr(sibling, "blob_id", None)
        if key:
            blob_keys[sibling.rfilename] = key
    return files, bool(info.gated), blob_keys


def hub_file_text(repo: str, revision: str, name: str) -> str:
    """One small file of a repository at a revision, without modifying the hub cache."""
    from huggingface_hub import hf_hub_download

    try:
        with tempfile.TemporaryDirectory() as cache:
            path = hf_hub_download(repo_id=repo, filename=name, revision=revision, cache_dir=cache)
            return Path(path).read_text()
    except Exception as error:
        if _is_gated_error(error):
            raise ModelStoreError("needs_token") from error
        raise ModelStoreError("unavailable") from error


def url_size(url: str) -> int:
    """The byte count an upscaler download must produce. A response that does not
    declare one is refused: a download with no declared size cannot be sized or checked."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            declared = response.headers.get("Content-Length")
    except (OSError, ValueError) as error:
        logger.warning("size of upscale weights unavailable (%s)", type(error).__name__)
        raise ModelStoreError("unavailable") from error
    if not declared or not declared.isdigit() or int(declared) <= 0:
        raise ModelStoreError("download_failed")
    return int(declared)


def _dedupe(items: list[Item]) -> list[Item]:
    """One item per destination: two factors that name one file are one download."""
    unique: dict[Path, Item] = {}
    for item in items:
        unique.setdefault(item.path, item)
    return list(unique.values())


@lru_cache(maxsize=1)
def bundled_hub_metadata() -> dict:
    return json.loads(Path(__file__).with_name("hub_lock.json").read_text())


def plan_items(manifest: Manifest, models_dir: str,
               device: str = "cuda") -> tuple[list[Item], frozenset[str]]:
    """Every file a full install of the manifest produces, and the gated repositories among
    the repositories it reads (a repository is gated when its files need a token)."""
    if "upscale" in manifest.capabilities:
        cache = _upscale_dir(manifest, models_dir)
        weights = [Item(path=path, size=(bundled_hub_metadata()["urls"][url]
                                if url in bundled_hub_metadata()["urls"] else url_size(url)),
                        url=url, root=cache)
                   for url, path in upscale_targets(manifest, models_dir)]
        return _dedupe(weights), frozenset()
    gated: set[str] = set()
    items: list[Item] = []
    for ref in repo_refs(manifest, device):
        locked = bundled_hub_metadata()["repos"].get(f"{ref.repo}@{ref.revision}")
        if locked is None:
            files, ref_gated, blob_keys = hub_listing(ref.repo, ref.revision)
        else:
            files, ref_gated, blob_keys = locked["files"], locked["gated"], locked["blobs"]
        if ref_gated:
            gated.add(ref.repo)
        if ref.kind == "adapter":
            chosen = [(name, size) for name, size in files if name == ref.filename]
            if not chosen:
                raise ModelStoreError("unavailable")
        elif ref.kind == "modular_pipeline":
            index_text = (hub_file_text(ref.repo, ref.revision, "modular_model_index.json")
                          if locked is None else locked.get("modular_model_index"))
            if index_text is None:
                raise ModelStoreError("unavailable")
            index = json.loads(index_text)
            chosen = [(name, size) for name, size in files if name == "modular_model_index.json"]
            if not chosen:
                raise ModelStoreError("unavailable")
            for component in ("text_encoder", "tokenizer", "transformer", "vae"):
                try:
                    spec = index[component][2]
                    folder = spec["subfolder"]
                    if spec["pretrained_model_name_or_path"] != ref.repo or not folder:
                        raise ValueError
                    component_files = [(name, size) for name, size in files
                                       if name.startswith(folder + "/")]
                    if not component_files:
                        raise ValueError
                    chosen.extend(select_plan(component_files, spec.get("variant") or ""))
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise ModelStoreError("unavailable") from error
        elif ref.kind == "pipeline":
            index_text = (hub_file_text(ref.repo, ref.revision, "model_index.json")
                          if locked is None else locked.get("model_index"))
            if index_text is None:
                raise ModelStoreError("unavailable")
            index = json.loads(index_text)
            # Only the engine's own loads hand in a replacement VAE; a candidate pipeline's
            # loader ignores `manifest.vae` and reads the stock one.
            passed = (frozenset({"vae"}) if manifest.vae
                      and manifest.pipeline not in CANDIDATE_PIPELINES else frozenset())
            custom = CUSTOM_PIPELINES.get(manifest.pipeline, "")
            chosen = select_pipeline_plan(files, index, ref.variant, passed=passed,
                                          custom_pipeline=custom)
        elif ref.repo == manifest.vae:
            chosen = select_vae_plan(files)
        elif (patterns := plain_repo_patterns(manifest, ref.repo)) is not None:
            chosen = select_plain_plan(files, patterns)
        else:
            chosen = select_plan(files, ref.variant)
        root = snapshot_dir(ref.repo, ref.revision)
        items.extend(
            Item(path=root / name, size=size, repo=ref.repo, revision=ref.revision,
                 name=name, folder=repo_folder(ref.repo), root=hub_cache_root(),
                 blob_key=blob_keys.get(name, ""))
            for name, size in chosen
        )
    return _dedupe(items), frozenset(gated)


def _needs_token(items: list[Item], gated: frozenset[str]) -> bool:
    """True when a file the install still has to fetch comes from a gated repository. A
    complete gated cache links without a fetch, and a gated model whose only missing files
    come from ungated companions installs without a token."""
    return any(item.repo in gated and not _size_ok(item.path, item.size)
               and not _blob_ready(item) for item in items)


def _gate_known(manifest: Manifest) -> bool | None:
    """Whether a token is needed, from the listings alone. None when no listing answered."""
    answered = False
    for ref in repo_refs(manifest):
        try:
            _files, gated, _keys = hub_listing(ref.repo, ref.revision)
        except ModelStoreError:
            continue
        answered = True
        if gated:
            return True
    return False if answered else None


def probe_model(manifest: Manifest, models_dir: str, device: str = "cuda") -> tuple[int | None, bool] | None:
    """(bytes still to fetch or None, needs a token) from hub metadata. The gate is kept
    even when the size cannot be found. None when nothing is known at all."""
    token = bool(os.environ.get("HF_TOKEN"))
    try:
        items, gated = plan_items(manifest, models_dir, device)
        size: int | None = remaining_bytes(items)
        needs = _needs_token(items, gated)
    except (ModelStoreError, OSError, ValueError):
        size = None
        known = _gate_known(manifest)
        if known is None:
            return None
        needs = known
    return size, needs and not token


def _is_gated_error(error: Exception) -> bool:
    return type(error).__name__ == "GatedRepoError"


# ---- fetching ------------------------------------------------------------------

def _progress_class(on_bytes: Callable[[int], None]) -> type:
    """A tqdm subclass that hands huggingface_hub's byte counts to `on_bytes`.

    The hub's own bar is the only progress hook a download offers, so the bar
    is the place to count bytes and to stop: raising from update() abandons the
    transfer at the next chunk.
    """
    from huggingface_hub.utils.tqdm import tqdm

    class ReportingBar(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            on_bytes(self.n)

        def update(self, n: float | None = 1) -> bool | None:
            result = super().update(n)
            on_bytes(self.n)
            return result

    return ReportingBar


def _fetch_hub(item: Item, on_bytes: Callable[[int], None]) -> None:
    from huggingface_hub import hf_hub_download

    if item.folder is None or not contained(item.path, hub_cache_root()) \
            or not contained(item.folder / "blobs", hub_cache_root()):
        raise ModelStoreError("download_failed")
    hf_hub_download(
        repo_id=item.repo, filename=item.name, revision=item.revision,
        cache_dir=str(hub_cache_root()), tqdm_class=_progress_class(on_bytes),
    )


def _fetch_url(item: Item, on_bytes: Callable[[int], None]) -> None:
    """Write one upscaler file. It is published under its name only when the bytes
    received equal the size the plan declared. The temporary file is this attempt's own."""
    if item.size <= 0 or item.root is None or not contained(item.path, item.root):
        raise ModelStoreError("download_failed")
    item.path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    tmp: Path | None = None
    try:
        with urllib.request.urlopen(item.url, timeout=60) as response:  # noqa: S310
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f"{item.path.name}.", suffix=".tmp",
                dir=item.path.parent, delete=False,
            ) as out:
                tmp = Path(out.name)
                while chunk := response.read(UPSCALE_CHUNK_BYTES):
                    written += len(chunk)
                    if written > item.size:
                        raise ModelStoreError("download_failed")
                    out.write(chunk)
                    on_bytes(written)
        if written != item.size:
            raise ModelStoreError("download_failed")
        tmp.replace(item.path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _link_cached(item: Item) -> None:
    """Point a snapshot path at a blob the hub cache already completed. No bytes move."""
    if not contained(item.path, hub_cache_root()):
        raise ModelStoreError("download_failed")
    if item.folder is None or not contained(item.folder / "blobs" / item.blob_key,
                                            hub_cache_root()):
        raise ModelStoreError("download_failed")
    item.path.parent.mkdir(parents=True, exist_ok=True)
    item.path.unlink(missing_ok=True)
    blob = item.folder / "blobs" / item.blob_key
    os.symlink(os.path.relpath(blob, item.path.parent), item.path)


def _blob_referenced(folder: Path, blob: Path) -> bool:
    for dirpath, _dirs, names in os.walk(folder / "snapshots"):
        for name in names:
            link = Path(dirpath) / name
            if link.is_symlink() and os.path.realpath(link) == str(blob):
                return True
    return False


def _linked_blobs(root: Path) -> set[Path]:
    """The blob every symlink under a snapshot points at, at any depth."""
    blobs: set[Path] = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            link = Path(dirpath) / name
            if link.is_symlink():
                blobs.add(Path(os.path.realpath(link)))
    return blobs


def _delete_unreferenced_blob(blob: Path, folder: Path, *, partials: bool) -> bool:
    """Delete under the Hub lock; return True if the lock was unavailable."""
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        return True
    lock_path = hub_cache_root() / ".locks" / folder.name / (blob.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(lock_path), timeout=0, mode=0o664)
        lock.acquire()
    except (Timeout, NotImplementedError, OSError):
        return True
    try:
        if _blob_referenced(folder, blob):
            return False
        blob.unlink(missing_ok=True)
        if partials:
            partial = Path(str(blob) + ".incomplete")
            if contained(partial, hub_cache_root()) and not partial.is_symlink():
                partial.unlink(missing_ok=True)
            for candidate in blob.parent.glob(blob.name + ".????????.incomplete"):
                if contained(candidate, hub_cache_root()) and not candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
        return False
    finally:
        lock.release()


def _delete_model_files(manifest: Manifest, models_dir: str,
                        others: list[Manifest]) -> None:
    """Delete this model's files, keeping any repository revision another installed model
    reads. Every target is resolved and must stay inside its cache before it is touched.
    The marker goes last: a deletion that fails midway leaves the model installed."""
    kept = {(ref.repo, ref.revision) for other in others for ref in repo_refs(other)}
    if "upscale" in manifest.capabilities:
        cache = upscale_root(models_dir)
        for _url, path in upscale_targets(manifest, models_dir):
            if not contained(path, cache):
                raise ModelStoreError("remove_failed")
            path.unlink(missing_ok=True)
        _write_marker(manifest.id, {"state": "removed"})
        return
    marker = _read_marker(manifest.id) or {}
    retained: set[str] = set()
    protected = set()
    legacy_folders: set[Path] = set()
    for other in others:
        other_marker = _read_marker(other.id) or {}
        protected.update(other_marker.get("blobs", []))
        if "blobs" not in other_marker:
            legacy_folders.update(repo_folder(ref.repo) for ref in repo_refs(other))
        for entry in other_marker.get("files", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                protected.add(os.path.realpath(entry["path"]))
    for ref in repo_refs(manifest):
        if (ref.repo, ref.revision) in kept:
            continue
        root = snapshot_dir(ref.repo, ref.revision)
        if not root.is_dir():
            continue
        if not contained(root, hub_cache_root()):
            raise ModelStoreError("remove_failed")
        blobs = _linked_blobs(root)
        shutil.rmtree(root)
        folder = repo_folder(ref.repo)
        for blob in blobs:
            if (str(blob) not in protected and folder not in legacy_folders
                    and contained(blob, hub_cache_root()) and blob.is_file()
                    and blob.parent == folder / "blobs"):
                if _delete_unreferenced_blob(blob, folder, partials=False):
                    retained.add(str(blob))
    for blob_name in marker.get("blobs", []):
        if not isinstance(blob_name, str):
            continue
        blob = Path(blob_name)
        if blob.parent.name == "blobs" and contained(blob, hub_cache_root()):
            if blob_name in protected or blob.parent.parent in legacy_folders:
                if marker.get("state") == "removed":
                    retained.add(blob_name)
                continue
            if _delete_unreferenced_blob(blob, blob.parent.parent, partials=True):
                retained.add(str(blob))
            else:
                retained.discard(str(blob))
    _write_marker(manifest.id, {"state": "removed", "blobs": sorted(retained)})


def _check_volumes(fetch_items: list[Item]) -> None:
    """Every volume a download writes to must keep the margin after its own bytes."""
    need: dict[int, int] = {}
    probes: dict[int, Path] = {}
    seen: set[Path] = set()
    for item in fetch_items:
        key = _transfer_key(item)
        if key in seen:
            continue
        seen.add(key)
        destination = item.folder / "blobs" if item.repo and item.folder else item.path
        if item.repo and (not contained(item.path, hub_cache_root())
                          or not contained(destination, hub_cache_root())):
            raise ModelStoreError("download_failed")
        device, probe = volume_of(destination.resolve())
        need[device] = need.get(device, 0) + _transfer_size(item)
        probes.setdefault(device, probe)
    for device, fetch in need.items():
        _total, free = free_bytes(probes[device])
        if free - fetch < DISK_MARGIN_BYTES:
            raise ModelStoreError("disk")


def install_blocking(manifest: Manifest, models_dir: str, *,
                     device: str = "cuda",
                     cancel: threading.Event | None = None,
                     progress: Callable[[int, int], None] | None = None) -> None:
    """Plan, check each volume, fetch what is missing, and verify. Blocks, so it runs in a
    thread or a script. `progress(done, total)` is called as bytes arrive; setting `cancel`
    stops the attempt at the next chunk. A cancelled or failed attempt keeps the files it
    completed: they are content-addressed and reusable, and `remove` frees the space.
    The marker says installed only after every planned file is verified. Raises ModelStoreError."""
    stop = cancel or threading.Event()
    report = progress or (lambda _done, _total: None)
    try:
        if (_read_marker(manifest.id) or {}).get("state") == "removed":
            _write_marker(manifest.id, {"state": "installing"})
        items, gated = plan_items(manifest, models_dir, device)
        if _needs_token(items, gated) and not os.environ.get("HF_TOKEN"):
            raise ModelStoreError("needs_token")
        missing = [item for item in items if not _size_ok(item.path, item.size)]
        linkable = [item for item in missing if _blob_ready(item)]
        fetch_items = [item for item in missing if not _blob_ready(item)]
        fetch = remaining_bytes(fetch_items)
        report(0, fetch)
        _check_volumes(fetch_items)
        _write_marker(manifest.id, {
            "state": "installing",
            "fingerprint": _fingerprint(manifest, device),
            "variant": weight_config(manifest.dtype, device)[1],
            "blobs": [str(_transfer_key(item)) for item in items
                      if item.folder is not None and item.blob_key],
        })
        for item in linkable:
            _link_cached(item)
        done_before = 0
        for item in fetch_items:
            if _blob_ready(item):
                _link_cached(item)
                continue
            if stop.is_set():
                raise _Cancelled()
            base = done_before
            residual = _transfer_size(item)
            resumed = item.size - residual

            def on_bytes(count: int, base: int = base) -> None:
                if stop.is_set():
                    raise _Cancelled()
                report(base + max(0, count - resumed), fetch)

            fetch_item = _fetch_url if item.url else _fetch_hub
            fetch_item(item, on_bytes)
            done_before += residual
            report(done_before, fetch)
        if not all(_size_ok(item.path, item.size) for item in items):
            raise ModelStoreError("incomplete")
        _write_marker(manifest.id, {
            "state": "installed",
            "fingerprint": _fingerprint(manifest, device),
            "variant": weight_config(manifest.dtype, device)[1],
            "files": [{"path": str(item.path), "size": item.size} for item in items],
            "blobs": [str(_transfer_key(item)) for item in items
                      if item.folder is not None and item.blob_key],
        })
    except _Cancelled:
        # ponytail: a cancelled install leaves its completed files until a later install or remove.
        raise ModelStoreError("cancelled") from None
    except ModelStoreError:
        raise
    except Exception as error:
        if _is_gated_error(error):
            raise ModelStoreError("needs_token") from error
        logger.warning("model %s download failed (%s)", manifest.id, type(error).__name__)
        raise ModelStoreError("download_failed") from error


def ensure_installed(manifest: Manifest, models_dir: str, *, device: str = "cuda", out: Any = None) -> None:
    """For a script that loads a model itself: install it now if it is missing, printing
    progress, and return once it is complete. A refusal raises RuntimeError with the
    reason, since a script has no status code to return it on."""
    if is_installed(manifest, models_dir, device) or adopt_legacy(manifest, models_dir, device):
        return
    stream = out or sys.stderr
    last_shown = [0.0]

    def show(done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - last_shown[0] < 2.0:
            return
        last_shown[0] = now
        stream.write(f"\r{manifest.id}: {done / GIB:.2f} of {total / GIB:.2f} GiB")
        stream.flush()

    try:
        install_blocking(manifest, models_dir, device=device, progress=show)
    except ModelStoreError as error:
        raise RuntimeError(f"{manifest.id} is not installed ({error.reason})") from error
    stream.write(f"\n{manifest.id}: installed\n")


class ModelStore:
    """Install state and operations for the worker process.

    `installed` is the live set the connection serves. The store updates it in
    place, so a session manager or a job lookup that holds the same dict sees
    an install or a removal at once. The store outlives connections: operations
    keep running while the connection is down, and the next attach reports them.
    """

    def __init__(self, manifests: list[Manifest], engine: Any, models_dir: str) -> None:
        self.bundled = {manifest.id: manifest for manifest in manifests}
        self.installed: dict[str, Manifest] = {}
        self._engine = engine
        self._models_dir = models_dir
        self._device = getattr(engine, "device", "cuda")
        self._ops: dict[str, Operation] = {}
        # One FIFO queue for the process, drained by one task. Operations start in the
        # order they were asked for; a cancel skips or signals one, never reorders.
        self._queue: asyncio.Queue[Operation | None] = asyncio.Queue()
        self._queued: list[Operation] = []
        self._current: Operation | None = None
        self._drainer: asyncio.Task | None = None
        self._closing = False
        self._send: Callable[[dict], Any] | None = None
        self._busy: Callable[[str], bool] = lambda _model_id: False
        self._dirty = False
        self._tasks: set[asyncio.Task] = set()
        self._status_task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None
        self._probe_generation = 0
        self._adoption_task: asyncio.Task | None = None
        self._connection_generation = 0
        self._incomplete_adoptions: set[str] = set()
        self._gpu: tuple[str, int | None] = ("unknown", None)
        self._adoption_lock = asyncio.Lock()
        self._adoption_wakeup = asyncio.Event()

    async def scan(self) -> None:
        """Set the installed set from the markers. A cache with no marker is adopted here
        when the exact hub plan is complete on disk."""
        found = await asyncio.to_thread(self._scan_disk)
        self.installed.clear()
        self.installed.update(found)

    def _scan_disk(self) -> dict[str, Manifest]:
        found: dict[str, Manifest] = {}
        for manifest in self.bundled.values():
            try:
                valid = is_installed(manifest, self._models_dir, self._device)
                marker = _read_marker(manifest.id)
                if not valid and marker is not None and marker.get("state") == "installed":
                    _remove_marker(manifest.id)
                if valid or adopt_legacy(manifest, self._models_dir, self._device):
                    found[manifest.id] = manifest
            except OSError:
                logger.warning("cannot scan model %s: cache is inaccessible", manifest.id)
        return found

    async def verify_for_load(self, model_id: str) -> bool:
        manifest = self.installed.get(model_id)
        if manifest is None:
            return False
        try:
            valid = await asyncio.to_thread(is_installed, manifest, self._models_dir, self._device)
        except OSError:
            valid = False
        if valid:
            return True
        self.installed.pop(model_id, None)
        self._incomplete_adoptions.discard(model_id)
        try:
            _remove_marker(model_id)
        except OSError:
            logger.warning("cannot remove invalid marker for model %s", model_id)
        self._invalidate_probes(model_id)
        self._schedule_refresh()
        await self._announce()
        await self.send_inventory()
        return False

    def catalog(self) -> list[dict]:
        rows = []
        for manifest in sorted(self.bundled.values(), key=lambda m: m.id):
            probe = _PROBES.get(manifest.id)
            row = manifest.wire()
            row["installed"] = manifest.id in self.installed
            row["download_bytes"] = probe[0] if probe else None
            row["needs_token"] = bool(probe and probe[1])
            rows.append(row)
        return rows

    def remember_hardware(self, gpu_name: str, vram_total_bytes: int | None) -> None:
        self._gpu = (gpu_name, vram_total_bytes)

    def hardware(self) -> dict[str, Any]:
        disk_total, disk_free = free_bytes(hub_cache_root())
        gpu_name, vram_total_bytes = self._gpu
        return {
            "gpu_name": gpu_name,
            "vram_total_bytes": vram_total_bytes,
            "disk_total_bytes": disk_total,
            "disk_free_bytes": disk_free,
        }

    def attach(self, send: Callable[[dict], Any], busy: Callable[[str], bool]) -> None:
        """Start reporting on this connection. Operations already running keep going.
        Called only once the API has signalled that it speaks model_store."""
        self._connection_generation += 1
        self._incomplete_adoptions.clear()
        self._adoption_wakeup.clear()
        self._send = send
        self._busy = busy
        self._dirty = True
        self._status_task = asyncio.create_task(self._status_loop())
        self._schedule_adoption()
        self._schedule_refresh()

    async def send_inventory(self) -> None:
        """store_inventory: the hardware and the bundled catalog, once per connection."""
        await self._emit({"type": "store_inventory", "hardware": self.hardware(),
                          "catalog": self.catalog(),
                          "op_id": max((op.op_id for op in self._ops.values()
                                        if op.op_id is not None), default=None)})

    async def close(self) -> None:
        """Detach from the connection that is ending. Operations keep running, and the
        next attach reports them, so a dropped connection throws away no bytes."""
        self._send = None
        self._adoption_wakeup.set()
        tasks = [task for task in (self._status_task, self._probe_task)
                 if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._status_task = None
        self._probe_task = None

    async def shutdown(self) -> None:
        """Process exit: skip what is queued, stop the running download, and wait for the
        drainer to finish. Nothing new starts after this."""
        self._closing = True
        await self.close()
        for op in list(self._queued):
            op.cancel.set()
        if self._current is not None:
            self._current.cancel.set()
        self._queue.put_nowait(None)
        if self._drainer is not None:
            await asyncio.gather(self._drainer, return_exceptions=True)
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def handle(self, control: dict) -> None:
        """Start a store command. Refusals are reported as store_status, never raised.
        A command that arrives while no connection has signalled model_store is ignored."""
        if self._send is None:
            logger.info("model store command ignored: the API has not signalled model_store")
            return
        kind = control["type"]
        model_id = control["model_id"]
        op_id = control.get("op_id")
        if kind == "install_model":
            self._install(model_id, op_id)
        elif kind == "cancel_install":
            self._cancel(model_id, op_id)
        elif kind == "remove_model":
            self._remove(model_id, op_id)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _mark(self) -> None:
        self._dirty = True

    def _enqueue(self, op: Operation) -> None:
        self._queued.append(op)
        self._queue.put_nowait(op)
        if self._drainer is None or self._drainer.done():
            self._drainer = self._spawn(self._drain())
        self._mark()

    async def _drain(self) -> None:
        while True:
            op = await self._queue.get()
            if op is None:
                self._queue.task_done()
                return
            self._queued = [queued for queued in self._queued if queued is not op]
            try:
                if op.kind == "refusal":
                    op.state, op.error = "failed", "in_use"
                    continue
                if op.kind == "cancel":
                    if not op.targets:
                        op.state, op.error = "failed", "not_running"
                    else:
                        target = next((target for target in op.targets
                                       if target.error == "cancelled"), op.targets[-1])
                        op.state, op.error = target.state, target.error
                        op.bytes_done, op.bytes_total = target.bytes_done, target.bytes_total
                    continue
                if self._closing or op.cancel.is_set():
                    if op.kind == "install":
                        op.state = "failed"
                        op.error = "cancelled"
                    continue
                self._current = op
                async with self._adoption_lock:
                    if op.kind == "install":
                        await self._run_install(op)
                    else:
                        await self._run_remove(op)
            finally:
                self._current = None
                if op.kind in ("install", "remove"):
                    self._invalidate_probes(op.model_id)
                    self._schedule_refresh()
                self._queue.task_done()
                self._mark()

    def _install(self, model_id: str, op_id: str | None) -> None:
        manifest = self.bundled.get(model_id)
        if manifest is None:
            logger.warning("install_model for an unbundled model ignored")
            return
        op = Operation(model_id, op_id=op_id, kind="install")
        self._ops[model_id] = op
        self._enqueue(op)

    async def _run_install(self, op: Operation) -> None:
        manifest = self.bundled[op.model_id]
        if manifest.id in self.installed:
            # Asked for twice, or installed by an earlier operation: nothing to do.
            op.state = "done"
            op.bytes_done = op.bytes_total
            return
        op.state = "downloading"
        self._mark()
        try:
            await asyncio.to_thread(self._download, op, manifest)
        except ModelStoreError as error:
            op.state = "failed"
            op.error = error.reason
            return
        op.state = "done"
        if not any(queued.model_id == manifest.id and queued.kind == "remove"
                   for queued in self._queued):
            self.installed[manifest.id] = manifest
            await self._announce()

    def _download(self, op: Operation, manifest: Manifest) -> None:
        """Runs in a thread. The store's view of install_blocking: progress lands on the op."""
        def report(done: int, total: int) -> None:
            op.bytes_done = done
            op.bytes_total = total
            self._mark()

        install_blocking(manifest, self._models_dir, device=self._device,
                         cancel=op.cancel, progress=report)

    def _cancel(self, model_id: str, op_id: str | None) -> None:
        """Mark the queued installs of this model skipped, and signal a running one. A cancel
        never reorders or releases anything: the queue keeps its order."""
        targets = []
        for op in self._queued:
            if op.model_id == model_id and op.kind == "install":
                targets.append(op)
                op.cancel.set()
                op.state = "failed"
                op.error = "cancelled"
        if self._current is not None and self._current.model_id == model_id \
                and self._current.kind == "install":
            targets.append(self._current)
            self._current.cancel.set()
        if model_id in self.bundled:
            result = Operation(model_id, op_id=op_id, kind="cancel", targets=targets)
            self._ops[model_id] = result
            self._enqueue(result)
        self._mark()

    def _remove(self, model_id: str, op_id: str | None) -> None:
        """Enqueue a removal. A served model stops being served now, and that is announced
        now, so routing stops before any disk work."""
        manifest = self.bundled.get(model_id)
        if manifest is None:
            return
        if model_id in self.installed:
            if self._busy(model_id):
                op = Operation(model_id, op_id=op_id, kind="refusal")
                self._ops[model_id] = op
                self._enqueue(op)
                return
            self.installed.pop(model_id)
            self._ops.pop(model_id, None)
            self._spawn(self._announce())
        op = Operation(model_id, op_id=op_id, kind="remove")
        self._ops[model_id] = op
        self._enqueue(op)

    async def _run_remove(self, op: Operation) -> None:
        manifest = self.bundled[op.model_id]
        if op.model_id in self.installed:
            # Installed by an operation that ran before this one, after the removal was asked for.
            if self._busy(op.model_id):
                op.state = "failed"
                op.error = "in_use"
                return
            self.installed.pop(op.model_id)
            await self._announce()
        try:
            if op.model_id in self._engine.loaded_models():
                await self._engine.unload_model(op.model_id)
            others = list(self.installed.values())
            await asyncio.to_thread(_delete_model_files, manifest, self._models_dir, others)
            op.state = "done"
        except Exception:
            logger.exception("removing %s failed", op.model_id)
            # The marker goes last, so a failed removal often leaves the model installed.
            # Serve it again if its files are still complete, so a retry is possible.
            if (is_installed(manifest, self._models_dir, self._device)
                    and not any(queued.model_id == op.model_id and queued.kind == "remove"
                                for queued in self._queued)):
                self.installed[op.model_id] = manifest
                await self._announce()
            op.state = "failed"
            op.error = "remove_failed"

    def _invalidate_probes(self, model_id: str) -> None:
        self._probe_generation += 1
        changed = self.bundled[model_id]
        repos = {ref.repo for ref in repo_refs(changed, self._device)}
        for manifest in self.bundled.values():
            if manifest.id == model_id or repos.intersection(
                    ref.repo for ref in repo_refs(manifest, self._device)):
                _PROBES.pop(manifest.id, None)
                self._incomplete_adoptions.discard(manifest.id)

    def _removal_pending(self, model_id: str) -> bool:
        return any(op is not None and op.model_id == model_id and op.kind == "remove"
                   for op in [self._current, *self._queued])

    def _schedule_adoption(self) -> None:
        if self._send is not None and (self._adoption_task is None or self._adoption_task.done()):
            self._adoption_task = self._spawn(self._retry_adoption())

    async def _retry_adoption(self) -> None:
        delay = ADOPTION_RETRY_SECONDS
        while self._send is not None:
            pending = False
            for manifest in self.bundled.values():
                if (manifest.id in self._incomplete_adoptions or manifest.id in self.installed
                        or _read_marker(manifest.id) is not None):
                    continue
                pending = True
                try:
                    async with self._adoption_lock:
                        if (manifest.id in self.installed or _read_marker(manifest.id) is not None
                                or self._removal_pending(manifest.id)):
                            continue
                        generation = self._connection_generation
                        adopted = await asyncio.to_thread(
                            adopt_legacy, manifest, self._models_dir, self._device)
                        if adopted is False and generation == self._connection_generation:
                            self._incomplete_adoptions.add(manifest.id)
                        if adopted and not self._removal_pending(manifest.id):
                            self.installed[manifest.id] = manifest
                            await self._announce()
                except OSError:
                    logger.warning("cannot scan model %s: cache is inaccessible", manifest.id)
            if not pending:
                return
            try:
                await asyncio.wait_for(self._adoption_wakeup.wait(), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    def _schedule_refresh(self) -> None:
        if self._send is not None and (self._probe_task is None or self._probe_task.done()):
            self._probe_task = asyncio.create_task(self._refresh_probes())

    async def _refresh_probes(self) -> None:
        while self._send is not None:
            generation = self._probe_generation
            await self._probe_unknown()
            if generation == self._probe_generation:
                await self._announce()
                if generation == self._probe_generation:
                    return

    async def _probe_unknown(self) -> None:
        """Fill in download sizes the process has not learned yet."""
        learned = False
        generation = self._probe_generation
        for manifest in list(self.bundled.values()):
            if manifest.id in _PROBES:
                continue
            result = await asyncio.to_thread(probe_model, manifest, self._models_dir, self._device)
            if generation != self._probe_generation:
                return
            self._schedule_adoption()
            if result is not None:
                _PROBES[manifest.id] = result
                learned = True
        if learned:
            await self._announce()

    async def _announce(self) -> None:
        """models_changed: the live model set and catalog, after an install or removal."""
        if self._send is None:
            return
        models = self._engine.measured_manifests(list(self.installed.values()))
        await self._emit({"type": "models_changed", "models": models,
                          "catalog": self.catalog()})

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL_SECONDS)
            if not self._dirty:
                continue
            self._dirty = False
            _total, free = free_bytes(hub_cache_root())
            await self._emit({
                "type": "store_status",
                "downloads": [
                    {"model_id": op.model_id, "state": op.state,
                     "kind": "remove" if op.kind == "refusal" else op.kind,
                     "bytes_done": op.bytes_done, "bytes_total": op.bytes_total,
                     "error": op.error, "op_id": op.op_id}
                    for op in self._ops.values()
                ],
                "disk_free_bytes": free,
            })

    async def _emit(self, message: dict) -> None:
        send = self._send
        if send is None:
            return
        try:
            await send(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The socket is gone; the next connection's registration carries the truth.
            logger.info("store message not sent (%s)", type(error).__name__)
