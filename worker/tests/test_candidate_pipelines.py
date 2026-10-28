from types import SimpleNamespace

import pytest

from worker.candidate_pipelines import Flux3Adapter, IrisAdapter
from worker.engine import Cancelled, DiffusersEngine, VRAMCapacityError
from worker.manifests import Manifest
from worker.memory_ladder import GB, model_offload_bytes, rung_vram_bytes, select_rung


def test_candidate_offload_floor_controls_selection_admission_and_stays_private():
    manifest = Manifest(
        id="candidate", name="Candidate", capabilities=["text_to_image"],
        min_vram_gb=32, model_offload_vram_gb=14,
    )
    assert model_offload_bytes(32, manifest.model_offload_vram_gb) == 14 * GB
    assert rung_vram_bytes(32, "model_offload", manifest.model_offload_vram_gb) == 14 * GB
    assert select_rung(32, 13 * GB, "auto", on_cpu=False,
                       model_offload_vram_gb=manifest.model_offload_vram_gb) == "group_offload"
    assert select_rung(32, 14 * GB, "auto", on_cpu=False,
                       model_offload_vram_gb=manifest.model_offload_vram_gb) == "model_offload"
    assert "model_offload_vram_gb" not in manifest.wire()


def test_invalid_candidate_offload_floors_are_rejected():
    fields = {
        "id": "candidate", "name": "Candidate", "capabilities": ["text_to_image"],
    }
    for floor in (0, -1, 2**31):
        with pytest.raises(ValueError):
            Manifest(**fields, model_offload_vram_gb=floor)


def test_candidate_loader_refuses_group_offload_before_loading(monkeypatch):
    manifest = Manifest(
        id="candidate", name="Candidate", capabilities=["text_to_image"],
        pipeline="Flux3Distilled", source="unused", source_revision="a" * 40,
    )
    engine = object.__new__(DiffusersEngine)
    engine._rungs = {manifest.id: "group_offload"}
    engine.device = "cuda:0"
    engine.vram_reserve_mb = 512
    loaded = object()
    monkeypatch.setattr("worker.candidate_pipelines.load_candidate_pipeline",
                        lambda *_args, **_kwargs: loaded)
    with pytest.raises(ValueError, match="does not support group_offload"):
        engine._load(manifest, "t2i")
    assert manifest.id not in engine._rungs
    engine._select_rung = lambda _manifest: "model_offload"
    assert engine._load(manifest, "t2i") is loaded


def test_candidate_offload_floor_is_used_for_engine_admission():
    manifest = Manifest(
        id="candidate", name="Candidate", capabilities=["text_to_image"],
        min_vram_gb=32, model_offload_vram_gb=14,
    )
    engine = object.__new__(DiffusersEngine)
    engine.device = "cuda:0"
    engine.memory_mode = "auto"
    engine.vram_reserve_mb = 0
    engine._pipelines = {}
    engine._rungs = {}
    engine._free_vram_bytes = lambda: 14 * GB
    engine._free_gpu_cache = lambda: None
    engine._evict_cold = lambda **_kwargs: None
    engine._ensure_vram(manifest)
    assert engine.model_rung(manifest.id) == "model_offload"

    engine._rungs.clear()
    engine.memory_mode = "model_offload"
    engine._free_vram_bytes = lambda: 13 * GB
    with pytest.raises(VRAMCapacityError, match="requires 15032385536 bytes"):
        engine._ensure_vram(manifest)


class _Manager:
    def __init__(self):
        self.closed = False

    def disable_auto_cpu_offload(self):
        self.closed = True


class _Scheduler:
    def step(self, noise, timestep, latents):
        return (latents + noise,)


class _FluxPipe:
    def __init__(self, steps=3):
        self.scheduler = _Scheduler()
        self.steps = steps
        self.received = None
        self.vae = SimpleNamespace(enable_tiling=lambda: None)

    def to(self, _device):
        return self

    def __call__(self, **kwargs):
        self.received = kwargs
        latents = 0
        for index in range(self.steps):
            latents = self.scheduler.step(index, index, latents)[0]
        return ["image"]


def test_flux_adapter_forwards_seed_configuration_steps_and_restores_scheduler():
    manager = _Manager()
    pipe = _FluxPipe()
    adapter = Flux3Adapter(pipe, manager, device="cpu", rung="full", torch=None)
    generator = object()
    callbacks = []
    result = adapter(
        prompt="scene", num_inference_steps=3, guidance_scale=0, width=768, height=512,
        generator=generator,
        callback_on_step_end=lambda _p, i, t, data: callbacks.append((i, t, data)),
    )
    assert result.images == ["image"]
    assert pipe.received["generator"] is generator
    assert pipe.received["num_inference_steps"] == 3
    assert pipe.received["max_sequence_length"] == 512
    assert [item[0] for item in callbacks] == [0, 1, 2]
    assert callable(pipe.scheduler.step)
    adapter.close()
    adapter.close()
    assert manager.closed


def test_flux_adapter_propagates_cancel_and_restores_scheduler():
    pipe = _FluxPipe()
    adapter = Flux3Adapter(pipe, _Manager(), device="cpu", rung="model_offload", torch=None)

    def cancel(*_args):
        raise Cancelled()

    with pytest.raises(Cancelled):
        adapter(prompt="scene", guidance_scale=0, callback_on_step_end=cancel)
    assert pipe.scheduler.step.__func__ is _Scheduler.step


@pytest.mark.parametrize("unsupported", [
    {"guidance_scale": 1}, {"guidance_scale": 0, "negative_prompt": "letters"},
])
def test_flux_adapter_rejects_unsupported_conditioning_before_inference(unsupported):
    pipe = _FluxPipe()
    adapter = Flux3Adapter(pipe, _Manager(), device="cpu", rung="full", torch=None)
    with pytest.raises(ValueError, match="does not support"):
        adapter(prompt="scene", **unsupported)
    assert pipe.received is None


def test_iris_cpu_tensor_probe_seed_forwarding_and_hook_cleanup():
    torch = pytest.importorskip("torch")
    model = torch.nn.Linear(1, 1, bias=False)
    model.weight.data.fill_(1)
    model_moves, encoder_moves = [], []
    model.to = lambda device: model_moves.append(device) or model
    encoder = SimpleNamespace(to=lambda device: encoder_moves.append(device))
    seed_generator = object()

    def generate(model_arg, _encoder, prompts, height, width, **kwargs):
        assert model_arg is model
        assert prompts == ["tiny"]
        assert kwargs["generator"] is seed_generator
        assert kwargs["steps"] == 2
        for _ in range(2):
            model_arg(torch.ones(1, 1))
        return torch.zeros(1, 3, height, width)

    adapter = IrisAdapter(
        model, encoder, device="test-device", rung="model_offload", torch=torch, generate=generate,
    )
    progress = []
    result = adapter(
        prompt="tiny", width=2, height=2, num_inference_steps=2,
        generator=seed_generator,
        callback_on_step_end=lambda _p, i, _t, _data: progress.append(i),
    )
    assert result.images[0].size == (2, 2)
    assert progress == [0, 1]
    assert len(model._forward_hooks) == 0
    assert model_moves == ["test-device", "cpu"]
    assert encoder_moves == ["cpu"]
    adapter.close()
    adapter.close()


def test_iris_cancel_removes_temporary_hook():
    torch = pytest.importorskip("torch")
    model = torch.nn.Linear(1, 1)
    encoder = SimpleNamespace(to=lambda _device: None)

    def generate(model_arg, *_args, **_kwargs):
        model_arg(torch.ones(1, 1))
        return torch.zeros(1, 3, 1, 1)

    adapter = IrisAdapter(
        model, encoder, device="cpu", rung="model_offload", torch=torch, generate=generate,
    )

    def cancel(*_args):
        raise Cancelled()

    with pytest.raises(Cancelled):
        adapter(prompt="tiny", callback_on_step_end=cancel)
    assert len(model._forward_hooks) == 0


def test_moss_cpu_prompt_encoding_seed_step_count_and_cancellation_cleanup():
    torch = pytest.importorskip("torch")
    from PIL import Image
    from worker.candidate_pipelines import MossAdapter

    class Tokenizer:
        def apply_chat_template(self, *_args, **_kwargs):
            return "formatted prompt"

        def __call__(self, *_args, **_kwargs):
            class Inputs(dict):
                def to(self, _device):
                    return self

            return Inputs(input_ids=torch.ones(1, 2, dtype=torch.long))

    encode_calls = []

    class Encoder:
        def __call__(self, input_ids, **_kwargs):
            encode_calls.append(1)
            assert not torch.is_grad_enabled()
            return SimpleNamespace(last_hidden_state=torch.ones(1, 2, 3))

    class Component:
        def __init__(self):
            self.device = "cpu"

        def to(self, device):
            self.device = device
            return self

        def enable_tiling(self):
            return None

    class Scheduler:
        def step(self, _prediction, timestep, latents, **_kwargs):
            return (latents + 1,)

    class Pipe:
        def __init__(self):
            self.text_encoder, self.tokenizer = Encoder(), Tokenizer()
            self.transformer, self.vae = Component(), Component()
            self.scheduler = Scheduler()
            self.generator = None

        def __call__(self, **kwargs):
            self.generator = kwargs["generator"]
            assert kwargs["prompt"] is None
            assert kwargs["prompt_embeds"].shape == (1, 2, 3)
            if kwargs["guidance_scale"] != 1:
                assert kwargs["negative_prompt_embeds"] is not None
                assert kwargs["negative_prompt_attention_mask"] is not None
            assert self.transformer.device == "test-device"
            assert self.vae.device == "cpu"
            for index in range(kwargs["num_inference_steps"]):
                self.scheduler.step(None, index, torch.tensor(0))
                if index + 1 < kwargs["num_inference_steps"]:
                    assert self.transformer.device == "test-device"
                    assert self.vae.device == "cpu"
            assert self.transformer.device == "cpu"
            assert self.vae.device == "test-device"
            return SimpleNamespace(images=[Image.new("RGB", (1, 1))])

    pipe = Pipe()
    adapter = MossAdapter(pipe, device="test-device", rung="model_offload", torch=torch)
    seed_generator = object()
    progress = []
    output = adapter(
        prompt="caption", num_inference_steps=2, generator=seed_generator,
        callback_on_step_end=lambda _p, i, _t, _data: progress.append(i),
    )
    assert output.images[0].size == (1, 1)
    assert pipe.generator is seed_generator
    assert progress == [0, 1]
    assert pipe.scheduler.step.__func__ is Scheduler.step
    assert pipe.transformer.device == pipe.vae.device == "cpu"
    before = len(encode_calls)
    adapter(prompt="caption", num_inference_steps=1, guidance_scale=1)
    assert len(encode_calls) == before + 1

    def cancel(*_args):
        raise Cancelled()

    with pytest.raises(Cancelled):
        adapter(prompt="caption", num_inference_steps=1, callback_on_step_end=cancel)
    assert pipe.scheduler.step.__func__ is Scheduler.step
    assert pipe.transformer.device == pipe.vae.device == "cpu"
    adapter.close()
    adapter.close()


def test_candidate_engine_rejects_image_editing_before_loading(monkeypatch):
    manifest = Manifest(id="candidate", name="Candidate", capabilities=["text_to_image"],
                        pipeline="Iris3B")
    engine = object.__new__(DiffusersEngine)
    engine._rungs = {manifest.id: "full"}
    engine.device = "cpu"
    monkeypatch.setattr("worker.candidate_pipelines.load_candidate_pipeline",
                        lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="only supports queued text-to-image"):
        engine._load(manifest, "i2i")


def test_candidate_loader_rejects_an_unknown_pipeline():
    from worker.candidate_pipelines import load_candidate_pipeline
    manifest = Manifest(id="candidate", name="Candidate", capabilities=["text_to_image"],
                        pipeline="Unknown")
    with pytest.raises(ValueError, match="unsupported candidate pipeline"):
        load_candidate_pipeline(manifest, rung="full", device="cpu")


def test_candidate_does_not_demote_into_unsupported_group_offload():
    manifest = Manifest(id="candidate", name="Candidate", capabilities=["text_to_image"],
                        pipeline="MOSSImage")
    engine = object.__new__(DiffusersEngine)
    engine.memory_mode = "auto"
    engine._rungs = {manifest.id: "model_offload"}
    evicted = []
    engine._evict_model = lambda model_id: evicted.append(model_id)
    assert engine._demote_rung(manifest, phase="generate") is False
    assert engine._rungs[manifest.id] == "model_offload"
    assert not evicted


def test_candidate_eviction_forgets_pipeline_and_rung_when_close_fails():
    def close():
        raise RuntimeError("device fault")

    engine = object.__new__(DiffusersEngine)
    key = ("candidate", "t2i")
    engine._pipelines = {key: SimpleNamespace(close=close)}
    engine._rungs = {"candidate": "model_offload"}
    engine._detach_preview_decoder = lambda _pipeline: None
    with pytest.raises(RuntimeError, match="device fault"):
        engine._drop_pipeline(key)
    assert key not in engine._pipelines
    assert "candidate" not in engine._rungs


def test_iris_null_prompt_cache_uses_memory_and_never_calls_disk_cache():
    from worker.candidate_pipelines import _IrisPromptEncoder
    calls = []

    class Encoder:
        def encode(self, prompts):
            calls.append(prompts)
            return object()

        def null(self, _prompt=""):
            raise AssertionError("upstream null writes a disk cache")

        def to(self, _device):
            return self

    encoder = _IrisPromptEncoder(Encoder())
    empty = encoder.null()
    assert encoder.null() is empty
    assert encoder.null("no text") is not empty
    assert calls == [[""], ["no text"]]
    assert encoder.to("cpu") is encoder


def test_candidate_prompt_kwargs_do_not_use_clip_tokenizers():
    engine = object.__new__(DiffusersEngine)
    manifest = Manifest(id="candidate", name="Candidate", capabilities=["text_to_image"],
                        pipeline="Iris3B", prompt_token_limit=300)
    assert engine._prompt_kwargs(object(), manifest, "scene", "negative") == {
        "prompt": "scene", "negative_prompt": "negative",
    }


def test_candidate_source_must_be_pinned():
    from worker.candidate_pipelines import _revision
    manifest = Manifest(id="candidate", name="Candidate", capabilities=["text_to_image"])
    with pytest.raises(ValueError, match="pinned source_revision"):
        _revision(manifest)
