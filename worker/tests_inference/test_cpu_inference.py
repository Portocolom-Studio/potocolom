import asyncio
import io
import json
import uuid

from PIL import Image

from worker.client import SessionManager, build_runtime, run_job
from worker.settings import Settings


def _save_tiny_pipeline(path) -> None:
    import torch
    from diffusers import AutoencoderKL, DDIMScheduler, StableDiffusionPipeline
    from diffusers import UNet2DConditionModel
    from diffusers.pipelines.stable_diffusion.safety_checker import (
        StableDiffusionSafetyChecker,
    )
    from transformers import (
        CLIPConfig,
        CLIPImageProcessor,
        CLIPTextConfig,
        CLIPTextModel,
        CLIPTokenizer,
        CLIPVisionConfig,
    )

    torch.manual_seed(0)
    tokenizer = CLIPTokenizer(
        vocab={
            "<|startoftext|>": 0,
            "<|endoftext|>": 1,
            "a</w>": 2,
        },
        merges=[],
        bos_token="<|startoftext|>",
        eos_token="<|endoftext|>",
        pad_token="<|endoftext|>",
        model_max_length=77,
    )
    text_config = CLIPTextConfig(
        vocab_size=len(tokenizer),
        hidden_size=8,
        intermediate_size=16,
        num_hidden_layers=1,
        num_attention_heads=1,
        max_position_embeddings=77,
        bos_token_id=tokenizer.bos_token_id,
        eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.pad_token_id,
    )
    vision_config = CLIPVisionConfig(
        hidden_size=8,
        intermediate_size=16,
        num_hidden_layers=1,
        num_attention_heads=1,
        image_size=16,
        patch_size=2,
    )
    safety_checker = StableDiffusionSafetyChecker(CLIPConfig(
        text_config=text_config.to_dict(),
        vision_config=vision_config.to_dict(),
        projection_dim=8,
    ))
    safety_checker.concept_embeds_weights.data.fill_(2.0)
    safety_checker.special_care_embeds_weights.data.fill_(2.0)
    pipeline = StableDiffusionPipeline(
        vae=AutoencoderKL(
            in_channels=3,
            out_channels=3,
            down_block_types=("DownEncoderBlock2D", "DownEncoderBlock2D"),
            up_block_types=("UpDecoderBlock2D", "UpDecoderBlock2D"),
            block_out_channels=(8, 16),
            layers_per_block=1,
            latent_channels=4,
            norm_num_groups=4,
            sample_size=16,
        ),
        text_encoder=CLIPTextModel(text_config),
        tokenizer=tokenizer,
        unet=UNet2DConditionModel(
            sample_size=8,
            in_channels=4,
            out_channels=4,
            layers_per_block=1,
            block_out_channels=(8, 16),
            down_block_types=("CrossAttnDownBlock2D", "DownBlock2D"),
            up_block_types=("UpBlock2D", "CrossAttnUpBlock2D"),
            cross_attention_dim=8,
            attention_head_dim=4,
            norm_num_groups=4,
        ),
        scheduler=DDIMScheduler(
            num_train_timesteps=10,
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            steps_offset=1,
        ),
        safety_checker=safety_checker,
        feature_extractor=CLIPImageProcessor(
            size={"height": 16, "width": 16},
            crop_size={"height": 16, "width": 16},
        ),
        requires_safety_checker=True,
    )
    pipeline.save_pretrained(path / "tiny-pipeline")


class _Response:
    @staticmethod
    def raise_for_status() -> None:
        pass


class _UploadClient:
    puts: list[bytes] = []

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass

    async def put(self, url, *, content, headers):
        self.puts.append(content)
        return _Response()


class _Socket:
    def __init__(self) -> None:
        self.sent = []
        self.ready = asyncio.Event()
        self.frame_sent = asyncio.Event()

    async def send(self, message) -> None:
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message)["type"] == "session_ready":
            self.ready.set()
        elif isinstance(message, bytes):
            self.frame_sent.set()


def test_real_diffusers_worker_dispatches_and_streams_on_cpu(tmp_path, monkeypatch) -> None:
    _save_tiny_pipeline(tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "tiny.json").write_text(json.dumps({
        "id": "ci-tiny",
        "name": "CI tiny random model",
        "capabilities": ["text_to_image", "image_to_image", "realtime"],
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "steps": {"type": "integer", "default": 1},
                "width": {"type": "integer", "default": 16},
                "height": {"type": "integer", "default": 16},
            },
            "required": ["prompt"],
        },
        "source": str(tmp_path / "tiny-pipeline"),
        "min_vram_gb": 0,
    }))

    manifests, engine = build_runtime(Settings(
        device="cpu",
        memory_mode="full",
        models_dir=str(models_dir),
    ))
    manifest = manifests[0]
    monkeypatch.setattr("worker.client.httpx.AsyncClient", _UploadClient)
    monkeypatch.setattr("worker.engine.REALTIME_SIZE", 16)
    _UploadClient.puts = []
    socket = _Socket()
    control = {
        "type": "dispatch_job",
        "job_id": "ci-job",
        "model_id": manifest.id,
        "params": {
            "prompt": "a",
            "steps": 1,
            "width": 16,
            "height": 16,
            "seed": 1,
        },
        "upload": {"url": "http://api/ci-job.png"},
        "dispatch_token": "ci-dispatch",
    }
    canvas = Image.new("RGB", (16, 16), "white")
    canvas.putpixel((8, 8), (0, 0, 0))
    encoded_canvas = io.BytesIO()
    canvas.save(encoded_canvas, format="PNG")

    async def exercise_worker() -> tuple[bytes, int]:
        await engine.load_model(manifest)
        safety_calls = []
        hook = engine._pipelines[(manifest.id, "t2i")].safety_checker.register_forward_hook(
            lambda *_: safety_calls.append(True)
        )
        sessions = SessionManager(socket, engine, manifests)
        try:
            await run_job(socket, engine, manifest, control)
            session_id = uuid.uuid4()
            await sessions.open({
                "type": "open_session",
                "session_id": str(session_id),
                "model_id": manifest.id,
                "params": {"prompt": "a", "strength": 0.7, "seed": 1},
                "control_generation": 1,
            })
            await asyncio.wait_for(socket.ready.wait(), timeout=30)
            sessions.submit(session_id, 1, encoded_canvas.getvalue())
            await asyncio.wait_for(socket.frame_sent.wait(), timeout=30)
            frame = next(message for message in socket.sent if isinstance(message, bytes))
        finally:
            await sessions.shutdown()
            hook.remove()
            await engine.close()
        return frame, len(safety_calls)

    frame_data, safety_calls = asyncio.run(exercise_worker())

    reports = [json.loads(message) for message in socket.sent if isinstance(message, str)]
    job_reports = [report for report in reports if report["type"].startswith("job_")]
    assert [report["type"] for report in job_reports] == ["job_progress", "job_done"]
    assert job_reports[-1]["dispatch_token"] == "ci-dispatch"
    assert job_reports[-1]["width"] == 16
    assert job_reports[-1]["height"] == 16
    assert safety_calls == 2
    assert len(_UploadClient.puts) == 1
    with Image.open(io.BytesIO(_UploadClient.puts[0])) as image:
        image.load()
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.getextrema() != ((0, 0), (0, 0), (0, 0))
    assert frame_data[0] == 0x02
    assert frame_data[1:17] == next(
        uuid.UUID(report["session_id"]).bytes
        for report in reports
        if report["type"] == "session_ready"
    )
    assert int.from_bytes(frame_data[17:21], "big") == 1
    with Image.open(io.BytesIO(frame_data[21:])) as image:
        image.load()
        assert image.format == "WEBP"
        assert image.size == (16, 16)
