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
    "prompt_token_limit": 77,
    "source": "stabilityai/sd-turbo",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
}


def test_load_manifests_and_wire_shape(tmp_path):
    (tmp_path / "sd-turbo.json").write_text(
        json.dumps({
            **SD_TURBO,
            "preview_vae": "madebyollin/taesd",
            "quantize": "text_encoder_3:int8",
        })
    )
    manifests = load_manifests(str(tmp_path))
    assert [m.id for m in manifests] == ["sd-turbo"]
    assert manifests[0].preview_vae == "madebyollin/taesd"
    assert manifests[0].quantize == "text_encoder_3:int8"
    wire = manifests[0].wire()
    assert wire["capabilities"] == ["text_to_image", "realtime"]
    assert wire["prompt_token_limit"] == 77  # the studio warning reads this
    assert "source" not in wire  # weight locations stay worker side
    assert "preview_vae" not in wire
    assert "quantize" not in wire


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
    assert sdxl_turbo.benchmark_only
    dreamshaper = next(m for m in manifests if m.id == "dreamshaper-lcm")
    assert dreamshaper.benchmark_only
    vega = next(m for m in manifests if m.id == "vega-rt")
    assert not vega.benchmark_only
    assert vega.scheduler == "lcm"
    assert vega.preview_vae == "madebyollin/taesdxl"
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
