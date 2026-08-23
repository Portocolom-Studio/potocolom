import asyncio
import io
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from worker.engine import (
    CODEC_CONCURRENCY_LIMIT,
    DiffusersEngine,
    GeneratedFrame,
    PromptCache,
    SimulatedEngine,
)
from worker.manifests import Manifest, SIMULATED_MANIFEST


PROFILE_KEYS = {
    "adapter_ms",
    "text_encode_ms",
    "unet_ms",
    "taesd_ms",
    "overhead_ms",
    "text_encode_cache_hit",
    "unet_forwards",
}


class _FakeTensor:
    def __init__(self, shape, *, values=None):
        self.shape = tuple(shape)
        self.values = values

    @property
    def ndim(self):
        return len(self.shape)

    def __getitem__(self, index):
        if type(index) is not int:
            raise TypeError("fake tensor only implements integer indexing")
        return _FakeTensor(self.shape[1:])


class _FakeTorch:
    class OutOfMemoryError(RuntimeError):
        pass

    class _Context:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    @staticmethod
    def no_grad():
        return _FakeTorch._Context()

    @staticmethod
    def inference_mode():
        return _FakeTorch._Context()

    @staticmethod
    def tensor(values, *, device):
        assert device == "cpu"
        return _FakeTensor((len(values), len(values[0])), values=values)

    @staticmethod
    def cat(tensors, dim):
        shape = list(tensors[0].shape)
        axis = dim if dim >= 0 else len(shape) + dim
        shape[axis] = sum(tensor.shape[axis] for tensor in tensors)
        return _FakeTensor(shape)

    @staticmethod
    def zeros_like(tensor):
        return _FakeTensor(tensor.shape)


class _FakeTokenizer:
    pad_token_id = 0
    bos_token_id = 1
    eos_token_id = 2

    def __call__(self, prompt, *, add_special_tokens, truncation):
        assert add_special_tokens is False
        assert truncation is False
        return {"input_ids": [10 + int(word[1:]) for word in prompt.split()]}

    @staticmethod
    def num_special_tokens_to_add(*, pair):
        assert pair is False
        return 2


class _FakeEncoderOutput:
    def __init__(self, hidden):
        self.hidden_states = [hidden, hidden]

    def __getitem__(self, index):
        assert index == 0
        return _FakeTensor((1, 4))


class _FakeTextEncoder:
    def __init__(self):
        self.config = SimpleNamespace(use_attention_mask=True)

    def __call__(self, input_ids, *, attention_mask, output_hidden_states=False):
        assert attention_mask.shape == input_ids.shape
        hidden = _FakeTensor((1, input_ids.shape[1], 4))
        if output_hidden_states:
            return _FakeEncoderOutput(hidden)
        return (hidden,)


class _HookModule:
    def __init__(self):
        self._pre_hooks = []
        self._hooks = []

    def register_forward_pre_hook(self, hook):
        self._pre_hooks.append(hook)
        return SimpleNamespace(remove=lambda: self._pre_hooks.remove(hook))

    def register_forward_hook(self, hook):
        self._hooks.append(hook)
        return SimpleNamespace(remove=lambda: self._hooks.remove(hook))

    def __call__(self, *args, **kwargs):
        for hook in tuple(self._pre_hooks):
            hook(self, args)
        result = SimpleNamespace()
        for hook in tuple(self._hooks):
            hook(self, args, result)
        return result


class _FakeVae:
    config = SimpleNamespace(scaling_factor=1.0)

    def encode(self, image):
        raise AssertionError("realtime adapter path called vae.encode")


class _ProfilePipeline:
    def __init__(self):
        self.tokenizer = _FakeTokenizer()
        self.text_encoder = _FakeTextEncoder()
        self.tokenizer_2 = None
        self.text_encoder_2 = None
        self.config = SimpleNamespace(force_zeros_for_empty_prompt=True)
        self.adapter = _HookModule()
        self.unet = _HookModule()
        self.vae = _FakeVae()
        self.image_processor = SimpleNamespace(
            postprocess=lambda decoded, output_type: [Image.new("RGB", (32, 32), (1, 2, 3))]
        )
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        self.adapter()
        self.unet()
        self.unet()
        if kwargs.get("output_type") == "latent":
            return SimpleNamespace(images=_FakeTensor((1, 4, 8, 8)))
        return SimpleNamespace(images=[Image.new("RGB", (32, 32), (1, 2, 3))])


def _payload():
    buffer = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(buffer, "PNG")
    return buffer.getvalue()


def _manifest():
    return Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        prompt_token_limit=77,
        t2i_adapter="org/vega-sketch",
        preview_decoder="madebyollin/taesdxl",
    )


def _engine(pipeline):
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = _FakeTorch()
    engine.device = "cpu"
    engine.dtype = object()
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    engine._pipeline = MagicMock(return_value=pipeline)
    return engine


def _decoder_modules():
    decoder = MagicMock()
    decoder.to.return_value = decoder
    decoder.decode.return_value = (_FakeTensor((1, 3, 32, 32)),)
    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.return_value = decoder
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    return diffusers


def _frame(engine, manifest, *, prompt_cache=None, profile=False):
    return asyncio.run(
        engine.frame(
            manifest,
            {"prompt": "w0 w1"},
            _payload(),
            prompt_cache=prompt_cache,
            profile=profile,
        )
    )


def test_default_frame_has_no_stages():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        result = _frame(engine, _manifest())
    assert result.stages is None


def test_profile_returns_exact_stage_keys():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        result = _frame(engine, _manifest(), profile=True)
    assert set(result.stages) == PROFILE_KEYS
    assert all(type(value) is int for value in result.stages.values())


def test_adapter_frame_does_not_encode_vae():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        result = _frame(engine, _manifest())
    assert isinstance(result, GeneratedFrame)


def test_prompt_cache_miss_then_hit():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    cache = PromptCache()
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        first = _frame(engine, _manifest(), prompt_cache=cache, profile=True)
        second = _frame(engine, _manifest(), prompt_cache=cache, profile=True)
    assert first.stages["text_encode_cache_hit"] == 0
    assert second.stages["text_encode_cache_hit"] == 1


def test_simulated_engine_honors_profile():
    engine = SimulatedEngine(0.0)
    default = asyncio.run(engine.frame(SIMULATED_MANIFEST, {}, _payload()))
    profiled = asyncio.run(
        engine.frame(SIMULATED_MANIFEST, {}, _payload(), profile=True)
    )
    assert default.stages is None
    assert set(profiled.stages) == PROFILE_KEYS
    assert all(type(value) is int for value in profiled.stages.values())


def test_profile_counts_unet_forwards():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        result = _frame(engine, _manifest(), profile=True)
    assert result.stages["unet_forwards"] == 2


def test_profile_overhead_is_nonnegative_int():
    pipeline = _ProfilePipeline()
    engine = _engine(pipeline)
    with patch.dict("sys.modules", {"diffusers": _decoder_modules()}):
        result = _frame(engine, _manifest(), profile=True)
    assert type(result.stages["overhead_ms"]) is int
    assert result.stages["overhead_ms"] >= 0
