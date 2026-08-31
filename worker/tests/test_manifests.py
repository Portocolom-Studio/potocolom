import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from worker.manifests import SIMULATED_MANIFEST, Manifest, load_manifests

PIN = "0" * 40  # shape-valid stand-in; the shipped pins live in the manifests

SD_TURBO = {
    "id": "sd-turbo",
    "name": "SD Turbo",
    "capabilities": ["text_to_image", "realtime"],
    "min_vram_gb": 8,
    "prompt_token_limit": 77,
    "source": "stabilityai/sd-turbo",
    "source_revision": "b261bac6fd2cf515557d5d0707481eafa0485ec2",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
}


def test_simulated_manifest_requires_prompt():
    """The no-GPU manifest must stay as strict as the shipped ones. A
    permissive simulated manifest is the easiest contract to build a client
    against, and a client built against it is refused 4000 by every shipped
    realtime model."""
    assert "prompt" in SIMULATED_MANIFEST.parameters["required"]


def test_load_manifests_and_wire_shape(tmp_path):
    (tmp_path / "sd-turbo.json").write_text(
        json.dumps({**SD_TURBO,
                    "preview_decoder": "madebyollin/taesd",
                    "preview_decoder_revision": PIN,
                    "quantize": "text_encoder_3:int8",
                    "t2i_adapter": "TencentARC/t2i-adapter-sketch-sdxl-1.0",
                    "t2i_adapter_revision": PIN})
    )
    manifests = load_manifests(str(tmp_path))
    assert [m.id for m in manifests] == ["sd-turbo"]
    assert manifests[0].preview_decoder == "madebyollin/taesd"
    assert manifests[0].quantize == "text_encoder_3:int8"
    assert manifests[0].t2i_adapter == "TencentARC/t2i-adapter-sketch-sdxl-1.0"
    wire = manifests[0].wire()
    assert wire["capabilities"] == ["text_to_image", "realtime"]
    assert wire["prompt_token_limit"] == 77  # the studio warning reads this
    assert "source" not in wire  # weight locations stay worker side
    assert "preview_decoder" not in wire
    assert "quantize" not in wire
    assert "t2i_adapter" not in wire
    for field in ("source_revision", "vae_revision", "preview_decoder_revision",
                  "lora_revision", "t2i_adapter_revision"):
        assert field not in wire  # a pin locates weights as much as its reference


def test_studio_capabilities_round_trips_through_wire():
    # The backend can only narrow what the worker tells it, so the studio
    # subset must survive the wire like every other advertised field.
    manifest = Manifest(**{**SD_TURBO, "studio_capabilities": ["realtime"]})
    assert manifest.studio_capabilities == ["realtime"]
    assert manifest.wire()["studio_capabilities"] == ["realtime"]


def test_malformed_quantize_is_loud(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps({**SD_TURBO, "quantize": "text_encoder_3"})
    )
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))


def test_unsupported_quantize_scheme_is_loud(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps({**SD_TURBO, "quantize": "text_encoder_3:int4"})
    )
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))


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
    sd_turbo = next(m for m in manifests if m.id == "sd-turbo")
    assert sd_turbo.benchmark_only
    sdxl_turbo = next(m for m in manifests if m.id == "sdxl-turbo")
    assert not sdxl_turbo.benchmark_only  # studio-visible realtime tier
    assert sdxl_turbo.default  # the realtime picker's declared default
    assert sdxl_turbo.t2i_adapter == "TencentARC/t2i-adapter-sketch-sdxl-1.0"
    assert sdxl_turbo.preview_decoder == "madebyollin/taesdxl"
    assert sdxl_turbo.parameters["properties"]["structure_strength"]["default"] == 1.0
    assert sdxl_turbo.parameters["properties"]["steps"]["maximum"] == 4
    dreamshaper = next(m for m in manifests if m.id == "dreamshaper-lcm")
    assert dreamshaper.benchmark_only
    vega = next(m for m in manifests if m.id == "vega-rt")
    assert not vega.benchmark_only
    assert vega.scheduler == "lcm"
    assert vega.preview_decoder == "madebyollin/taesdxl"
    assert vega.license_id == "apache-2.0"
    assert "realtime" in vega.capabilities
    assert "image_to_image" in vega.capabilities
    assert "strength" in vega.parameters["properties"]
    sd35 = next(m for m in manifests if m.id == "sd35-medium")
    # Gated Community License weights: attribution and the revenue cap must
    # reach the studio, and the T5 path is text_to_image only for now.
    assert sd35.capabilities == ["text_to_image"]
    assert sd35.license_id == "stability-ai-community"
    assert sd35.commercial_max_revenue_usd == 1000000
    assert sd35.requires_attribution == "Powered by Stability AI"
    assert sd35.scheduler == ""  # native flow-matching scheduler
    assert not sd35.lora and not sd35.vae
    assert sd35.quantize == "text_encoder_3:int8"
    assert not sd35.benchmark_only  # studio-visible quality tier (issue #151)
    # 20 steps measured at 56s vs 89s for 40 on the reference card, with no
    # quality difference worth 33 seconds.
    assert sd35.parameters["properties"]["steps"]["default"] == 20
    # int8 T5 peaks at 13.44 GB including generation activations.
    assert sd35.min_vram_gb == 14
    assert sd35.wire()["requires_attribution"] == "Powered by Stability AI"
    assert "quantize" not in sd35.wire()
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


def test_vega_rt_declares_sketch_adapter_and_structure_strength():
    models_dir = Path(__file__).resolve().parents[1] / "models"
    vega = next(m for m in load_manifests(str(models_dir)) if m.id == "vega-rt")
    assert vega.t2i_adapter == "TencentARC/t2i-adapter-sketch-sdxl-1.0"
    properties = vega.parameters["properties"]
    # The realtime frame switches to conditioned text-to-image when a
    # manifest names an adapter, and img2img keeps its own meaning for jobs:
    # structure_strength is new, strength stays.
    structure_strength = properties["structure_strength"]
    assert structure_strength["type"] == "number"
    assert structure_strength["minimum"] == 0
    assert structure_strength["maximum"] == 1.5
    assert structure_strength["default"] == 0.7
    steps = properties["steps"]
    assert steps["minimum"] == 2
    assert steps["maximum"] == 8
    assert steps["default"] == 4
    assert "strength" in properties
    # An adapter-only parameter must never appear on a manifest without one.
    for manifest in load_manifests(str(models_dir)):
        if manifest.id == "vega-rt":
            continue
        if not manifest.t2i_adapter:
            assert "structure_strength" not in manifest.parameters.get("properties", {})
        assert "t2i_adapter" not in manifest.wire()


def test_every_prompting_manifest_declares_its_token_window():
    """A missing window silences the studio warning (issue #148), so a new
    diffusion manifest that forgets it should fail here rather than ship a
    prompt whose tail is dropped with nothing said. Upscalers take no prompt.
    """
    models_dir = Path(__file__).resolve().parents[1] / "models"
    for manifest in load_manifests(str(models_dir)):
        if "upscale" in manifest.capabilities:
            assert manifest.prompt_token_limit == 0, manifest.id
        else:
            assert manifest.prompt_token_limit > 0, manifest.id


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


def test_min_vram_gb_is_bounded_where_the_operator_sees_it(tmp_path):
    # The API refuses a value past int4 at registration, which the operator
    # experiences as a worker that starts and reconnect-loops with no cause on
    # either side. Catching it at load keeps the failure next to the file that
    # caused it (issue #232).
    (tmp_path / "bad.json").write_text(json.dumps({
        "id": "bad", "name": "bad", "capabilities": ["text_to_image"],
        "source": "x/y", "source_revision": PIN, "min_vram_gb": 3_000_000_000,
    }))
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))

    (tmp_path / "bad.json").unlink()
    (tmp_path / "good.json").write_text(json.dumps({
        "id": "good", "name": "good", "capabilities": ["text_to_image"],
        "source": "x/y", "source_revision": PIN, "min_vram_gb": 12,
    }))
    assert load_manifests(str(tmp_path))[0].min_vram_gb == 12


def test_dtype_round_trips_and_stays_worker_side():
    manifest = Manifest(**{**SD_TURBO, "dtype": "bfloat16"})
    assert manifest.dtype == "bfloat16"
    assert "dtype" not in manifest.wire()


def test_unknown_dtype_is_loud():
    with pytest.raises(ValidationError):
        Manifest(**{**SD_TURBO, "dtype": "fp8"})


def test_pipeline_round_trips_and_stays_worker_side():
    manifest = Manifest(**{**SD_TURBO, "pipeline": "SanaSprint"})
    assert manifest.pipeline == "SanaSprint"
    assert "pipeline" not in manifest.wire()


def test_pipeline_defaults_to_empty_so_autopipeline_still_applies():
    assert Manifest(**SD_TURBO).pipeline == ""


@pytest.mark.parametrize("value", ["sanaSprint", "Sana_Sprint", "Sana Sprint", "1Sana"])
def test_unknown_pipeline_stem_is_loud(value):
    with pytest.raises(ValidationError):
        Manifest(**{**SD_TURBO, "pipeline": value})


CANDIDATE_MANIFESTS = ("flux2-klein-4b", "z-image-turbo", "sana-sprint-06b")


@pytest.mark.parametrize("model_id", CANDIDATE_MANIFESTS)
def test_shipped_candidate_manifests_are_apache_benchmark_only(model_id):
    path = Path(__file__).resolve().parents[1] / "models" / f"{model_id}.json"
    manifest = Manifest.model_validate_json(path.read_text())
    assert manifest.benchmark_only is True
    assert manifest.license_id == "apache-2.0"
    assert manifest.dtype == "bfloat16"
    assert manifest.commercial_max_revenue_usd is None
    assert "realtime" not in manifest.capabilities


def test_unpinned_reference_is_refused(tmp_path):
    """An unpinned repository id follows its default branch, so the same
    release renders differently on two installs (issue #319)."""
    (tmp_path / "bad.json").write_text(
        json.dumps({k: v for k, v in SD_TURBO.items() if k != "source_revision"})
    )
    with pytest.raises(ValueError, match="needs a source_revision"):
        load_manifests(str(tmp_path))


@pytest.mark.parametrize("field,reference", [
    ("vae", "madebyollin/sdxl-vae-fp16-fix"),
    ("preview_decoder", "madebyollin/taesdxl"),
    ("lora", "ByteDance/SDXL-Lightning/sdxl_lightning_8step_lora.safetensors"),
    ("t2i_adapter", "TencentARC/t2i-adapter-sketch-sdxl-1.0"),
])
def test_every_reference_needs_its_own_pin(tmp_path, field, reference):
    """One pin per reference, because a manifest names up to five repositories
    and they move independently."""
    (tmp_path / "bad.json").write_text(json.dumps({**SD_TURBO, field: reference}))
    with pytest.raises(ValueError, match=f"needs a {field}_revision"):
        load_manifests(str(tmp_path))


@pytest.mark.parametrize("revision", [
    "main",                                      # a branch moves
    "v1.0",                                      # so does a tag
    "b261bac",                                   # short shas grow ambiguous
    "B261BAC6FD2CF515557D5D0707481EAFA0485EC2",  # the hub serves lowercase
    "b261bac6fd2cf515557d5d0707481eafa0485ec",   # 39 characters
    "b261bac6fd2cf515557d5d0707481eafa0485ec2z",
])
def test_malformed_revision_is_refused(tmp_path, revision):
    (tmp_path / "bad.json").write_text(
        json.dumps({**SD_TURBO, "source_revision": revision})
    )
    with pytest.raises(ValidationError):
        load_manifests(str(tmp_path))


def test_pin_without_a_reference_is_refused(tmp_path):
    """A pin on an absent reference silently pins nothing."""
    (tmp_path / "bad.json").write_text(json.dumps({**SD_TURBO, "vae_revision": PIN}))
    with pytest.raises(ValueError, match="vae_revision pins nothing"):
        load_manifests(str(tmp_path))


@pytest.mark.parametrize("source", [
    "https://github.com/xinntao/Real-ESRGAN/releases/download",
    "/srv/weights/my-model",
    "./weights/my-model",
])
def test_references_with_nothing_to_pin_stay_loadable(tmp_path, source):
    """A release URL carries its own version and a local path has no history,
    so requiring a sha of either would refuse manifests that are already as
    reproducible as they can be."""
    (tmp_path / "ok.json").write_text(json.dumps({
        **{k: v for k, v in SD_TURBO.items() if k != "source_revision"},
        "source": source,
    }))
    assert load_manifests(str(tmp_path))[0].source == source


def test_every_shipped_reference_is_pinned_to_a_commit():
    """The shipped fleet, not just the schema: an added manifest that names a
    repository without a sha fails here."""
    models_dir = Path(__file__).resolve().parents[1] / "models"
    checked = 0
    for manifest in load_manifests(str(models_dir)):
        for field in ("source", "vae", "preview_decoder", "lora", "t2i_adapter"):
            reference = getattr(manifest, field)
            if not reference or "://" in reference:
                continue
            revision = getattr(manifest, f"{field}_revision")
            assert re.fullmatch(r"[0-9a-f]{40}", revision), (manifest.id, field)
            checked += 1
    # A floor, not a census: it fails if the loop degenerates to checking
    # nothing, and adding a manifest does not make it lie.
    assert checked >= 28


def test_one_repository_is_pinned_to_one_commit_across_manifests():
    """Six manifests share the fp16 VAE and three share a base. Pinning one
    copy and not its siblings would load two revisions of one repository into
    one fleet."""
    models_dir = Path(__file__).resolve().parents[1] / "models"
    pins: dict[str, set[str]] = {}
    for manifest in load_manifests(str(models_dir)):
        for field in ("source", "vae", "preview_decoder", "lora", "t2i_adapter"):
            reference = getattr(manifest, field)
            if not reference or "://" in reference:
                continue
            repo = reference.rpartition("/")[0] if field == "lora" else reference
            pins.setdefault(repo, set()).add(getattr(manifest, f"{field}_revision"))
    assert {repo: found for repo, found in pins.items() if len(found) > 1} == {}


def test_dreamshaper_names_the_canonical_repository_case():
    """The hub answers `lykon/...` with a 307 to `Lykon/...`, so the old
    lowercase id worked while nothing pinned it. A pin should name the
    repository the sha belongs to rather than lean on a redirect."""
    models_dir = Path(__file__).resolve().parents[1] / "models"
    dreamshaper = next(
        m for m in load_manifests(str(models_dir)) if m.id == "dreamshaper-lcm"
    )
    assert dreamshaper.source == "Lykon/dreamshaper-8-lcm"
