import asyncio
import io
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from worker.engine import (
    CALIBRATION_SAMPLES,
    Cancelled,
    CODEC_CONCURRENCY_LIMIT,
    DiffusersEngine,
    NotResidentError,
    OBSERVED_FRAME_SAMPLES,
    OBSERVED_FRAME_WINDOW,
    PromptCache,
    REALTIME_SIZE,
    SimulatedEngine,
    _canvas_to_sketch_map,
    encode_webp,
    reject_degenerate_output,
)
from worker.manifests import Manifest, SIMULATED_MANIFEST
from worker.memory_ladder import slots_from_frame_ms


def _fake_oom() -> type[BaseException]:
    """torch.OutOfMemoryError subclasses RuntimeError, and the preview decoder's
    failure handling treats RuntimeError as recoverable. A fake based on Exception
    would hide that interaction, so these tests must mirror the real hierarchy.
    """
    return type("OutOfMemoryError", (RuntimeError,), {})


class _FakeTensor:
    def __init__(self, shape, *, values=None, marker=None):
        self.shape = tuple(shape)
        self.values = values
        self.marker = marker

    @property
    def ndim(self):
        return len(self.shape)

    def __getitem__(self, index):
        if type(index) is not int:
            raise TypeError("fake tensor only implements integer indexing")
        if not self.shape or not -self.shape[0] <= index < self.shape[0]:
            raise IndexError("fake tensor index out of range")
        return _FakeTensor(self.shape[1:], marker=self.marker)


class _FakeTorch:
    OutOfMemoryError = type("OutOfMemoryError", (Exception,), {})

    class _NoGrad:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    @staticmethod
    def no_grad():
        return _FakeTorch._NoGrad()

    @staticmethod
    def tensor(values, *, device):
        assert device == "cpu"
        return _FakeTensor((len(values), len(values[0])), values=values)

    @staticmethod
    def cat(tensors, dim):
        shape = list(tensors[0].shape)
        axis = dim if dim >= 0 else len(shape) + dim
        shape[axis] = sum(tensor.shape[axis] for tensor in tensors)
        return _FakeTensor(shape, marker=tensors[0].marker)

    @staticmethod
    def zeros_like(tensor):
        return _FakeTensor(tensor.shape, marker=0)


class _FakeTokenizer:
    # Only the attributes a real CLIPTokenizer actually exposes. This fake must
    # never offer a method the real one lacks: it previously had
    # build_inputs_with_special_tokens, which transformers 5 removed, and these
    # tests passed while every long prompt raised AttributeError on a real model.
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
    def __init__(self, pooled, hidden):
        self.pooled = pooled
        self.hidden_states = [hidden, hidden]

    def __getitem__(self, index):
        assert index == 0
        return self.pooled


class _FakeTextEncoder:
    def __init__(self, width):
        self.config = SimpleNamespace(use_attention_mask=True)
        self.width = width
        self.calls = 0

    def __call__(self, input_ids, *, attention_mask, output_hidden_states=False):
        self.calls += 1
        assert attention_mask.shape == input_ids.shape
        hidden = _FakeTensor((1, input_ids.shape[1], self.width))
        if output_hidden_states:
            first_token = input_ids.values[0][1]
            return _FakeEncoderOutput(
                _FakeTensor((1, self.width), marker=first_token),
                hidden,
            )
        return (hidden,)


def _fake_prompt_engine():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = _FakeTorch()
    engine.device = "cpu"
    return engine


def _fake_pipeline(*, dual=False):
    pipeline = SimpleNamespace(
        tokenizer=_FakeTokenizer(),
        text_encoder=_FakeTextEncoder(4),
        tokenizer_2=None,
        text_encoder_2=None,
        config=SimpleNamespace(force_zeros_for_empty_prompt=True),
    )
    if dual:
        pipeline.tokenizer_2 = _FakeTokenizer()
        pipeline.text_encoder_2 = _FakeTextEncoder(6)
    return pipeline


def _clip_manifest():
    return Manifest(
        id="clip",
        name="CLIP",
        capabilities=["text_to_image"],
        prompt_token_limit=77,
    )


class _RenderPipeline:
    """A diffusers-style pipeline that renders and records what it was given.

    Composed from the shared tokenizer/encoder fakes so the prompt encoding
    counts are observable across frames, exactly like a real pipeline's
    text encoders are.
    """

    def __init__(self):
        self.tokenizer = _FakeTokenizer()
        self.text_encoder = _FakeTextEncoder(4)
        self.tokenizer_2 = None
        self.text_encoder_2 = None
        self.config = SimpleNamespace(force_zeros_for_empty_prompt=True)
        self.render_kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.render_kwargs.append(kwargs)
        return SimpleNamespace(images=[Image.new("RGB", (32, 32))])


class _JobPipeline:
    """Records pipeline keyword arguments for job-path tests."""

    def __init__(self, *, dual: bool = False):
        fake = _fake_pipeline(dual=dual)
        self.tokenizer = fake.tokenizer
        self.text_encoder = fake.text_encoder
        self.tokenizer_2 = fake.tokenizer_2
        self.text_encoder_2 = fake.text_encoder_2
        self.config = fake.config
        self.call_kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.call_kwargs.append(kwargs)
        image = Image.new("RGB", (64, 48))
        image.putpixel((1, 0), (1, 0, 0))
        return SimpleNamespace(images=[image])


def _job_engine(pipeline: _JobPipeline) -> DiffusersEngine:
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = _FakeTorch()
    engine.device = "cpu"
    engine._pipelines = {}
    engine._pipeline = MagicMock(return_value=pipeline)
    return engine


def _sdxl_job_manifest() -> Manifest:
    return Manifest(
        id="sdxl-base",
        name="SDXL",
        capabilities=["text_to_image", "image_to_image"],
        prompt_token_limit=77,
    )


def _input_image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (128, 96), (40, 80, 120)).save(buffer, "WEBP")
    return buffer.getvalue()


def _fake_content_token_ids(text: str) -> list[int]:
    return list(
        _FakeTokenizer()(
            text, add_special_tokens=False, truncation=False,
        )["input_ids"],
    )


def _expected_pooled_marker(text: str) -> int:
    token_ids = _fake_content_token_ids(text)
    if token_ids:
        return token_ids[0]
    return _FakeTokenizer.eos_token_id


def _assert_negative_reachable(
    kwargs: dict,
    expected_negative: str,
    *,
    expected_positive: str | None = None,
) -> None:
    negative_embeds = kwargs.get("negative_prompt_embeds")
    if negative_embeds is not None:
        prompt_embeds = kwargs["prompt_embeds"]
        if negative_embeds is prompt_embeds:
            raise AssertionError(
                "negative_prompt_embeds is the same object as prompt_embeds",
            )
        negative_pooled = kwargs.get("negative_pooled_prompt_embeds")
        if negative_pooled is not None:
            expected_marker = _expected_pooled_marker(expected_negative)
            if negative_pooled.marker != expected_marker:
                raise AssertionError(
                    "negative_pooled_prompt_embeds marker does not match "
                    "the expected negative text",
                )
            pooled_prompt = kwargs.get("pooled_prompt_embeds")
            if pooled_prompt is not None and negative_pooled is pooled_prompt:
                raise AssertionError(
                    "negative_pooled_prompt_embeds is the same object as "
                    "pooled_prompt_embeds",
                )
            if expected_positive is not None:
                if negative_pooled.marker == _expected_pooled_marker(
                    expected_positive,
                ):
                    raise AssertionError(
                        "negative pooled marker matches the positive text",
                    )
        else:
            negative_tokens = len(_fake_content_token_ids(expected_negative))
            if negative_embeds.shape[1] != negative_tokens:
                raise AssertionError(
                    "negative_prompt_embeds sequence length does not match "
                    "the expected negative token count",
                )
        return

    negative_text = kwargs.get("negative_prompt")
    if negative_text is not None:
        if negative_text != expected_negative:
            raise AssertionError(
                "negative_prompt does not match the expected negative text",
            )
        return

    raise AssertionError(
        "pipeline kwargs lack negative_prompt_embeds or negative_prompt",
    )


def _frame_engine(pipeline: _RenderPipeline) -> DiffusersEngine:
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = _FakeTorch()
    engine.device = "cpu"
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    engine._pipeline = MagicMock(return_value=pipeline)
    return engine


def _realtime_manifest() -> Manifest:
    return Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        prompt_token_limit=77,
    )


def _canvas_payload() -> bytes:
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")
    return source.getvalue()


def test_long_prompt_embeddings_span_multiple_clip_windows():
    engine = _fake_prompt_engine()
    pipeline = _fake_pipeline()

    kwargs = engine._prompt_kwargs(
        pipeline,
        _clip_manifest(),
        " ".join(f"w{index}" for index in range(80)),
        None,
    )

    assert kwargs["prompt_embeds"].shape == (1, 154, 4)
    assert kwargs["prompt_embeds"].shape[1] > 77
    assert kwargs["prompt_embeds"].shape[1] % 77 == 0


def test_long_positive_and_short_negative_embeddings_have_matching_shapes():
    engine = _fake_prompt_engine()
    pipeline = _fake_pipeline()

    kwargs = engine._prompt_kwargs(
        pipeline,
        _clip_manifest(),
        " ".join(f"w{index}" for index in range(80)),
        "w0",
    )

    assert kwargs["prompt_embeds"].shape == kwargs["negative_prompt_embeds"].shape
    assert kwargs["negative_prompt_embeds"].shape == (1, 154, 4)


def test_short_prompt_embeds_without_chunking():
    """A short prompt must produce embeddings too, or the realtime cache
    never engages for the common case: the string path is re-encoded inside
    diffusers on every frame."""
    engine = _fake_prompt_engine()
    pipeline = _fake_pipeline()

    kwargs = engine._prompt_kwargs(pipeline, _clip_manifest(), "w0 w1", None)

    assert "prompt" not in kwargs
    assert kwargs["prompt_embeds"].shape == (1, 77, 4)
    # One encode is two calls on the single CLIP: the positive prompt and the
    # empty-string negative prompt both pass through the encoder.
    assert pipeline.text_encoder.calls == 2


def test_merge_prompt_embeds_refuses_prompt_only_kwargs():
    engine = _fake_prompt_engine()
    assert engine._merge_prompt_embeds([
        {"prompt": "a"}, {"prompt": "b"},
    ]) is None


def test_merge_prompt_embeds_refuses_mismatched_chunk_counts():
    engine = _fake_prompt_engine()
    assert engine._merge_prompt_embeds([
        {"prompt_embeds": _FakeTensor((1, 77, 4))},
        {"prompt_embeds": _FakeTensor((1, 154, 4))},
    ]) is None


def test_merge_prompt_embeds_stacks_matching_tensors():
    engine = _fake_prompt_engine()
    merged = engine._merge_prompt_embeds([
        {"prompt": "a", "prompt_embeds": _FakeTensor((1, 77, 4))},
        {"prompt": "b", "prompt_embeds": _FakeTensor((1, 77, 4))},
    ])
    assert merged is not None
    assert merged["prompt_embeds"].shape == (2, 77, 4)
    assert "prompt" not in merged


def test_third_text_encoder_keeps_pipeline_prompt_path():
    engine = _fake_prompt_engine()
    pipeline = _fake_pipeline(dual=True)
    pipeline.tokenizer_3 = object()
    pipeline.text_encoder_3 = object()
    prompt = " ".join(f"w{index}" for index in range(80))

    kwargs = engine._prompt_kwargs(pipeline, _clip_manifest(), prompt, "w0")

    assert kwargs == {"prompt": prompt, "negative_prompt": "w0"}
    assert pipeline.text_encoder.calls == 0
    assert pipeline.text_encoder_2.calls == 0


def test_fake_tokenizer_only_offers_what_a_real_one_does():
    """The chunking code is exercised above against a fake tokenizer, which can
    drift from the library and hide a crash. It already did once: the fake
    provided build_inputs_with_special_tokens, transformers 5 does not, and
    every long prompt raised AttributeError on a real model while these tests
    passed. Skips where transformers is absent, as the upscale tests do for torch.
    """
    pytest = __import__("pytest")
    pytest.importorskip("transformers")
    from transformers import CLIPTokenizer

    try:
        real = CLIPTokenizer.from_pretrained(
            "stabilityai/sd-turbo", subfolder="tokenizer", local_files_only=True,
        )
    except Exception as error:  # not cached on this machine
        pytest.skip(f"no cached CLIP tokenizer: {error}")

    for attribute in ("bos_token_id", "eos_token_id", "pad_token_id",
                      "num_special_tokens_to_add", "model_max_length"):
        assert hasattr(real, attribute), f"engine uses tokenizer.{attribute}"
    assert real.num_special_tokens_to_add(pair=False) == 2


def test_realtime_session_encodes_its_prompt_once_across_frames():
    """A realtime session encodes the same prompt for every frame, and
    encoding is a full pass through each text encoder, so the second frame
    must not pay for it again (measured at 322 ms on CPU for two chunks
    against a 250 ms frame budget). The holder is per session, and the two
    renders receive the very same tensors: nothing else about a frame (seed,
    canvas, structure strength) reaches the text encoders."""
    pipeline = _RenderPipeline()
    engine = _frame_engine(pipeline)
    manifest = _realtime_manifest()
    cache = PromptCache()

    async def scenario():
        await engine.frame(manifest, {"prompt": "w0 w1"}, _canvas_payload(),
                           prompt_cache=cache)
        await engine.frame(manifest, {"prompt": "w0 w1"}, _canvas_payload(),
                           prompt_cache=cache)

    asyncio.run(scenario())
    # One encode across both frames: the positive and the empty-string
    # negative each pass through the single CLIP once on the first frame,
    # and the second frame renders the cached tensors untouched.
    assert pipeline.text_encoder.calls == 2
    assert len(pipeline.render_kwargs) == 2
    assert pipeline.render_kwargs[0]["prompt_embeds"] is pipeline.render_kwargs[1]["prompt_embeds"]


def test_editing_the_prompt_between_frames_reencodes():
    """The key is the encoded text, so an edited prompt misses and encodes
    once, which is exactly what update_session carrying a new prompt is."""
    pipeline = _RenderPipeline()
    engine = _frame_engine(pipeline)
    manifest = _realtime_manifest()
    cache = PromptCache()

    async def scenario():
        await engine.frame(manifest, {"prompt": "w0 w1"}, _canvas_payload(),
                           prompt_cache=cache)
        await engine.frame(manifest, {"prompt": "w7 w8"}, _canvas_payload(),
                           prompt_cache=cache)

    asyncio.run(scenario())
    # Each distinct prompt is encoded once (positive + empty negative).
    assert pipeline.text_encoder.calls == 4


def test_two_sessions_with_different_prompts_do_not_evict_each_other():
    """The cache belongs to the session, so interleaved sessions with
    different prompts each encode theirs once. A single pipeline-level entry
    fails this: every frame from the other session evicts it, so each frame
    re-encodes (eight encoder calls here)."""
    pipeline = _RenderPipeline()
    engine = _frame_engine(pipeline)
    manifest = _realtime_manifest()
    first = PromptCache()
    second = PromptCache()

    async def scenario():
        for _ in range(2):
            await engine.frame(manifest, {"prompt": "w0 w1"}, _canvas_payload(),
                               prompt_cache=first)
            await engine.frame(manifest, {"prompt": "w7 w8"}, _canvas_payload(),
                               prompt_cache=second)

    asyncio.run(scenario())
    # One encode per session, four frames rendered: each session's second
    # frame is a cache hit, so only two encodes (four encoder calls) happen.
    assert pipeline.text_encoder.calls == 4
    assert len(pipeline.render_kwargs) == 4


def test_realtime_sd3_frames_keep_the_string_prompt_path():
    """SD3 requires joint CLIP and T5 prompt embeddings; this CLIP-only
    chunker cannot satisfy that contract, so its frames must keep passing the
    raw prompt to diffusers rather than embeddings, cached or not."""
    pipeline = _RenderPipeline()
    pipeline.tokenizer_3 = object()
    pipeline.text_encoder_3 = object()
    engine = _frame_engine(pipeline)
    manifest = _realtime_manifest()
    cache = PromptCache()

    async def scenario():
        for _ in range(2):
            await engine.frame(manifest, {"prompt": "w0 w1"}, _canvas_payload(),
                               prompt_cache=cache)

    asyncio.run(scenario())
    assert pipeline.text_encoder.calls == 0
    assert all(kwargs.get("prompt") == "w0 w1" for kwargs in pipeline.render_kwargs)
    assert all("prompt_embeds" not in kwargs for kwargs in pipeline.render_kwargs)


def test_generate_job_passes_negative_prompt_to_pipeline():
    """Shipped sdxl-base guidance default is 6; a queued job at that value must
    still reach _prompt_kwargs so the user's negative prompt is encoded, not
    dropped by a zero-guidance shortcut that only forwards positive embeds."""
    pipeline = _JobPipeline(dual=True)
    engine = _job_engine(pipeline)
    manifest = _sdxl_job_manifest()
    params = {
        "prompt": "w0 w1",
        "negative_prompt": "w3",
        "guidance": 6.0,
    }
    loop = asyncio.new_event_loop()
    try:
        result = engine._generate(manifest, params, lambda _: None, loop)
    finally:
        loop.close()

    _assert_negative_reachable(
        pipeline.call_kwargs[0], "w3", expected_positive="w0 w1",
    )
    assert result.width == 64
    assert result.height == 48


def test_generate_i2i_job_passes_negative_prompt_to_pipeline():
    """Shipped ssd-1b guidance default is 7; img2img at that value must encode
    the negative prompt the same way, not route through a helper that drops it."""
    pipeline = _JobPipeline(dual=True)
    engine = _job_engine(pipeline)
    manifest = _sdxl_job_manifest()
    params = {
        "prompt": "w0 w1",
        "negative_prompt": "w5 w6 w7",
        "guidance": 7.0,
    }
    loop = asyncio.new_event_loop()
    try:
        result = engine._generate_i2i(
            manifest, params, lambda _: None, loop, _input_image_bytes(),
        )
    finally:
        loop.close()

    _assert_negative_reachable(
        pipeline.call_kwargs[0], "w5 w6 w7", expected_positive="w0 w1",
    )
    assert result.width == 64
    assert result.height == 48


def test_sdxl_pooled_embedding_comes_from_first_chunk():
    engine = _fake_prompt_engine()
    pipeline = _fake_pipeline(dual=True)

    kwargs = engine._prompt_kwargs(
        pipeline,
        _clip_manifest(),
        " ".join(f"w{index}" for index in range(80)),
        "w0",
    )

    assert kwargs["prompt_embeds"].shape == (1, 154, 10)
    assert kwargs["prompt_embeds"].shape == kwargs["negative_prompt_embeds"].shape
    assert kwargs["pooled_prompt_embeds"].shape == (1, 6)
    assert kwargs["pooled_prompt_embeds"].marker == 10


def test_canvas_to_sketch_map_inverts_stroke_polarity():
    """The T2I-Adapter wants light strokes on black; the canvas is dark
    strokes on white. Sample a stroke pixel and a background pixel so an
    inverted (or missing) conversion fails rather than passing by accident."""
    from PIL import ImageDraw

    canvas = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.line((0, 0, REALTIME_SIZE, REALTIME_SIZE), fill=(0, 0, 0), width=8)
    draw.line((REALTIME_SIZE, 0, 0, REALTIME_SIZE), fill=(0, 0, 0), width=8)

    sketch = _canvas_to_sketch_map(canvas)

    assert sketch.size == (REALTIME_SIZE, REALTIME_SIZE)
    assert sketch.mode == "RGB"
    assert sketch.getpixel((REALTIME_SIZE // 2, REALTIME_SIZE // 2)) == (255, 255, 255)
    assert sketch.getpixel((REALTIME_SIZE // 2, REALTIME_SIZE // 4)) == (0, 0, 0)


def test_canvas_to_sketch_map_reads_a_stroke_of_any_saturated_colour():
    """Colour carries no meaning to the sketch adapter, but it must not decide
    whether a stroke exists at all. Thresholding luminance did exactly that
    once the studio canvas gained a palette: green weighs 150 and yellow 226
    against the 128 threshold, so both reached the model as blank paper while
    red at 76 and blue at 29 drew. A pale tint is still paper, which is the
    constraint the studio palette is picked against."""
    from PIL import ImageDraw

    strokes = ((220, 38, 38), (250, 204, 21), (22, 163, 74), (37, 99, 235))
    paper_tint = (255, 224, 224)
    rows = (*strokes, paper_tint)
    canvas = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    band = REALTIME_SIZE // (len(rows) + 1)
    for index, colour in enumerate(rows, start=1):
        draw.line((0, index * band, REALTIME_SIZE, index * band), fill=colour, width=8)

    sketch = _canvas_to_sketch_map(canvas)

    for index in range(1, len(strokes) + 1):
        assert sketch.getpixel((REALTIME_SIZE // 2, index * band)) == (255, 255, 255)
    assert sketch.getpixel((REALTIME_SIZE // 2, len(rows) * band)) == (0, 0, 0)
    assert sketch.getpixel((REALTIME_SIZE // 2, band // 2)) == (0, 0, 0)


def test_eviction_releases_every_entry_a_model_shares_weights_across():
    """A model's realtime and t2i entries share their UNet, text encoders and
    VAE, so freeing its memory means dropping both.

    Eviction works per model rather than per cache key, which is what makes
    that true: _evict_cold collects model ids and _evict_model drops every key
    for the one it picks. Pinned here because the sharing is deliberate and a
    future per-key eviction would free nothing while reporting that it had.
    """
    engine = DiffusersEngine.__new__(DiffusersEngine)
    shared = object()

    class Pipe:
        def __init__(self):
            self.unet = shared

    engine._pipelines = {("rt", "t2i"): Pipe(), ("rt", "realtime"): Pipe(),
                         ("other", "t2i"): Pipe()}
    engine._rungs = {"rt": "full", "other": "full"}
    engine._last_used = {"rt": 1.0, "other": 2.0}
    with patch.object(DiffusersEngine, "_free_gpu_cache", lambda self: None), \
         patch.object(DiffusersEngine, "_free_vram_bytes", lambda self: 0):
        engine._evict_cold(except_model_id="other")

    assert not [key for key in engine._pipelines if key[0] == "rt"]
    assert "rt" not in engine._rungs  # nothing resident, so the rung goes too
    assert ("other", "t2i") in engine._pipelines


def test_load_realtime_registers_the_base_under_the_t2i_key():
    """The realtime pipeline's base must be visible to the cache.

    A realtime session on a full-resident model holds the whole text-to-image
    pipeline, and a queued text-to-image job for the same model afterwards
    must reuse it: without the registration below, the cache has no entry and
    the job loads a second complete pipeline while the first is still held.
    """
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = _FakeTorch()
    engine.device = "cpu"
    engine.dtype = object()
    engine._pipelines = {}
    base = MagicMock()
    base.unet = object()
    pipeline = MagicMock()
    pipeline.unet = base.unet
    adapter = MagicMock()
    diffusers = ModuleType("diffusers")
    diffusers.T2IAdapter = MagicMock()
    diffusers.T2IAdapter.from_pretrained = MagicMock(return_value=adapter)
    diffusers.StableDiffusionXLAdapterPipeline = MagicMock()
    diffusers.StableDiffusionXLAdapterPipeline.from_pipe = MagicMock(return_value=pipeline)
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        t2i_adapter="org/vega-sketch",
    )
    engine._load = MagicMock(return_value=base)

    with patch.dict(sys.modules, {"diffusers": diffusers}):
        loaded = engine._load_realtime(manifest)

    assert loaded is pipeline
    # The base is cached under the t2i key, and it is the very object the
    # realtime pipeline was composed from: the job and the session share the
    # UNet, text encoders and VAE.
    assert engine._pipelines[(manifest.id, "t2i")] is base
    assert diffusers.StableDiffusionXLAdapterPipeline.from_pipe.call_args.args[0] is base
    engine._load.assert_called_once_with(manifest, "t2i")


def test_simulated_gpu_lifecycle():
    engine = SimulatedEngine(0.04)

    async def scenario():
        assert engine.loaded_models() == []
        load_ms = await engine.load_model(SIMULATED_MANIFEST)
        assert load_ms >= 0
        assert engine.loaded_models() == ["sd-sim"]
        await engine.unload_all()
        assert engine.loaded_models() == []

    asyncio.run(scenario())


def test_simulated_generate_with_input_image():
    engine = SimulatedEngine(0.01)
    buffer = io.BytesIO()
    Image.new("RGB", (256, 128), (40, 80, 120)).save(buffer, "WEBP")
    input_image = buffer.getvalue()
    progress_values: list[float] = []

    async def scenario():
        result = await engine.generate(
            SIMULATED_MANIFEST, {"prompt": "blend"}, progress_values.append,
            input_image=input_image,
        )
        return result

    result = asyncio.run(scenario())
    assert progress_values[-1] == 1.0
    assert result.width == 256
    assert result.height == 128
    assert result.load_ms >= 0
    assert engine.loaded_models() == ["sd-sim"]
    assert result.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_simulated_upscale_resizes_by_factor():
    engine = SimulatedEngine(0.01)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buffer, "WEBP")
    input_image = buffer.getvalue()
    manifest = Manifest(
        id="realesrgan",
        name="Real-ESRGAN",
        capabilities=["upscale"],
        parameters={
            "type": "object",
            "properties": {"factor": {"type": "integer", "enum": [2, 4], "default": 2}},
            "required": ["factor"],
        },
    )
    progress_values: list[float] = []

    async def scenario():
        return await engine.generate(
            manifest, {"factor": 4}, progress_values.append, input_image=input_image,
        )

    result = asyncio.run(scenario())
    assert result.width == 256
    assert result.height == 192
    assert progress_values[-1] == 1.0
    assert result.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_simulated_generate_stops_between_steps_when_cancelled():
    engine = SimulatedEngine(0.01)
    progress_values: list[float] = []

    async def scenario():
        await engine.generate(SIMULATED_MANIFEST, {"prompt": "x"},
                              progress_values.append, cancelled=lambda: True)

    try:
        asyncio.run(scenario())
    except Cancelled:
        assert progress_values == []
    else:
        raise AssertionError("expected Cancelled")


def test_job_step_callbacks_abort_when_cancelled():
    """Raising from callback_on_step_end is the only way to stop a pipeline
    already running on the GPU thread, so every job path must do it."""
    pipeline = _JobPipeline(dual=True)
    engine = _job_engine(pipeline)
    manifest = _sdxl_job_manifest()
    params = {"prompt": "w0 w1", "guidance": 6.0}
    loop = asyncio.new_event_loop()
    try:
        engine._generate(manifest, params, lambda _: None, loop, None, lambda: True)
        engine._generate_i2i(manifest, params, lambda _: None, loop,
                             _input_image_bytes(), lambda: True)
    finally:
        loop.close()

    assert len(pipeline.call_kwargs) == 2
    for kwargs in pipeline.call_kwargs:
        try:
            kwargs["callback_on_step_end"](pipeline, 0, None, {})
        except Cancelled:
            continue
        raise AssertionError("a step callback ran on without aborting")


def test_simulated_upscale_requires_input():
    engine = SimulatedEngine(0.01)
    manifest = Manifest(id="realesrgan", name="Real-ESRGAN", capabilities=["upscale"])

    async def scenario():
        await engine.generate(manifest, {"factor": 2}, lambda _: None)

    try:
        asyncio.run(scenario())
    except ValueError as error:
        assert "requires input_image" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_diffusers_measured_manifests_use_free_vram():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {}
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=3 * 1024**3):
        wires = engine.measured_manifests([manifest])
    assert wires[0]["id"] == "xl"
    assert "realtime" not in wires[0]["capabilities"]


def test_measured_manifests_respect_pinned_rungs():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {"xl": "model_offload"}
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    # Plenty of free VRAM, but the loaded pipeline is pinned to an offload
    # rung: the hello must not re-advertise realtime for it.
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=64 * 1024**3):
        wire = engine.measured_manifests([manifest])
    assert "realtime" not in wire[0]["capabilities"]


def test_diffusers_effective_realtime_slots_zero_without_realtime():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {}
    engine._calibrated_slots = None
    manifest = Manifest(
        id="xl",
        name="XL",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=10,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=3 * 1024**3):
        wire = engine.measured_manifests([manifest])
        assert engine.effective_realtime_slots(wire, 2) == 0


def test_diffusers_effective_realtime_slots_uses_calibration():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine._rungs = {"rt": "full"}
    engine._calibrated_slots = 1
    manifest = Manifest(
        id="rt",
        name="RT",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=4,
    )
    with patch.object(DiffusersEngine, "_free_vram_bytes", return_value=64 * 1024**3):
        wire = engine.measured_manifests([manifest])
        assert engine.effective_realtime_slots(wire, 2) == 1


def test_optimize_resident_skips_offload_and_survives_compile_failure():
    torch_stub = MagicMock()
    torch_stub.compile.side_effect = RuntimeError("inductor blew up")

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = True
    engine.attention_backend = "_native_efficient"

    unet = MagicMock()
    unet.set_attention_backend = MagicMock(side_effect=ValueError("no backend"))
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None

    engine._warmup_pipeline = MagicMock()
    engine._optimize_resident(pipeline, "t2i")

    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    torch_stub.compile.assert_called_once()
    engine._warmup_pipeline.assert_not_called()


def test_optimize_resident_reverts_after_warmup_failure():
    torch_stub = MagicMock()
    compiled = MagicMock(name="compiled")
    torch_stub.compile.return_value = compiled

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = True
    engine.attention_backend = "_native_efficient"

    unet = MagicMock(name="eager_unet")
    unet.set_attention_backend = MagicMock()
    unet.reset_attention_backend = MagicMock()
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock(side_effect=RuntimeError("cudagraph boom"))

    engine._optimize_resident(pipeline, "t2i")

    assert pipeline.unet is unet
    engine._warmup_pipeline.assert_called_once_with(pipeline, "t2i")
    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    unet.reset_attention_backend.assert_called_once_with()


def test_optimize_resident_warms_attention_backend_without_compile():
    torch_stub = MagicMock()
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = False
    engine.attention_backend = "_native_efficient"

    unet = MagicMock()
    unet.set_attention_backend = MagicMock()
    pipeline = MagicMock()
    pipeline.unet = unet
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock()

    engine._optimize_resident(pipeline, "t2i")

    unet.set_attention_backend.assert_called_once_with("_native_efficient")
    torch_stub.compile.assert_not_called()
    engine._warmup_pipeline.assert_called_once_with(pipeline, "t2i")


def test_optimize_resident_skipped_when_compile_disabled():
    torch_stub = MagicMock()
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.torch_compile = False
    engine.attention_backend = ""
    pipeline = MagicMock()
    pipeline.unet = MagicMock()
    pipeline.transformer = None
    engine._warmup_pipeline = MagicMock()

    engine._optimize_resident(pipeline, "t2i")

    torch_stub.compile.assert_not_called()
    engine._warmup_pipeline.assert_not_called()


def test_load_quantizes_named_component_before_device_move():
    pipeline = MagicMock()
    component = object()
    pipeline.text_encoder_3 = component
    events: list[str] = []

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cpu"
    engine._pipelines = {}
    engine._from_pretrained = MagicMock(return_value=pipeline)
    engine._pick_rung = MagicMock(return_value="full")
    engine._apply_rung = MagicMock(
        side_effect=lambda *_args: events.append("apply_rung") or pipeline,
    )
    engine._optimize_resident = MagicMock()

    diffusers = ModuleType("diffusers")
    diffusers.AutoPipelineForText2Image = object()
    diffusers.AutoPipelineForImage2Image = object()
    quantization = ModuleType("torchao.quantization")
    quantization.quantize_ = MagicMock(
        side_effect=lambda *_args: events.append("quantize"),
    )
    config = object()
    quantization.Int8WeightOnlyConfig = MagicMock(return_value=config)
    torchao = ModuleType("torchao")
    torchao.quantization = quantization
    manifest = Manifest(
        id="m",
        name="M",
        capabilities=["text_to_image"],
        quantize="text_encoder_3:int8",
    )

    with patch.dict(
        sys.modules,
        {"diffusers": diffusers, "torchao": torchao, "torchao.quantization": quantization},
    ):
        loaded = engine._load(manifest, "t2i")

    assert loaded is pipeline
    quantization.quantize_.assert_called_once_with(component, config)
    engine._apply_rung.assert_called_once_with(pipeline, manifest, "full")
    assert events == ["quantize", "apply_rung"]
    engine._optimize_resident.assert_called_once_with(
        pipeline, "t2i", force_compile=True,
    )


def test_group_offload_uses_disk_only_for_unquantized_models(tmp_path):
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = MagicMock()
    engine.device = "cuda"
    engine.models_dir = str(tmp_path)

    quantized_pipeline = MagicMock()
    quantized_manifest = Manifest(
        id="quantized",
        name="Quantized",
        capabilities=["text_to_image"],
        quantize="text_encoder_3:int8",
    )
    engine._apply_rung(quantized_pipeline, quantized_manifest, "group_offload")

    quantized_kwargs = quantized_pipeline.enable_group_offload.call_args.kwargs
    assert quantized_kwargs["use_stream"] is False
    assert quantized_kwargs["offload_to_disk_path"] is None

    pipeline = MagicMock()
    manifest = Manifest(
        id="unquantized",
        name="Unquantized",
        capabilities=["text_to_image"],
    )
    engine._apply_rung(pipeline, manifest, "group_offload")

    kwargs = pipeline.enable_group_offload.call_args.kwargs
    # Streaming stays off for every model on this rung: diffusers' lazy
    # prefetch skips leaves and the run decodes to solid black. Only the
    # disk offload path is conditional on quantization.
    assert kwargs["use_stream"] is False
    assert kwargs["offload_to_disk_path"] == str(
        tmp_path / ".offload" / "unquantized"
    )


def test_preview_decoder_is_realtime_only_and_takes_unscaled_latents():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    inference_state = {"active": False, "entries": 0}

    class InferenceContext:
        def __enter__(self):
            assert inference_state["active"] is False
            inference_state["active"] = True
            inference_state["entries"] += 1

        def __exit__(self, exc_type, exc_value, traceback):
            inference_state["active"] = False

    engine.torch = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
        inference_mode=InferenceContext,
    )
    engine.device = "cpu"
    dtype = object()
    engine.dtype = dtype
    engine._pipelines = {}
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    engine._prompt_kwargs = MagicMock(return_value={"prompt": "frame"})

    latents = _FakeTensor((1, 4, 64, 64), marker="pipeline-latents")
    decoded = object()
    preview_image = Image.new("RGB", (32, 32), (12, 34, 56))
    stored_image = Image.new("RGB", (32, 32), (78, 90, 123))
    stored_image.putpixel((1, 0), (79, 90, 123))

    def run_pipeline(**kwargs):
        if kwargs.get("output_type") == "latent":
            return SimpleNamespace(images=latents)
        return SimpleNamespace(images=[stored_image])

    pipeline = MagicMock(side_effect=run_pipeline)
    pipeline.image_processor = SimpleNamespace(
        postprocess=MagicMock(return_value=[preview_image]),
    )
    engine._pipeline = MagicMock(return_value=pipeline)

    decoder = MagicMock()
    decoder.to.return_value = decoder
    decode_inference_states = []

    def decode_latents(received, *, return_dict):
        decode_inference_states.append(inference_state["active"])
        assert return_dict is False
        assert received.ndim == 4, (
            f"preview decoder requires 4D latents, got shape {received.shape}"
        )
        assert received.shape == (1, 4, 64, 64)
        return (decoded,)

    decoder.decode.side_effect = decode_latents
    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.return_value = decoder
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        preview_decoder="madebyollin/taesdxl",
    )
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")

    async def scenario():
        frame = await engine.frame(
            manifest, {"prompt": "frame"}, source.getvalue(),
        )
        stored = await engine.generate(
            manifest, {"prompt": "stored"}, lambda _fraction: None,
        )
        stored_i2i = await engine.generate(
            manifest,
            {"prompt": "stored edit"},
            lambda _fraction: None,
            input_image=source.getvalue(),
        )
        return frame, stored, stored_i2i

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    with (
        patch.dict(sys.modules, {"diffusers": diffusers}),
        patch("asyncio.to_thread", side_effect=run_inline),
    ):
        frame, stored, stored_i2i = asyncio.run(scenario())

    autoencoder_tiny.from_pretrained.assert_called_once_with(
        "madebyollin/taesdxl", torch_dtype=dtype,
    )
    decoder.to.assert_called_once_with("cpu")
    # Identity proves the tiny decoder receives the pipeline latents directly,
    # while the fake decoder enforces the real batched tensor shape.
    decoder.decode.assert_called_once_with(latents, return_dict=False)
    pipeline.image_processor.postprocess.assert_called_once_with(
        decoded, output_type="pil",
    )
    assert pipeline.call_args_list[0].kwargs["output_type"] == "latent"
    assert "output_type" not in pipeline.call_args_list[1].kwargs
    assert "output_type" not in pipeline.call_args_list[2].kwargs
    assert decoder.decode.call_count == 1
    assert inference_state == {"active": False, "entries": 1}
    assert decode_inference_states == [True]
    with Image.open(io.BytesIO(frame.data)) as opened:
        pixel = opened.resize((1, 1)).getpixel((0, 0))
        assert all(abs(actual - expected) <= 3 for actual, expected in zip(pixel, (12, 34, 56)))
    with Image.open(io.BytesIO(stored.data)) as opened:
        assert opened.getpixel((0, 0)) == (78, 90, 123)
    with Image.open(io.BytesIO(stored_i2i.data)) as opened:
        assert opened.getpixel((0, 0)) == (78, 90, 123)
    engine._evict_except.assert_not_called()
    engine._evict_poisoned.assert_not_called()


def _preview_decoder_frame_setup(*, t2i_adapter: str | None = None):
    engine = DiffusersEngine.__new__(DiffusersEngine)
    inference_state = {"active": False, "entries": 0}

    class InferenceContext:
        def __enter__(self):
            assert inference_state["active"] is False
            inference_state["active"] = True
            inference_state["entries"] += 1

        def __exit__(self, exc_type, exc_value, traceback):
            inference_state["active"] = False

    engine.torch = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
        inference_mode=InferenceContext,
    )
    engine.device = "cpu"
    dtype = object()
    engine.dtype = dtype
    engine._calibrated_slots = 3
    engine._prompt_kwargs = MagicMock(return_value={"prompt": "frame"})

    latents = _FakeTensor((1, 4, 64, 64), marker="pipeline-latents")
    decoded = object()
    preview_image = Image.new("RGB", (32, 32), (12, 34, 56))
    fallback_image = Image.new("RGB", (32, 32), (78, 90, 123))

    def run_pipeline(**kwargs):
        if kwargs.get("output_type") == "latent":
            return SimpleNamespace(images=latents)
        return SimpleNamespace(images=[fallback_image])

    pipeline = MagicMock(side_effect=run_pipeline)
    pipeline.image_processor = SimpleNamespace(
        postprocess=MagicMock(return_value=[preview_image]),
    )
    engine._pipeline = MagicMock(return_value=pipeline)

    decoder = MagicMock()
    decoder.to.return_value = decoder
    decode_inference_states = []

    def decode_latents(received, *, return_dict):
        decode_inference_states.append(inference_state["active"])
        assert return_dict is False
        assert received.ndim == 4
        assert received.shape == (1, 4, 64, 64)
        return (decoded,)

    decoder.decode.side_effect = decode_latents
    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.return_value = decoder
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    manifest_kwargs = {
        "id": "vega-rt",
        "name": "VegaRT",
        "capabilities": ["text_to_image", "image_to_image", "realtime"],
        "preview_decoder": "madebyollin/taesdxl",
    }
    if t2i_adapter is not None:
        manifest_kwargs["t2i_adapter"] = t2i_adapter
    manifest = Manifest(**manifest_kwargs)
    return SimpleNamespace(
        engine=engine,
        manifest=manifest,
        pipeline=pipeline,
        decoder=decoder,
        latents=latents,
        decoded=decoded,
        preview_image=preview_image,
        fallback_image=fallback_image,
        autoencoder_tiny=autoencoder_tiny,
        diffusers=diffusers,
        dtype=dtype,
        inference_state=inference_state,
        decode_inference_states=decode_inference_states,
    )


def test_adapter_frame_uses_preview_decoder():
    """Realtime models declare t2i_adapter, so the preview decoder must run on
    that branch or it is never used at all."""
    setup = _preview_decoder_frame_setup(t2i_adapter="org/vega-sketch")

    with patch.dict(sys.modules, {"diffusers": setup.diffusers}):
        image, _ = setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )

    setup.engine._pipeline.assert_called_once_with(
        setup.manifest, "realtime", allow_demotion=False,
    )
    setup.decoder.decode.assert_called_once_with(setup.latents, return_dict=False)
    setup.pipeline.image_processor.postprocess.assert_called_once_with(
        setup.decoded, output_type="pil",
    )
    assert setup.pipeline.call_args_list[0].kwargs["output_type"] == "latent"
    assert setup.pipeline.call_count == 1
    assert image is setup.preview_image
    assert setup.inference_state == {"active": False, "entries": 1}
    assert setup.decode_inference_states == [True]


def test_adapter_second_frame_after_decode_failure_makes_one_full_vae_call():
    """After a decode failure the retry window keeps the fast path off until it
    expires: the failing frame pays for latent denoise plus full VAE, and every
    later frame in that window must not."""
    setup = _preview_decoder_frame_setup(t2i_adapter="org/vega-sketch")
    setup.decoder.decode.side_effect = RuntimeError("decode broke")

    with patch.dict(sys.modules, {"diffusers": setup.diffusers}):
        setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )
        setup.pipeline.reset_mock()
        setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )

    assert setup.pipeline.call_count == 1
    assert "output_type" not in setup.pipeline.call_args.kwargs


def test_adapter_frame_preview_decode_failure_falls_back():
    import worker.engine as engine_module

    setup = _preview_decoder_frame_setup(t2i_adapter="org/vega-sketch")
    setup.decoder.decode.side_effect = RuntimeError("decode broke")

    with patch.dict(sys.modules, {"diffusers": setup.diffusers}):
        image, _ = setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )

    assert image is setup.fallback_image
    assert setup.pipeline.call_count == 2
    assert setup.pipeline.call_args_list[0].kwargs["output_type"] == "latent"
    assert "output_type" not in setup.pipeline.call_args_list[1].kwargs
    assert setup.engine._calibrated_slots is None
    assert engine_module._PREVIEW_DECODER_ATTR not in vars(setup.pipeline)
    assert engine_module._PREVIEW_DECODER_RETRY_ATTR in vars(setup.pipeline)


def test_i2i_frame_uses_preview_decoder():
    setup = _preview_decoder_frame_setup()

    with patch.dict(sys.modules, {"diffusers": setup.diffusers}):
        image, _ = setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )

    setup.engine._pipeline.assert_called_once_with(
        setup.manifest, "i2i", allow_demotion=False,
    )
    setup.decoder.decode.assert_called_once_with(setup.latents, return_dict=False)
    setup.pipeline.image_processor.postprocess.assert_called_once_with(
        setup.decoded, output_type="pil",
    )
    assert setup.pipeline.call_args_list[0].kwargs["output_type"] == "latent"
    assert setup.pipeline.call_count == 1
    assert image is setup.preview_image


def test_preview_decoder_load_failure_falls_back_once_per_pipeline():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
    )
    engine.device = "cpu"
    engine.dtype = object()
    engine._calibrated_slots = 3
    engine._prompt_kwargs = MagicMock(return_value={"prompt": "frame"})
    rendered = Image.new("RGB", (32, 32), (12, 34, 56))
    pipeline = MagicMock(return_value=SimpleNamespace(images=[rendered]))
    engine._pipeline = MagicMock(return_value=pipeline)

    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.side_effect = OSError("weights unavailable")
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["image_to_image", "realtime"],
        preview_decoder="missing/preview-vae",
    )

    with patch.dict(sys.modules, {"diffusers": diffusers}):
        first, _ = engine._frame(manifest, {}, Image.new("RGB", (512, 512)), 0.7)
        assert engine._calibrated_slots is None
        # A later full-VAE calibration is valid and must survive cached fallback.
        engine._calibrated_slots = 1
        second, _ = engine._frame(manifest, {}, Image.new("RGB", (512, 512)), 0.7)

    assert first is rendered
    assert second is rendered
    assert engine._calibrated_slots == 1
    assert autoencoder_tiny.from_pretrained.call_count == 1
    assert all("output_type" not in call.kwargs for call in pipeline.call_args_list)


def test_preview_decoder_failure_falls_back_and_retries_after_cooldown():
    """No decoder failure may stop a frame rendering.

    frame() cannot recover an out-of-memory decoder load by evicting: it evicts
    other models, and a realtime worker usually holds only this one. So the load
    falls back to the full VAE for this frame and retries after a pause, rather
    than propagating or disabling the fast path for the pipeline's lifetime.
    """
    import worker.engine as engine_module

    for failure in (
        _fake_oom()("no room"),
        OSError("registry unreachable"),
        RuntimeError("HIP error: out of memory"),
    ):
        engine = DiffusersEngine.__new__(DiffusersEngine)
        engine.torch = SimpleNamespace(OutOfMemoryError=type(failure))
        engine.device = "cpu"
        engine.dtype = object()
        engine._calibrated_slots = 3
        pipeline = SimpleNamespace()
        decoder = MagicMock()
        decoder.to.return_value = decoder
        autoencoder_tiny = MagicMock()
        autoencoder_tiny.from_pretrained.side_effect = [failure, decoder]
        diffusers = ModuleType("diffusers")
        diffusers.AutoencoderTiny = autoencoder_tiny
        manifest = Manifest(
            id="vega-rt",
            name="VegaRT",
            capabilities=["image_to_image", "realtime"],
            preview_decoder="madebyollin/taesdxl",
        )

        with patch.dict(sys.modules, {"diffusers": diffusers}):
            assert engine._preview_decoder(pipeline, manifest) is None, failure
            # Capacity measured with the fast decoder no longer describes this path.
            assert engine._calibrated_slots is None
            # Within the cooldown it does not hammer the loader every frame.
            assert engine._preview_decoder(pipeline, manifest) is None
            assert autoencoder_tiny.from_pretrained.call_count == 1
            # After the cooldown the failure is treated as transient.
            vars(pipeline)[engine_module._PREVIEW_DECODER_RETRY_ATTR] = 0.0
            assert engine._preview_decoder(pipeline, manifest) is decoder
            assert autoencoder_tiny.from_pretrained.call_count == 2


def test_preview_decoder_loads_once_across_frames():
    """Constraint: load once, not per frame. A mutant that drops the cache passes
    a single-frame test, so this one runs two."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(OutOfMemoryError=_fake_oom())
    engine.device = "cpu"
    engine.dtype = object()
    engine._calibrated_slots = 3
    pipeline = SimpleNamespace()
    decoder = MagicMock()
    decoder.to.return_value = decoder
    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.return_value = decoder
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["image_to_image", "realtime"],
        preview_decoder="madebyollin/taesdxl",
    )

    with patch.dict(sys.modules, {"diffusers": diffusers}):
        first = engine._preview_decoder(pipeline, manifest)
        second = engine._preview_decoder(pipeline, manifest)

    assert first is decoder and second is decoder
    assert autoencoder_tiny.from_pretrained.call_count == 1


def test_preview_decoder_does_not_outlive_its_pipeline():
    """Constraint: the decoder must not survive eviction. A mutant that stores it
    on the engine instead of the pipeline passes if the test plants the attribute
    itself, so this one loads it through the real path first."""
    import worker.engine as engine_module

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(OutOfMemoryError=_fake_oom())
    engine.device = "cpu"
    engine.dtype = object()
    engine._calibrated_slots = 3
    pipeline = SimpleNamespace()
    decoder = MagicMock()
    decoder.to.return_value = decoder
    autoencoder_tiny = MagicMock()
    autoencoder_tiny.from_pretrained.return_value = decoder
    diffusers = ModuleType("diffusers")
    diffusers.AutoencoderTiny = autoencoder_tiny
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["image_to_image", "realtime"],
        preview_decoder="madebyollin/taesdxl",
    )

    with patch.dict(sys.modules, {"diffusers": diffusers}):
        assert engine._preview_decoder(pipeline, manifest) is decoder
    assert engine_module._PREVIEW_DECODER_ATTR in vars(pipeline)

    engine._detach_preview_decoder(pipeline)
    assert engine_module._PREVIEW_DECODER_ATTR not in vars(pipeline)

    # And nothing kept a second reference that would resurrect it.
    with patch.dict(sys.modules, {"diffusers": diffusers}):
        engine._preview_decoder(pipeline, manifest)
    assert autoencoder_tiny.from_pretrained.call_count == 2


def test_cached_taesdxl_decodes_fixed_latents_on_cpu():
    pytest = __import__("pytest")
    torch = pytest.importorskip("torch")
    diffusers = pytest.importorskip("diffusers")
    huggingface_hub = pytest.importorskip("huggingface_hub")

    model_id = "madebyollin/taesdxl"
    cached_config = huggingface_hub.try_to_load_from_cache(model_id, "config.json")
    if not isinstance(cached_config, str):
        pytest.skip("madebyollin/taesdxl is not cached")
    snapshot = Path(cached_config).parent
    weight_names = (
        "diffusion_pytorch_model.safetensors",
        "diffusion_pytorch_model.bin",
    )
    if not any((snapshot / name).is_file() for name in weight_names):
        pytest.skip("madebyollin/taesdxl weights are not cached")

    decoder = diffusers.AutoencoderTiny.from_pretrained(
        snapshot, torch_dtype=torch.float32, local_files_only=True,
    ).to("cpu")
    latents = torch.linspace(
        -0.5, 0.5, steps=4 * 16 * 16, dtype=torch.float32, device="cpu",
    ).reshape(1, 4, 16, 16)

    with torch.inference_mode():
        decoded = decoder.decode(latents, return_dict=False)[0]

    assert decoded.shape == (1, 3, 128, 128)
    assert decoded.device.type == "cpu"
    assert decoded.dtype == torch.float32
    assert torch.isfinite(decoded).all().item()
    assert decoded.amin().item() >= -1.1
    assert decoded.amax().item() <= 1.1
    assert decoded.std().item() > 0.01


def test_frame_keeps_pillow_work_outside_gpu_lock():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    torch_stub = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
    )
    engine.torch = torch_stub
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
    )
    rendered = Image.new("RGB", (32, 32), (12, 34, 56))
    expected = encode_webp(rendered)
    events: list[tuple[str, bool]] = []

    def run_pipeline(**kwargs):
        events.append(("diffusion", engine._gpu.locked()))
        assert kwargs["image"].size == (512, 512)
        assert "output_type" not in kwargs
        return SimpleNamespace(images=[rendered])

    pipeline = MagicMock(side_effect=run_pipeline)

    def load_pipeline(*_args, **_kwargs):
        events.append(("pipeline", engine._gpu.locked()))
        return pipeline

    def prompt_kwargs(*_args, **_kwargs):
        events.append(("prompt", engine._gpu.locked()))
        return {"prompt": "frame"}

    engine._pipeline = MagicMock(side_effect=load_pipeline)
    engine._prompt_kwargs = MagicMock(side_effect=prompt_kwargs)
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")
    open_image = Image.open

    def track_open(*args, **kwargs):
        events.append(("decode", engine._gpu.locked()))
        return open_image(*args, **kwargs)

    def track_encode(image):
        events.append(("encode", engine._gpu.locked()))
        return encode_webp(image)

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    with (
        patch("asyncio.to_thread", side_effect=run_inline) as run,
        patch("worker.engine.Image.open", side_effect=track_open),
        patch("worker.engine.encode_webp", side_effect=track_encode),
    ):
        result = asyncio.run(engine.frame(manifest, {"prompt": "frame"}, source.getvalue()))

    assert events == [
        ("decode", False),
        ("pipeline", True),
        ("prompt", True),
        ("diffusion", True),
        ("encode", False),
    ]
    assert result.data == expected
    assert isinstance(result.gpu_ms, int)
    assert result.gpu_ms >= 0
    assert run.call_count == 3
    engine._evict_except.assert_not_called()
    engine._evict_poisoned.assert_not_called()


def test_frame_refuses_if_the_rung_drops_while_waiting_for_the_gpu():
    """Residency is rechecked after `_gpu` is held. Another task can demote
    the model while we wait; `_frame` must not then load an offload
    pipeline (issue #270).
    """
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(OutOfMemoryError=_fake_oom())
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    engine._frame = MagicMock()

    def pick(_manifest):
        return "model_offload" if engine._gpu.locked() else "full"

    engine._pick_rung = pick
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
    )
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    pytest = __import__("pytest")
    with patch("asyncio.to_thread", side_effect=run_inline):
        with pytest.raises(NotResidentError):
            asyncio.run(engine.frame(
                manifest, {"prompt": "frame"}, source.getvalue()))

    engine._frame.assert_not_called()


def test_realtime_frame_load_oom_refuses_without_demoting():
    """A realtime load must not demote to offload and then render. The
    session refuses instead (issue #270).
    """
    oom_type = _fake_oom()
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(OutOfMemoryError=oom_type)
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine.memory_mode = "auto"
    engine._rungs = {"vega-rt": "full"}
    engine._pipelines = {}
    engine._last_used = {}
    engine._ensure_vram = MagicMock()
    engine._load = MagicMock(side_effect=oom_type("oom"))
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    engine._evict_cold = MagicMock()
    engine._evict_model = MagicMock()
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
    )
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    pytest = __import__("pytest")
    with patch("asyncio.to_thread", side_effect=run_inline):
        with pytest.raises(NotResidentError):
            asyncio.run(engine.frame(
                manifest, {"prompt": "frame"}, source.getvalue()))

    assert engine._rungs["vega-rt"] == "full"
    engine._evict_except.assert_called_once()
    engine._evict_model.assert_not_called()


def test_frame_bounds_codec_concurrency():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
    )
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    rendered = Image.new("RGB", (32, 32), (12, 34, 56))
    engine._frame = MagicMock(return_value=(rendered, 17))
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
    )
    call_count = CODEC_CONCURRENCY_LIMIT + 1
    first_wave_full = asyncio.Event()
    release_first_wave = asyncio.Event()
    second_wave_full = asyncio.Event()
    release_second_wave = asyncio.Event()
    active = 0
    maximum = 0
    started = 0
    kinds_seen: set[str] = set()

    async def fake_to_thread(function, *args, **kwargs):
        nonlocal active, maximum, started
        name = getattr(function, "__name__", "")
        if name == "prepare_canvas":
            kind = "decode"
            result = Image.new("RGB", (REALTIME_SIZE, REALTIME_SIZE))
        elif function is encode_webp:
            kind = "encode"
            result = b"webp"
        else:
            return function(*args, **kwargs)

        started += 1
        wave = 1 if started <= CODEC_CONCURRENCY_LIMIT else 2
        active += 1
        maximum = max(maximum, active)
        kinds_seen.add(kind)
        if wave == 1 and active == CODEC_CONCURRENCY_LIMIT:
            first_wave_full.set()
        if wave == 2 and active == CODEC_CONCURRENCY_LIMIT:
            second_wave_full.set()
        try:
            if wave == 1:
                await release_first_wave.wait()
            else:
                await release_second_wave.wait()
            return result
        finally:
            active -= 1

    async def run_frames():
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            tasks = [
                asyncio.create_task(engine.frame(manifest, {}, b"png"))
                for _ in range(call_count)
            ]
            await first_wave_full.wait()
            assert active == CODEC_CONCURRENCY_LIMIT
            assert not any(task.done() for task in tasks)
            release_first_wave.set()
            await second_wave_full.wait()
            assert active == CODEC_CONCURRENCY_LIMIT
            release_second_wave.set()
            return await asyncio.gather(*tasks)

    results = asyncio.run(run_frames())

    assert maximum == CODEC_CONCURRENCY_LIMIT
    assert kinds_seen == {"decode", "encode"}
    assert len(results) == call_count
    assert all(result.data == b"webp" for result in results)


def test_frame_oom_retries_once_without_decoding_twice():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    torch_stub = SimpleNamespace(
        OutOfMemoryError=_fake_oom(),
    )
    engine.torch = torch_stub
    engine._gpu = asyncio.Lock()
    engine._codec = asyncio.Semaphore(CODEC_CONCURRENCY_LIMIT)
    engine._pick_rung = MagicMock(return_value="full")
    engine._evict_except = MagicMock()
    engine._evict_poisoned = MagicMock()
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
    )
    rendered = Image.new("RGB", (32, 32), (12, 34, 56))
    canvases: list[Image.Image] = []

    def run_frame(_manifest, _params, canvas, _strength, **_kwargs):
        canvases.append(canvas)
        if len(canvases) == 1:
            raise torch_stub.OutOfMemoryError
        return rendered, 17

    engine._frame = MagicMock(side_effect=run_frame)
    source = io.BytesIO()
    Image.new("RGB", (24, 16), (1, 2, 3)).save(source, "PNG")
    open_image = Image.open

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    with (
        patch("asyncio.to_thread", side_effect=run_inline) as run,
        patch("worker.engine.Image.open", wraps=open_image) as decode,
    ):
        result = asyncio.run(engine.frame(manifest, {}, source.getvalue()))

    assert decode.call_count == 1
    assert run.call_count == 4
    assert engine._frame.call_count == 2
    assert canvases[0] is canvases[1]
    engine._evict_except.assert_called_once_with(manifest.id)
    engine._evict_poisoned.assert_not_called()
    assert result.data == encode_webp(rendered)


def test_calibrate_realtime_sets_slots_from_p95():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(
        return_value=(Image.new("RGB", (32, 32)), 200),
    )

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    # Discarded pass + CALIBRATION_SAMPLES at 200 ms -> 2 slots under 500 ms.
    times = iter([0.0, 0.2] * (CALIBRATION_SAMPLES + 1))

    def fake_monotonic():
        return next(times)

    with (
        patch("worker.engine.time.monotonic", side_effect=fake_monotonic),
        patch("worker.engine.encode_webp") as encode,
    ):
        slots = engine._calibrate_realtime(manifest, configured=4)

    assert slots == 2
    assert engine._calibrated_slots == 2
    assert engine._frame.call_count == CALIBRATION_SAMPLES + 1
    encode.assert_not_called()
    canvases = [call.args[2] for call in engine._frame.call_args_list]
    assert all(canvas is canvases[0] for canvas in canvases)
    assert all(call.args[3] == 0.7 for call in engine._frame.call_args_list)
    # The measured p95 is advertised per model: 200 ms samples -> 200.
    assert engine.realtime_p95_ms("vega-rt") == 200
    assert engine.realtime_p95_ms("sdxl-turbo") is None


def test_realtime_p95_ms_none_before_any_calibration():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    assert engine.realtime_p95_ms("vega-rt") is None


def test_observed_frames_below_threshold_keep_the_calibration_value():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {"vega-rt": 408}
    engine._observed_frame_ms = {}
    for _ in range(OBSERVED_FRAME_SAMPLES - 1):
        engine.observe_frame_ms("vega-rt", 600.0)
    assert engine.realtime_p95_ms("vega-rt") == 408


def test_observed_frames_supersede_calibration_with_nearest_rank_p95():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {"vega-rt": 408}
    engine._observed_frame_ms = {}
    for _ in range(OBSERVED_FRAME_SAMPLES - 1):
        engine.observe_frame_ms("vega-rt", 200.4)
    engine.observe_frame_ms("vega-rt", 900.0)  # one outlier
    # Nearest-rank p95 of 20 is the 19th ordered sample (200.4, rounded to
    # 200), not the calibration number and not the outlier.
    assert engine.realtime_p95_ms("vega-rt") == 200


def test_observed_frame_window_drops_the_oldest():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._observed_frame_ms = {}
    for _ in range(OBSERVED_FRAME_WINDOW):
        engine.observe_frame_ms("vega-rt", 900.0)
    for _ in range(OBSERVED_FRAME_WINDOW):
        engine.observe_frame_ms("vega-rt", 300.0)
    # The bounded window dropped the slow early session entirely: unbounded,
    # the p95 of 240 samples would sit in the 900 ms tail.
    assert engine.realtime_p95_ms("vega-rt") == 300


def test_observed_frames_report_a_value_without_calibration():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._observed_frame_ms = {}
    for _ in range(OBSERVED_FRAME_SAMPLES):
        engine.observe_frame_ms("sdxl-turbo", 250.0)
    assert engine.realtime_p95_ms("sdxl-turbo") == 250


def test_p95_model_ids_covers_observed_and_calibrated_and_never_none():
    # The headline case the feature exists for is a model with observed
    # frames and no calibration entry: it must be advertised, and a test-local
    # p95_model_ids cannot prove the real method does. The union is what
    # matters: enough observations supersede calibration, a calibration entry
    # advertises until observations catch up, and nothing whose realtime_p95
    # is None is ever named.
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    for _ in range(OBSERVED_FRAME_SAMPLES):
        engine.observe_frame_ms("vega-rt", 200.0)
    engine._realtime_p95_ms["sdxl-turbo"] = 333
    engine.observe_frame_ms("sd-turbo", 400.0)  # too few to supersede

    ids = engine.p95_model_ids()

    assert "vega-rt" in ids  # observed enough frames, no calibration entry
    assert engine.realtime_p95_ms("vega-rt") == 200
    assert "sdxl-turbo" in ids  # calibration entry, no observations
    assert engine.realtime_p95_ms("sdxl-turbo") == 333
    assert "sd-turbo" not in ids  # no calibration entry, too few observations
    assert all(engine.realtime_p95_ms(model_id) is not None for model_id in ids)


def test_simulated_engine_ignores_frame_observations():
    engine = SimulatedEngine(0.01)
    engine.observe_frame_ms("sd-sim", 123.0)
    engine.observe_frame_ms("sd-sim", 456.0)
    assert engine.realtime_p95_ms("sd-sim") is None


def test_calibrate_realtime_p95_tolerates_one_outlier():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(return_value=b"webp")

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    # Discarded pass, then 19x 200 ms and one 900 ms outlier. Nearest-rank
    # p95 of 20 is the 19th ordered value (200 ms), so slots stay 2.
    pairs = [(0.0, 0.2)]  # discarded
    pairs.extend([(0.0, 0.2)] * (CALIBRATION_SAMPLES - 1))
    pairs.append((0.0, 0.9))
    flat: list[float] = []
    for start, end in pairs:
        flat.extend([start, end])
    times = iter(flat)

    with patch("worker.engine.time.monotonic", side_effect=lambda: next(times)):
        slots = engine._calibrate_realtime(manifest, configured=4)

    assert slots == 2
    assert engine._calibrated_slots == 2


def test_calibrate_realtime_failure_advertises_zero_slots():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(side_effect=RuntimeError("hip boom"))

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    async def scenario():
        return await engine.calibrate_realtime(manifest, 4)

    slots = asyncio.run(scenario())
    assert slots == 0
    assert engine._calibrated_slots == 0


def test_calibrate_realtime_skips_cpu_without_frames():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cpu"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibrated_slots = None
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(return_value=b"webp")

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )

    slots = engine._calibrate_realtime(manifest, configured=4)

    assert slots == 0
    assert engine._calibrated_slots == 0
    assert engine.realtime_p95_ms("vega-rt") is None
    engine._frame.assert_not_called()
    engine._select_rung.assert_not_called()


def test_evict_cold_removes_oldest_first():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._pipelines = {("a", "t2i"): object(), ("b", "t2i"): object()}
    engine._rungs = {"a": "full", "b": "full"}
    engine._last_used = {"a": 20.0, "b": 10.0}
    engine._free_gpu_cache = MagicMock()
    engine._free_vram_bytes = MagicMock(return_value=0)

    engine._evict_cold(except_model_id="a")

    assert ("a", "t2i") in engine._pipelines
    assert ("b", "t2i") not in engine._pipelines
    assert "b" not in engine._rungs


def test_evicting_the_last_pipeline_forgets_the_cached_rung():
    """A cached rung must not outlive its conditions: with the model evicted,
    the next load decides against the VRAM that exists then, not the VRAM
    that existed when the rung was chosen (issue #270)."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._pipelines = {("m", "t2i"): object()}
    engine._rungs = {"m": "full"}
    engine._last_used = {"m": 1.0}
    engine._free_gpu_cache = MagicMock()

    engine._evict_model("m")

    assert engine._pipelines == {}
    assert engine.model_rung("m") is None


def test_a_cached_rung_survives_while_another_pipeline_entry_is_resident():
    """The realtime and t2i entries share every weight, so removing one of
    them leaves the model resident and the rung it was loaded at is still
    the truth; only the removal of the last entry may forget it."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._pipelines = {("m", "t2i"): object(), ("m", "realtime"): object()}
    engine._rungs = {"m": "full"}
    engine._last_used = {"m": 1.0}
    engine._free_gpu_cache = MagicMock()

    engine._drop_pipeline(("m", "t2i"))
    assert engine.model_rung("m") == "full"
    engine._drop_pipeline(("m", "realtime"))
    assert engine.model_rung("m") is None


def test_ensure_realtime_resident_returns_true_without_evicting_when_full():
    """The common path must stay cheap: a full-resident model is answered
    from the cached rung and the load path, which is what evicts, runs."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._rungs = {"vega-rt": "full"}
    resident = object()
    engine._pipelines = {("vega-rt", "t2i"): resident}
    engine.load_model = MagicMock()
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=8,
    )

    result = asyncio.run(engine.ensure_realtime_resident(manifest))

    assert result is True
    engine.load_model.assert_not_called()
    assert engine._pipelines[("vega-rt", "t2i")] is resident
    assert engine.model_rung("vega-rt") == "full"


def test_ensure_realtime_resident_reloads_full_when_the_rung_was_cached_offload():
    """A queued job on another model can consume VRAM between sessions and
    leave a stale "model_offload" cached answer behind (issue #270). Resident
    means deciding against the VRAM that exists once that job is evicted,
    which is the load path, not re-reading the stale cache."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    torch_stub = MagicMock()
    torch_stub.OutOfMemoryError = type("OutOfMemoryError", (Exception,), {})
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine.models_dir = ""
    engine.torch_compile = False
    engine.attention_backend = ""
    engine._pipelines = {("other", "t2i"): object()}
    engine._rungs = {"vega-rt": "model_offload", "other": "full"}
    engine._last_used = {}
    engine._free_gpu_cache = MagicMock()
    engine._gpu = asyncio.Lock()
    engine._from_pretrained = MagicMock(return_value=MagicMock())

    def free_vram():
        # Before the load path evicts the job, the card cannot hold full
        # residency for the session's model; afterwards it can.
        return 1 if ("other", "t2i") in engine._pipelines else 64 * 1024**3

    engine._free_vram_bytes = MagicMock(side_effect=free_vram)

    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=8,
    )
    diffusers = ModuleType("diffusers")
    diffusers.AutoPipelineForText2Image = object()
    diffusers.AutoPipelineForImage2Image = object()

    async def scenario():
        with patch.dict(sys.modules, {"diffusers": diffusers}):
            return await engine.ensure_realtime_resident(manifest)

    assert asyncio.run(scenario()) is True
    assert engine.model_rung("vega-rt") == "full"
    assert ("other", "t2i") not in engine._pipelines
    assert ("vega-rt", "t2i") in engine._pipelines


def test_ensure_realtime_resident_leaves_a_pinned_mode_alone():
    """A pinned memory mode is an operator choice; the load path, which
    evicts everyone, is not entered for it. The session gets the per-frame
    fallback instead and nothing resident is dropped."""
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.memory_mode = "model_offload"
    engine._rungs = {"vega-rt": "model_offload"}
    resident = object()
    engine._pipelines = {("vega-rt", "t2i"): resident}
    engine.load_model = MagicMock()
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=8,
    )

    result = asyncio.run(engine.ensure_realtime_resident(manifest))

    assert result is False
    engine.load_model.assert_not_called()
    assert engine._pipelines[("vega-rt", "t2i")] is resident


def test_evict_detaches_preview_decoders():
    # Keyed by the constant, not the literal, so this asserts the behaviour
    # rather than a spelling: whatever attribute the engine actually stores the
    # decoder under is the one eviction has to remove. With the literal, renaming
    # the constant in production would fail this test for the wrong reason.
    import worker.engine as engine_module

    attr = engine_module._PREVIEW_DECODER_ATTR
    first = SimpleNamespace(**{attr: object()})
    second = SimpleNamespace(**{attr: object()})
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._pipelines = {("a", "i2i"): first, ("b", "i2i"): second}
    engine._rungs = {"a": "full", "b": "full"}
    engine._last_used = {"a": 1.0, "b": 2.0}
    engine._free_gpu_cache = MagicMock()

    engine._evict_model("a")

    assert not hasattr(first, attr)
    assert hasattr(second, attr)

    engine._evict_all()

    assert not hasattr(second, attr)
    assert engine._pipelines == {}


def _load_oom_engine(failing_rungs: set[str]) -> tuple[DiffusersEngine, list[str]]:
    torch_stub = MagicMock()
    torch_stub.OutOfMemoryError = _fake_oom()

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cuda"
    engine.memory_mode = "auto"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._free_gpu_cache = MagicMock()
    engine._gpu = asyncio.Lock()
    engine._free_vram_bytes = MagicMock(return_value=64 * 1024**3)
    engine._select_rung = MagicMock(return_value="full")
    attempts: list[str] = []

    def load(manifest, _mode):
        rung = engine._pick_rung(manifest)
        attempts.append(rung)
        if rung in failing_rungs:
            raise torch_stub.OutOfMemoryError
        return object()

    engine._load = load
    return engine, attempts


def test_load_oom_demotes_full_to_model_offload():
    engine, attempts = _load_oom_engine({"full"})
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    engine._load_model(manifest)

    assert attempts == ["full", "model_offload"]
    assert engine.model_rung("m") == "model_offload"


def test_load_second_oom_demotes_model_to_group_offload():
    engine, attempts = _load_oom_engine({"full", "model_offload"})
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    engine._load_model(manifest)

    assert attempts == ["full", "model_offload", "group_offload"]
    assert engine.model_rung("m") == "group_offload"


def test_load_oom_does_not_demote_pinned_rung():
    engine, attempts = _load_oom_engine(set())
    engine.memory_mode = "full"
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    def load(current_manifest, _mode):
        rung = engine._pick_rung(current_manifest)
        attempts.append(rung)
        if len(attempts) == 1:
            raise engine.torch.OutOfMemoryError
        return object()

    engine._load = load
    engine._load_model(manifest)

    assert attempts == ["full", "full"]
    assert engine.model_rung("m") == "full"


def test_final_load_oom_drops_failed_attempt_traceback():
    engine, attempts = _load_oom_engine({"full", "model_offload", "group_offload"})
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    try:
        engine._load_model(manifest)
    except engine.torch.OutOfMemoryError as error:
        frame_names = []
        traceback = error.__traceback__
        while traceback is not None:
            frame_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
    else:
        raise AssertionError("expected OutOfMemoryError")

    assert attempts == ["full", "model_offload", "group_offload", "group_offload"]
    assert "load" not in frame_names


def _poison_engine(model_id: str = "m") -> DiffusersEngine:
    from worker.engine import GeneratedImage

    # CI worker venv has no torch; only OutOfMemoryError must be a real type
    # so `except self.torch.OutOfMemoryError` stays valid.
    torch_stub = MagicMock()
    torch_stub.OutOfMemoryError = _fake_oom()

    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.torch = torch_stub
    engine.device = "cpu"
    engine.dtype = object()
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {(model_id, "t2i"): object()}
    engine._rungs = {model_id: "full"}
    engine._last_used = {model_id: 1.0}
    engine._poison_evicted_at = {}
    engine._poison_evict_count = {}
    engine._gpu = asyncio.Lock()
    engine._free_gpu_cache = MagicMock()
    engine._ok = GeneratedImage(b"webp", 64, 64, 1, 0)
    return engine


def test_evict_poisoned_drops_resident_and_counts():
    engine = _poison_engine()
    assert engine._evict_poisoned("m") is True
    assert engine._pipelines == {}
    assert engine._poison_evict_count["m"] == 1


def test_evict_poisoned_respects_cooldown():
    engine = _poison_engine()
    assert engine._evict_poisoned("m") is True
    engine._pipelines[("m", "t2i")] = object()  # simulate a reload that failed again
    assert engine._evict_poisoned("m") is False
    assert ("m", "t2i") in engine._pipelines
    assert engine._poison_evict_count["m"] == 1


def test_generate_fails_once_then_succeeds_after_poison_evict():
    """Non-OOM error drops the resident; the next job can complete (issue #103)."""
    engine = _poison_engine()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("Input type c10::Half and bias type float")

    async def scenario():
        with patch("asyncio.to_thread", side_effect=boom):
            try:
                await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
            except RuntimeError as error:
                assert "c10::Half" in str(error)
            else:
                raise AssertionError("expected RuntimeError")
        assert engine._pipelines == {}
        assert engine._poison_evict_count["m"] == 1
        # Reload would re-populate; simulate that and a clean second pass.
        engine._pipelines[("m", "t2i")] = object()
        with patch("asyncio.to_thread", return_value=engine._ok):
            result = await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
        assert result is engine._ok

    asyncio.run(scenario())


def test_generate_value_error_does_not_evict():
    engine = _poison_engine()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    def bad_request(*_args, **_kwargs):
        raise ValueError("bad params")

    async def scenario():
        with patch("asyncio.to_thread", side_effect=bad_request):
            try:
                await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError")

    asyncio.run(scenario())
    assert ("m", "t2i") in engine._pipelines
    assert engine._poison_evict_count == {}


def test_reject_degenerate_output_raises_on_a_flat_image():
    """A saturated denoise decodes to one colour; it must not be stored."""
    for colour in ((0, 0, 0), (255, 255, 255), (12, 34, 56)):
        try:
            reject_degenerate_output(Image.new("RGB", (32, 32), colour), "ssd-1b")
        except RuntimeError as error:
            assert "ssd-1b" in str(error)
            assert "flat colour" in str(error)
        else:
            raise AssertionError(f"expected RuntimeError for {colour}")


def test_reject_degenerate_output_passes_real_output():
    """One varying band is enough; VAE noise means real output is never flat."""
    image = Image.new("RGB", (32, 32), (10, 20, 30))
    image.putpixel((0, 0), (10, 20, 31))
    reject_degenerate_output(image, "ssd-1b")

    gradient = Image.linear_gradient("L")
    reject_degenerate_output(gradient, "ssd-1b")


def test_generate_rejects_a_flat_render_and_evicts_the_resident():
    """The guard fails the job and drops the pipeline that produced it."""
    engine = _poison_engine()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])
    flat = Image.new("RGB", (32, 32), (0, 0, 0))

    engine._pipeline = MagicMock(return_value=MagicMock(
        return_value=SimpleNamespace(images=[flat]),
    ))
    engine._prompt_kwargs = MagicMock(return_value={"prompt": "x"})

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def scenario():
        with patch("asyncio.to_thread", side_effect=run_inline):
            try:
                await engine.generate(manifest, {"prompt": "x", "steps": 1}, lambda _: None)
            except RuntimeError as error:
                assert "flat colour" in str(error)
            else:
                raise AssertionError("expected RuntimeError")

    asyncio.run(scenario())
    assert engine._pipelines == {}
    assert engine._poison_evict_count["m"] == 1


def test_generation_oom_demotes_once_and_retries_once():
    engine = _poison_engine()
    engine.memory_mode = "auto"
    engine._pipeline = MagicMock()
    manifest = Manifest(id="m", name="M", capabilities=["text_to_image"])

    async def scenario():
        async def fake_to_thread(function, *args, **kwargs):
            if function is engine._pipeline:
                return function(*args, **kwargs)
            raise engine.torch.OutOfMemoryError

        with patch("asyncio.to_thread", side_effect=fake_to_thread) as run:
            try:
                await engine.generate(manifest, {"prompt": "x"}, lambda _: None)
            except engine.torch.OutOfMemoryError:
                pass
            else:
                raise AssertionError("expected OutOfMemoryError")
        assert run.call_count == 4
        pipeline_call = run.call_args_list[2]
        assert pipeline_call.args == (engine._pipeline, manifest, "t2i")
        assert pipeline_call.kwargs == {"allow_demotion": False}

    asyncio.run(scenario())
    assert engine.model_rung("m") == "model_offload"
    engine._pipeline.assert_called_once_with(
        manifest, "t2i", allow_demotion=False,
    )


def test_gpu_lock_is_held_until_the_thread_finishes():
    """Cancelling mid-inference must not free the GPU for the next entrant.

    asyncio cancellation cannot stop a thread, so awaiting to_thread directly
    would unwind the `async with self._gpu` and release the lock while the
    thread is still on the device (issue #202).
    """
    import threading
    import time

    running, finished = threading.Event(), threading.Event()

    def native_work():
        running.set()
        time.sleep(0.3)
        finished.set()

    async def scenario():
        gpu = asyncio.Lock()
        engine = SimpleNamespace(_run_to_completion=DiffusersEngine._run_to_completion)

        async def held():
            async with gpu:
                await engine._run_to_completion(engine, native_work)

        task = asyncio.create_task(held())
        await asyncio.get_running_loop().run_in_executor(None, running.wait)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The lock may be free by now, but only because the thread is done.
        return gpu.locked(), finished.is_set()

    locked, thread_finished = asyncio.run(scenario())
    assert thread_finished, "lock was released while the thread was still running"
    assert not locked


def test_gpu_lock_survives_a_second_cancellation():
    """A cancel arriving while we wait for the thread must not free the lock.

    asyncio.wait is itself an await point, so a second cancellation there would
    skip the raise, unwind the `async with self._gpu`, and leak the lock exactly
    as the original defect did. Shutdown gathers can deliver one.
    """
    import threading
    import time

    running, finished = threading.Event(), threading.Event()

    def native_work():
        running.set()
        time.sleep(0.4)
        finished.set()

    async def scenario():
        gpu = asyncio.Lock()
        engine = SimpleNamespace(_run_to_completion=DiffusersEngine._run_to_completion)

        async def held():
            async with gpu:
                await engine._run_to_completion(engine, native_work)

        task = asyncio.create_task(held())
        await asyncio.get_running_loop().run_in_executor(None, running.wait)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()  # e.g. a second Ctrl-C during a shutdown gather
        try:
            await task
        except asyncio.CancelledError:
            pass
        return gpu.locked(), finished.is_set()

    locked, thread_finished = asyncio.run(scenario())
    assert thread_finished, "second cancellation released the lock mid-thread"
    assert not locked


def test_calibrated_slots_are_the_min_across_models():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._calibration_cap = 8
    engine._realtime_p95_ms = {"fast": 100, "slow": 400}
    engine._calibrated_slots = None
    # 100 ms would be 5 slots; 400 ms is 1. Last-write-wins advertised
    # whichever ran last. The honest scalar is the min.
    assert engine._recompute_calibrated_slots() == 1
    assert engine._calibrated_slots == 1


def test_calibrated_slots_use_the_slowest_batch_curve():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._calibration_cap = 8
    engine._realtime_p95_ms = {"fast": 139, "slow": 139}
    engine._realtime_batch_ms = {
        "fast": [139, 309, 404],
        "slow": [139, 309],
    }
    engine._calibrated_slots = None
    assert engine._recompute_calibrated_slots() == 2
    assert engine._calibrated_slots == 2


def test_calibrate_batch_curve_stops_at_the_bar():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine._calibration_cap = 4
    engine._realtime_p95_ms = {"vega-rt": 139}
    engine._realtime_batch_ms = {}
    engine._calibrated_slots = None
    engine._measure_batch_p95 = AsyncMock(side_effect=[309, 404, 501])
    manifest = _realtime_manifest()

    asyncio.run(engine._calibrate_batch_curve(manifest, configured=4))

    assert engine._realtime_batch_ms["vega-rt"] == [139, 309, 404]
    assert engine._calibrated_slots == 3


def test_measure_batch_p95_uses_one_prompt_cache_per_session():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.frame = AsyncMock()
    manifest = _realtime_manifest()

    asyncio.run(engine._measure_batch_p95(3, manifest, {"prompt": "x"}, b"canvas"))

    assert engine.frame.call_count == (CALIBRATION_SAMPLES + 1) * 3
    caches = [
        call.kwargs["prompt_cache"]
        for call in engine.frame.call_args_list
    ]
    assert len({id(cache) for cache in caches}) == 3


def test_calibrate_failure_does_not_zero_a_sibling():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibration_cap = 4
    engine._calibrated_slots = 2
    engine._realtime_p95_ms = {"ok": 200}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")
    engine._frame = MagicMock(side_effect=RuntimeError("hip boom"))

    boom = Manifest(
        id="boom",
        name="Boom",
        capabilities=["text_to_image", "realtime"],
        min_vram_gb=8,
    )
    slots = asyncio.run(engine.calibrate_realtime(boom, 4))
    assert slots == 0
    assert engine._realtime_p95_ms == {"ok": 200}
    assert engine._calibrated_slots == 2


def test_frame_gpu_ms_includes_work_before_the_pipeline_call():
    setup = _preview_decoder_frame_setup(t2i_adapter="org/vega-sketch")
    real_pipeline = setup.engine._pipeline

    def slow_pipeline(*args, **kwargs):
        time.sleep(0.05)
        return real_pipeline(*args, **kwargs)

    setup.engine._pipeline = slow_pipeline
    with patch.dict(sys.modules, {"diffusers": setup.diffusers}):
        _image, gpu_ms = setup.engine._frame(
            setup.manifest, {"prompt": "frame"}, Image.new("RGB", (512, 512)), 0.7,
        )
    assert gpu_ms >= 50


def test_calibrate_elapsed_includes_the_frame_call():
    engine = DiffusersEngine.__new__(DiffusersEngine)
    engine.device = "cuda"
    engine.memory_mode = "full"
    engine.models_dir = ""
    engine._pipelines = {}
    engine._rungs = {}
    engine._last_used = {}
    engine._calibration_cap = 4
    engine._calibrated_slots = None
    engine._realtime_p95_ms = {}
    engine._observed_frame_ms = {}
    engine._gpu = asyncio.Lock()
    engine._select_rung = MagicMock(return_value="full")

    def slow_frame(*_args, **_kwargs):
        time.sleep(0.05)
        return Image.new("RGB", (32, 32)), 1

    engine._frame = slow_frame
    manifest = Manifest(
        id="vega-rt",
        name="VegaRT",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=8,
    )
    slots = engine._calibrate_realtime(manifest, configured=4)
    assert engine.realtime_p95_ms("vega-rt") >= 50
    assert slots == slots_from_frame_ms(engine.realtime_p95_ms("vega-rt"), 4)


def test_a_simulated_upscale_stops_when_it_is_asked_to():
    """An upscale is one long resize here and a tile loop on the real engine.
    Both have to answer the same ask, or a cancelled upscale reports success."""
    from worker.engine import Cancelled

    pytest = __import__("pytest")
    engine = SimulatedEngine(0.01)
    manifest = Manifest(id="sim-upscale", name="Sim Upscale", capabilities=["upscale"])
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    source = buffer.getvalue()

    with pytest.raises(Cancelled):
        asyncio.run(engine.generate(manifest, {"factor": 2}, lambda _: None,
                                    input_image=source, cancelled=lambda: True))

    finished = asyncio.run(engine.generate(manifest, {"factor": 2}, lambda _: None,
                                           input_image=source, cancelled=lambda: False))
    assert (finished.width, finished.height) == (16, 16)
