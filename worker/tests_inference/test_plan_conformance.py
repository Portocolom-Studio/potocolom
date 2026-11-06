"""The download plan is the loader's own file selection (decision: "The download plan is the
loader's own file selection").

The store plans the files a load reads from a repository listing. These tests check that plan
against what diffusers itself selects. `DiffusionPipeline.download` is driven for real, with
every hub call stubbed to serve the pinned listing from hub_lock.json (no network, no cache).
The patterns it hands to `snapshot_download` are captured, and applied the way diffusers
applies them (pipeline_utils.py lines 1767-1773). The result must equal our plan.

Covered: every ordinary pipeline the engine loads (text-to-image and image-to-image classes,
both variant branches of `DiffusersEngine._from_pretrained`), the two MOSS candidates (driven
with the loader's custom-pipeline arguments), and the two plain repositories of Iris-3B.

Not covered by diffusers: Flux3Distilled. Its modular pipeline has no `download()` equivalent:
`load_components` calls each component's own `from_pretrained`, so there is no pattern set to
capture. Its plan is covered by the unit tests in worker/tests/test_model_store.py.

The committed worker/tests/fixtures/expected_files.json pins the diffusers result for each
pipeline at the ROCm variant, so a change to either side shows in review. This module writes
nothing.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from worker import model_store
from worker.candidate_pipelines import CUSTOM_PIPELINES, IRIS_ALLOW, IRIS_ENCODER_ALLOW, plain_repo_patterns
from worker.gpu_device import weight_config
from worker.manifests import load_manifests

MODELS = Path(__file__).parents[1] / "models"
FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "expected_files.json"
DEVICE = "rocm"
# Manifests the diffusers conformance covers: a diffusers pipeline, or a custom one.
# Flux3Distilled is excluded (see the module docstring), Iris-3B and its encoder are plain repos.
EXCLUDED_PIPELINES = {"Flux3Distilled", "Iris3B"}


def diffusion_manifests():
    return [m for m in load_manifests(str(MODELS))
            if "upscale" not in m.capabilities and m.pipeline not in EXCLUDED_PIPELINES]


def _lock(repo: str, revision: str) -> dict:
    return model_store.bundled_hub_metadata()["repos"][f"{repo}@{revision}"]


class _Captured(Exception):
    """Stops `snapshot_download` after it has received the patterns."""

    def __init__(self, allow: list[str], ignore: list[str]) -> None:
        super().__init__("patterns captured")
        self.allow = list(allow)
        self.ignore = list(ignore)


def _custom_class(*_args, **_kwargs):
    """The MOSS module's class. The module's code is not in the lock, so only its signature is
    stood in: diffusers reads the component names from it, and MOSS passes none of them."""
    from diffusers import DiffusionPipeline

    class MOSSImagePipeline(DiffusionPipeline):
        def __init__(self, scheduler, text_encoder, tokenizer, transformer, vae):
            pass

    return MOSSImagePipeline


def diffusers_download(repo: str, revision: str, *, cls: type, variant: str, index_text: str,
                       files: list[tuple[str, int]], passed: bool,
                       custom_pipeline: str) -> list[str]:
    """The names `DiffusionPipeline.download` would fetch, with the hub stubbed to `files`.

    Raises ValueError when diffusers refuses the variant (no variant file in the repository).
    Writes nothing: the model_index.json reader returns the lock's contents, and the hub
    calls are stubbed.
    """
    from diffusers.pipelines import pipeline_loading_utils as loading
    from diffusers.pipelines import pipeline_utils as pipeline_module

    def hub_download(_repo_id: str, filename: str, **_kwargs: object) -> str:
        # A name only: the reader below serves the index, so no file is read from it.
        assert filename == "model_index.json", filename
        return filename

    def read_index(_cls: type, _path: object) -> dict:
        return json.loads(index_text)

    def model_info(_repo_id: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(siblings=[SimpleNamespace(rfilename=n) for n, _size in files])

    def snapshot_download(_repo_id: str, *, allow_patterns: list[str],
                          ignore_patterns: list[str], **_kwargs: object) -> str:
        raise _Captured(allow_patterns, ignore_patterns)

    def resolve(_name: str, *, revision: str | None = None, **_kwargs: object) -> str | None:
        return revision

    kwargs: dict[str, object] = {"vae": None} if passed else {}
    with mock.patch.multiple(pipeline_module, model_info=model_info,
                             hf_hub_download=hub_download,
                             snapshot_download=snapshot_download,
                             _resolve_revision=resolve), \
            mock.patch.object(loading, "get_class_from_dynamic_module", _custom_class), \
            mock.patch.object(cls, "_dict_from_json_file", classmethod(read_index)):
        try:
            cls.download(repo, revision=revision, variant=variant or None,
                         local_files_only=False, custom_pipeline=custom_pipeline or None,
                         trust_remote_code=bool(custom_pipeline), **kwargs)
        except _Captured as captured:
            allow, ignore = captured.allow, captured.ignore
        else:
            raise AssertionError("download returned before snapshot_download")
    # The same selection as pipeline_utils.py lines 1767-1773.
    names = [name for name, _size in files]
    re_ignore = [re.compile(fnmatch.translate(p)) for p in ignore]
    re_allow = [re.compile(fnmatch.translate(p)) for p in allow]
    selected = [n for n in names if not any(p.match(n) for p in re_ignore)]
    return sorted(n for n in selected if any(p.match(n) for p in re_allow))


def _listing(repo: str, revision: str) -> tuple[list[tuple[str, int]], str | None]:
    entry = _lock(repo, revision)
    return [(name, size) for name, size in entry["files"]], entry.get("model_index")


def diffusers_selection(manifest, mode: str, variant: str) -> list[str]:
    """What diffusers fetches for one class of the manifest, with the engine's fallback: a
    variant the repository lacks is retried without one (DiffusersEngine._from_pretrained)."""
    from diffusers import DiffusionPipeline

    files, index_text = _listing(manifest.source, manifest.source_revision)
    custom = CUSTOM_PIPELINES.get(manifest.pipeline, "")
    if custom:
        cls: type = DiffusionPipeline
    else:
        class_name = json.loads(index_text)["_class_name"]
        cls = _diffusers_class(class_name, mode)
    passed = bool(manifest.vae)
    try:
        return diffusers_download(manifest.source, manifest.source_revision, cls=cls,
                                  variant=variant, index_text=index_text, files=files,
                                  passed=passed, custom_pipeline=custom)
    except ValueError:
        return diffusers_download(manifest.source, manifest.source_revision, cls=cls,
                                  variant="", index_text=index_text, files=files,
                                  passed=passed, custom_pipeline=custom)


def _diffusers_class(class_name: str, mode: str) -> type:
    """The class the engine loads for a manifest with no pipeline name: AutoPipeline resolves
    the text-to-image class from `_class_name`, and the image-to-image class is its twin."""
    import diffusers

    name = class_name if mode == "t2i" else class_name.replace("Pipeline", "Img2ImgPipeline")
    cls = getattr(diffusers, name, None)
    assert cls is not None, f"diffusers has no class {name}"
    return cls


def our_selection(manifest, variant: str) -> list[str]:
    files, index_text = _listing(manifest.source, manifest.source_revision)
    index = json.loads(index_text)
    passed = frozenset({"vae"}) if manifest.vae else frozenset()
    custom = CUSTOM_PIPELINES.get(manifest.pipeline, "")
    chosen = model_store.select_pipeline_plan(files, index, variant, passed=passed,
                                              custom_pipeline=custom)
    return [name for name, _size in chosen]


def _modes(manifest) -> list[str]:
    return ["t2i", "i2i"] if "image_to_image" in manifest.capabilities else ["t2i"]


CASES = [
    pytest.param(manifest, mode, variant,
                 id=f"{manifest.id}-{mode}-{variant or 'default'}")
    for manifest in diffusion_manifests()
    for mode in _modes(manifest)
    for variant in ("fp16", "")
]


@pytest.mark.parametrize("manifest, mode, variant", CASES)
def test_plan_equals_what_diffusers_downloads(manifest, mode, variant):
    expected = diffusers_selection(manifest, mode, variant)
    assert our_selection(manifest, variant) == expected


@pytest.mark.parametrize("manifest", diffusion_manifests(), ids=lambda m: m.id)
def test_committed_fixture_matches_diffusers(manifest):
    fixture = json.loads(FIXTURE.read_text())
    key = f"{manifest.source}@{manifest.source_revision}"
    variant = weight_config(manifest.dtype, DEVICE)[1]
    assert fixture["manifests"][manifest.id][key] == diffusers_selection(manifest, "t2i", variant)


LEGACY_VARIANT_FILES = [
    ("model_index.json", 10), ("unet/config.json", 3),
    ("unet/diffusion_pytorch_model-00001-of-00002.fp16.bin", 90),
    ("unet/diffusion_pytorch_model-00002-of-00002.fp16.bin", 90),
    ("unet/diffusion_pytorch_model.bin.fp16.index.json", 1),
    ("unet/diffusion_pytorch_model-00001-of-00002.bin", 90),
    ("unet/diffusion_pytorch_model-00002-of-00002.bin", 90),
    ("unet/diffusion_pytorch_model.bin.index.json", 1),
]


def test_legacy_variant_shards_are_planned_as_diffusers_selects_them():
    """A repository whose fp16 weights use the older shard layout (no new-style variant file):
    diffusers selects the legacy shards and their index for the variant, and the plan must
    select the same files."""
    index_text = json.dumps({"_class_name": "StableDiffusionXLPipeline",
                             "unet": ["diffusers", "UNet2DConditionModel"]})
    cls = _diffusers_class("StableDiffusionXLPipeline", "t2i")
    expected = diffusers_download("org/legacy-variant", "a" * 40, cls=cls, variant="fp16",
                                  index_text=index_text, files=LEGACY_VARIANT_FILES,
                                  passed=False, custom_pipeline="")
    assert "unet/diffusion_pytorch_model-00002-of-00002.fp16.bin" in expected
    chosen = model_store.select_pipeline_plan(LEGACY_VARIANT_FILES, json.loads(index_text), "fp16")
    assert [name for name, _size in chosen] == expected


def test_plain_repositories_follow_the_loader_constant():
    """Iris-3B and its Qwen encoder: the plan is the constant over the listing, and the loader
    fetches the same patterns, the encoder pinned by the manifest."""
    manifest = next(m for m in load_manifests(str(MODELS)) if m.pipeline == "Iris3B")
    for repo in (manifest.source, manifest.worker_repos[0].repo):
        pin = (manifest.source_revision if repo == manifest.source
               else manifest.worker_repos[0].revision)
        files, _index = _listing(repo, pin)
        patterns = plain_repo_patterns(manifest, repo)
        chosen = model_store.select_plain_plan(files, patterns)
        assert [n for n, _s in chosen] == sorted(
            n for n, _s in files if any(fnmatch.fnmatchcase(n, p) for p in patterns))


class _StopLoad(Exception):
    pass


def test_iris_loader_fetches_the_planned_patterns(monkeypatch):
    """Drive `_load_iris` until its second snapshot is requested, and compare both requests with
    the constant. The iris3b package is stood in: its code is vendored, not needed for this.
    The snapshot paths are synthetic: the stand-ins never read them, and nothing is written."""
    import huggingface_hub

    manifest = next(m for m in load_manifests(str(MODELS)) if m.pipeline == "Iris3B")
    # An encoder pin other than the bundled one: a loader that hard-codes the bundled revision
    # requests a different snapshot than the manifest names, and the check below fails.
    bundled_pin = manifest.worker_repos[0]
    encoder_pin = bundled_pin.model_copy(update={"revision": "0" * 40})
    assert encoder_pin.revision != bundled_pin.revision
    manifest = manifest.model_copy(
        update={"worker_repos": [encoder_pin, *manifest.worker_repos[1:]]})
    iris_dir, qwen_dir = "/synthetic/iris-3b", "/synthetic/qwen3-vl"
    requests: list[tuple[str, str, list[str]]] = []

    def snapshot(repo_id: str, *, revision: str | None = None,
                 allow_patterns: list[str], local_files_only: bool = False, **_kwargs: object) -> str:
        requests.append((repo_id, revision or "", list(allow_patterns)))
        return iris_dir if len(requests) == 1 else qwen_dir

    def stop(*_args: object, **_kwargs: object) -> None:
        raise _StopLoad

    def inference_config(_raw: object) -> SimpleNamespace:
        return SimpleNamespace(model=None, text_encoder=SimpleNamespace(pretrained=None))

    stand_in = {
        "iris3b": types.ModuleType("iris3b"),
        "iris3b.config": types.ModuleType("iris3b.config"),
        "iris3b.models": types.ModuleType("iris3b.models"),
        "iris3b.models.dit": types.ModuleType("iris3b.models.dit"),
        "iris3b.sampling": types.ModuleType("iris3b.sampling"),
        "iris3b.text": types.ModuleType("iris3b.text"),
        "iris3b.text.qwen3_vl": types.ModuleType("iris3b.text.qwen3_vl"),
    }
    stand_in["iris3b.config"].inference_config = inference_config  # type: ignore[attr-defined]
    stand_in["iris3b.models.dit"].IrisDiT = stop  # type: ignore[attr-defined]
    stand_in["iris3b.sampling"].generate = stop  # type: ignore[attr-defined]
    stand_in["iris3b.sampling"].load_for_inference = (  # type: ignore[attr-defined]
        lambda _dir: (object(), {}))
    stand_in["iris3b.text.qwen3_vl"].Qwen3VLTextEncoder = stop  # type: ignore[attr-defined]
    for name, module in stand_in.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)

    from worker import candidate_pipelines

    with pytest.raises(_StopLoad):
        candidate_pipelines._load_iris(manifest, "full", "cpu")

    (iris_request, qwen_request) = requests
    assert iris_request == (manifest.source, manifest.source_revision,
                            list(IRIS_ALLOW))
    encoder = manifest.worker_repos[0]
    assert qwen_request == (encoder.repo, encoder.revision,
                            list(IRIS_ENCODER_ALLOW))


def test_moss_candidates_ship_the_custom_pipeline_module_the_loader_names():
    """The loader and the plan read one module name, and each MOSS listing ships that file."""
    moss = [m for m in load_manifests(str(MODELS)) if m.pipeline == "MOSSImage"]
    assert moss
    for manifest in moss:
        files, _index = _listing(manifest.source, manifest.source_revision)
        assert f"{CUSTOM_PIPELINES['MOSSImage']}.py" in dict(files)
