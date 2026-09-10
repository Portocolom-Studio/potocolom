"""Forked Dream arms, and the Dream-only negative prompt (window 2).

Window 2 rests on two claims that have to hold by construction rather than by
inspection: the arms of one base differ only in the setting under test, and a
run with a single arm is the run this repository has already validated.

The adapter is faked so both phases run on CPU in milliseconds. Every random
draw goes through the passed generator, exactly as the real SDS sampler and
SDEdit calls do, so an arm that forgets to restore RNG state comes out
different and these tests fail.
"""

from types import SimpleNamespace
from typing import Any

import pytest

torch = pytest.importorskip("torch")

import worker.illusions as illusions  # noqa: E402
from worker.illusions import (  # noqa: E402
    NFSD_NEGATIVE_PROMPT,
    DiffusionAdapter,
    DreamArm,
    IllusionConfig,
    optimize_illusion,
)

ADAPTERS: list["FakeAdapter"] = []


class FakeAdapter:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.dtype = torch.float32
        self.dream_phase_calls = 0
        self.sds_calls = 0
        self.sds_kwargs: list[dict[str, Any]] = []
        self.dream_calls: list[tuple[Any, ...]] = []
        ADAPTERS.append(self)

    def begin_dream_phase(self) -> None:
        self.dream_phase_calls += 1

    def sds_loss_batch(
        self,
        derived: Any,
        prompts: list[str],
        weights: list[float],
        guidance_scale: float,
        generator: Any,
        **kwargs: Any,
    ) -> Any:
        self.sds_calls += 1
        self.sds_kwargs.append(kwargs)
        torch.randn(derived.shape, generator=generator)  # the real sampler's draw
        return derived.mean()

    def note_backward(self) -> None:
        pass

    def sdedit(
        self,
        image: Any,
        prompt: str,
        strength: float,
        generator: Any,
        *,
        negative_prompt: str | None = None,
    ) -> Any:
        self.dream_calls.append(("sdedit", prompt, strength, negative_prompt))
        return torch.rand(image.shape, generator=generator)

    def sdedit_joint(
        self,
        views: list[Any],
        prompts: list[str],
        strength: float,
        generator: Any,
        *,
        negative_prompt: str | None = None,
    ) -> list[Any]:
        self.dream_calls.append(("joint", tuple(prompts), strength, negative_prompt))
        target = torch.rand(views[0].shape, generator=generator)
        return [target, illusions.rot90(target, 2)]


def _tiny(monkeypatch: pytest.MonkeyPatch) -> IllusionConfig:
    ADAPTERS.clear()
    # 16px arrangements keep the real FFN and the real optimizer, at test speed.
    monkeypatch.setattr(illusions, "RESOLUTION", 16)
    monkeypatch.setattr(illusions, "DiffusionAdapter", FakeAdapter)
    return IllusionConfig(
        illusion="flip",
        prompts=["a dog", "a sloth"],
        device="cpu",
        dream_model_id=None,
        sds_steps=3,
        dream_rounds=1,
        dream_steps=2,
        seed=7,
    )


def test_single_arm_is_the_unforked_run(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _tiny(monkeypatch)
    plain = optimize_illusion(config)
    forked = optimize_illusion(config, arms=[DreamArm(name="only")])

    for before, after in zip(plain.derived, forked.derived, strict=True):
        assert torch.equal(before, after)
    for before, after in zip(plain.primes, forked.primes, strict=True):
        assert torch.equal(before, after)
    # An unforked run reports no arms, so existing callers see what they saw.
    assert plain.arm_results == {}
    assert set(forked.arm_results) == {"only"}


def test_arms_do_not_disturb_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _tiny(monkeypatch)
    independent = DreamArm(name="x", dream_joint=False)
    joint = DreamArm(name="y", dream_joint=True)

    both = optimize_illusion(config, arms=[independent, joint])
    alone = optimize_illusion(config, arms=[independent])

    paired_x = both.arm_results["x"].derived
    solo_x = alone.arm_results["x"].derived
    for paired, solo in zip(paired_x, solo_x, strict=True):
        assert torch.equal(paired, solo)
    # The arms must actually differ, or the equality above proves nothing.
    assert not torch.equal(both.arm_results["x"].derived[0], both.arm_results["y"].derived[0])
    # Each arm keeps its own round log rather than appending to a shared one.
    assert len(both.arm_results["y"].diagnostics["dream_rounds"]) == 1
    assert both.arm_results["y"].diagnostics["dream_arm"] == "y"


def test_sds_runs_once_for_every_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _tiny(monkeypatch)
    optimize_illusion(config, arms=[DreamArm(name="a"), DreamArm(name="b", dream_joint=True)])
    adapter = ADAPTERS[-1]

    assert adapter.sds_calls == config.sds_steps
    # The img2img swap is a transition, not per-arm state: repeating it would
    # reload the Dream checkpoint and invalidate its embedding cache.
    assert adapter.dream_phase_calls == 1
    # Arm a is independent (one SDEdit per view), arm b joint (one call).
    assert [call[0] for call in adapter.dream_calls] == ["sdedit", "sdedit", "joint"]


def test_negative_prompt_reaches_the_dream_phase_only(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _tiny(monkeypatch)
    config.negative_prompt = "watermark, frame"
    optimize_illusion(
        config,
        arms=[
            DreamArm(name="off"),
            DreamArm(name="on", negative_prompt=config.negative_prompt),
        ],
    )
    adapter = ADAPTERS[-1]

    negatives = [call[-1] for call in adapter.dream_calls]
    assert negatives == [None, None, "watermark, frame", "watermark, frame"]
    # SDS is untouched: replacing its unconditional branch would give the
    # negative term coefficient 5.9 against the positive 6.0.
    assert all("negative" not in key for kwargs in adapter.sds_kwargs for key in kwargs)


def test_arm_names_must_be_unique_and_present(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _tiny(monkeypatch)
    with pytest.raises(ValueError, match="unique"):
        optimize_illusion(config, arms=[DreamArm(name="a"), DreamArm(name="a")])
    with pytest.raises(ValueError, match="empty"):
        optimize_illusion(config, arms=[])


def test_sdedit_passes_the_negative_prompt_and_defaults_to_none() -> None:
    adapter = object.__new__(DiffusionAdapter)
    adapter.dtype = torch.float32
    adapter.dream_inference_steps = 4
    adapter.dream_guidance = 2.0
    seen: list[Any] = []

    def fake_img2img(**kwargs: Any) -> Any:
        seen.append(kwargs["negative_prompt"])
        return SimpleNamespace(images=torch.zeros(1, 3, 8, 8))

    adapter.img2img = fake_img2img
    image = torch.rand(1, 3, 8, 8)
    adapter.sdedit(image, "a dog", 0.95, None)
    adapter.sdedit(image, "a dog", 0.95, None, negative_prompt="watermark")
    # Unset is the pipeline's own default, so window 1's call is unchanged.
    assert seen == [None, "watermark"]


def test_dream_cfg_embedding_replaces_the_unconditional_row() -> None:
    adapter = object.__new__(DiffusionAdapter)
    positive = torch.stack([torch.full((4,), 1.0), torch.full((4,), 2.0)])
    negative = torch.stack([torch.full((4,), 3.0), torch.full((4,), 4.0)])
    adapter.dream_embeddings = {"a dog": (positive, None), "watermark": (negative, None)}

    plain, _ = adapter._dream_embed_cfg("a dog", None)
    assert torch.equal(plain, positive)

    negated, _ = adapter._dream_embed_cfg("a dog", "watermark")
    # [negative conditional, positive conditional]: standard sampling CFG.
    assert torch.equal(negated, torch.cat([negative[1:], positive[1:]]))


def test_nfsd_keeps_its_own_negative_prompt() -> None:
    adapter = object.__new__(DiffusionAdapter)
    adapter.device = "cpu"
    adapter.dtype = torch.float32
    adapter.sds_objective = "nfsd"
    adapter.sds_gradient_scale = 1.0
    asked: list[str] = []
    chunks: list[int] = []

    class FakeUNet:
        def __call__(self, model_in: Any, timesteps: Any, encoder_hidden_states: Any) -> Any:
            chunks.append(model_in.shape[0])
            return SimpleNamespace(sample=torch.zeros_like(model_in))

    class FakeScheduler:
        config = SimpleNamespace(num_train_timesteps=1000)
        alphas_cumprod = torch.linspace(0.99, 0.01, 1000)

        def add_noise(self, latents: Any, noise: Any, timesteps: Any) -> Any:
            return latents

    def fake_embed(prompt: str) -> tuple[Any, None]:
        asked.append(prompt)
        return torch.zeros(2, 4, 8), None

    adapter.pipe = SimpleNamespace(unet=FakeUNet())
    adapter.scheduler = FakeScheduler()
    adapter.embed = fake_embed
    adapter.encode_latent = lambda image, **_kwargs: torch.zeros(image.shape[0], 4, 2, 2)

    adapter.sds_loss_batch(
        torch.rand(1, 3, 16, 16),
        ["a dog"],
        [1.0],
        7.5,
        torch.Generator().manual_seed(0),
        objective="nfsd",
        shared_timestep=500,
        shared_noise=torch.zeros(1, 4, 2, 2),
    )
    # Still the published NFSD prompt on the third chunk, untouched by window 2.
    assert asked == ["a dog", NFSD_NEGATIVE_PROMPT]
    assert chunks == [3]
