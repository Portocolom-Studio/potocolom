from types import SimpleNamespace

import pytest
import torch
from diffusers import AutoencoderKL

from worker.engine import DiffusersEngine
from worker.manifests import Manifest


@pytest.mark.parametrize("device", ["cuda:0", "cpu"])
def test_loaded_pipeline_keeps_decoder_work_within_gpu_tile_budget(monkeypatch, device):
    vae = AutoencoderKL(
        in_channels=3, out_channels=3,
        down_block_types=("DownEncoderBlock2D", "DownEncoderBlock2D"),
        up_block_types=("UpDecoderBlock2D", "UpDecoderBlock2D"),
        block_out_channels=(4, 8), latent_channels=2, norm_num_groups=1, sample_size=32,
    )
    decoded_sizes = []

    def bounded_decoder(_module, inputs):
        shape = inputs[0].shape[-2:]
        decoded_sizes.append(shape)
        if device.startswith("cuda") and max(shape) > vae.tile_latent_min_size:
            raise RuntimeError("decoder working set exceeds GPU test budget")

    vae.decoder.register_forward_pre_hook(bounded_decoder)
    pipeline = SimpleNamespace(vae=vae, set_progress_bar_config=lambda **_kwargs: None)
    engine = DiffusersEngine("cpu")
    engine.device = device
    engine._rungs["fixture"] = "full"
    monkeypatch.setattr(engine, "_pipeline_class", lambda *_args: object)
    monkeypatch.setattr(engine, "_from_pretrained", lambda *_args: pipeline)
    monkeypatch.setattr(engine, "_apply_rung", lambda pipe, *_args: pipe)
    monkeypatch.setattr(engine, "_optimize_resident", lambda *_args, **_kwargs: None)
    manifest = Manifest(id="fixture", name="fixture", capabilities=["text_to_image"])
    loaded = engine._load(manifest, "t2i")

    with torch.no_grad():
        output = loaded.vae.decode(torch.zeros(1, 2, 24, 24)).sample

    assert output.shape == (1, 3, 48, 48)
    assert torch.isfinite(output).all()
    if device == "cpu":
        assert decoded_sizes == [(24, 24)]
    else:
        assert decoded_sizes and all(max(shape) <= vae.tile_latent_min_size for shape in decoded_sizes)
