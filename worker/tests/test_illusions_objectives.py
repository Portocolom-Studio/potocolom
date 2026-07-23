"""Unit tests for SDS objectives, LR flags, styles, microbatching, and NFSD."""

from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from worker.illusions import (  # noqa: E402
    NFSD_NEGATIVE_PROMPT,
    STYLE_TEMPLATES,
    DiffusionAdapter,
    IllusionConfig,
    apply_style_template,
    build_arg_parser,
    compute_sds_gradient,
    config_from_args,
    gradient_conflict_stats,
    hifa_timestep_fraction,
    nfsd_delta_d,
    sds_timestep_weight,
    warn_low_clip_margins,
)


def test_compute_sds_gradient_legacy_matches_cfg_minus_noise() -> None:
    noise = torch.randn(2, 4, 8, 8)
    uncond = torch.randn(2, 4, 8, 8)
    cond = torch.randn(2, 4, 8, 8)
    guidance = 100.0
    weight = torch.tensor(0.7)
    guided = uncond + guidance * (cond - uncond)
    expected = (guided - noise).float()
    got = compute_sds_gradient(
        objective="legacy",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=guidance,
        weight_t=weight,
    )
    assert torch.allclose(got, expected)


def test_compute_sds_gradient_weighted_applies_w_t() -> None:
    noise = torch.ones(1, 1, 2, 2)
    uncond = torch.zeros(1, 1, 2, 2)
    cond = torch.ones(1, 1, 2, 2)
    guidance = 2.0
    weight = torch.tensor(0.5)
    # guided = 0 + 2*(1-0) = 2; residual = 2-1 = 1; weighted = 0.5
    got = compute_sds_gradient(
        objective="weighted_sds",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=guidance,
        weight_t=weight,
    )
    assert torch.allclose(got, torch.full_like(got, 0.5))


def test_compute_sds_gradient_csd() -> None:
    noise = torch.randn(1, 1, 2, 2)
    uncond = torch.zeros(1, 1, 2, 2)
    cond = torch.ones(1, 1, 2, 2)
    weight = torch.tensor(0.25)
    guidance = 8.0
    got = compute_sds_gradient(
        objective="csd",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=guidance,
        weight_t=weight,
    )
    # w * guidance * (cond - uncond) = 0.25 * 8 * 1 = 2
    assert torch.allclose(got, torch.full_like(got, 2.0))


def test_compute_sds_gradient_nfsd() -> None:
    noise = torch.zeros(1, 1, 2, 2)
    uncond = torch.ones(1, 1, 2, 2)
    cond = torch.full((1, 1, 2, 2), 3.0)
    delta_d = torch.full((1, 1, 2, 2), 0.5)
    weight = torch.tensor(2.0)
    guidance = 7.5
    # delta_c = 2; w*(delta_d + g*delta_c) = 2*(0.5 + 7.5*2) = 2*15.5 = 31
    got = compute_sds_gradient(
        objective="nfsd",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=guidance,
        weight_t=weight,
        delta_d=delta_d,
    )
    assert torch.allclose(got, torch.full_like(got, 31.0))


def test_nfsd_delta_d_branches() -> None:
    uncond = torch.ones(1, 1, 2, 2)
    neg = torch.zeros(1, 1, 2, 2)
    assert torch.equal(nfsd_delta_d(timestep=199, uncond=uncond, neg=neg), uncond)
    assert torch.equal(nfsd_delta_d(timestep=200, uncond=uncond, neg=neg), uncond - neg)
    with pytest.raises(ValueError):
        nfsd_delta_d(timestep=500, uncond=uncond, neg=None)


def test_nfsd_negative_prompt_matches_paper() -> None:
    assert "unrealistic" in NFSD_NEGATIVE_PROMPT
    assert "blurry" in NFSD_NEGATIVE_PROMPT
    assert "gloomy" in NFSD_NEGATIVE_PROMPT


def test_hifa_timestep_fraction_bounds() -> None:
    assert hifa_timestep_fraction(0.0) == pytest.approx(0.98)
    assert hifa_timestep_fraction(1.0) == pytest.approx(0.02)
    mid = hifa_timestep_fraction(0.25)
    assert 0.02 < mid < 0.98


def test_sds_timestep_weight() -> None:
    alphas = torch.tensor([0.9, 0.5, 0.1])
    assert sds_timestep_weight(alphas, 1).item() == pytest.approx(0.5)


def test_learning_rate_legacy_alias_sets_both() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--type",
            "flip",
            "--prompt",
            "a",
            "--prompt",
            "b",
            "--out",
            "/tmp/x",
            "--learning-rate",
            "0.01",
        ]
    )
    config = config_from_args(args)
    assert config.sds_lr == 0.01
    assert config.dream_lr == 0.01


def test_learning_rate_split_flags_override_legacy() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--type",
            "flip",
            "--prompt",
            "a",
            "--prompt",
            "b",
            "--out",
            "/tmp/x",
            "--sds-lr",
            "0.002",
            "--dream-lr",
            "0.003",
        ]
    )
    config = config_from_args(args)
    assert config.sds_lr == 0.002
    assert config.dream_lr == 0.003


def test_style_templates_preserve_subject() -> None:
    subject = "a dog"
    for name, template in STYLE_TEMPLATES.items():
        styled = apply_style_template(subject, name)
        assert subject in styled or "dog" in styled
        assert styled == template.format(subject)
    assert apply_style_template(subject, None) == subject
    assert apply_style_template(subject, "none") == subject


def test_warn_low_clip_margins_non_blocking() -> None:
    import warnings

    with pytest.warns(UserWarning, match="low-confidence CLIP margins"):
        warn_low_clip_margins([0.1, -0.2])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_low_clip_margins([0.1, 0.2])
    assert not any(issubclass(w.category, UserWarning) for w in caught)


def test_gradient_conflict_stats() -> None:
    a = torch.tensor([1.0, 0.0])
    b = torch.tensor([-1.0, 0.0])
    stats = gradient_conflict_stats([a, b])
    assert stats["cosine"] == pytest.approx(-1.0)
    assert stats["norm_ratio"] == pytest.approx(1.0)


def test_round_robin_exposure_counts_equal() -> None:
    """Simulate alternating exposure accounting used by optimize_illusion."""
    exposures = [0, 0]
    steps = 10  # 5 logical * 2
    for step in range(steps):
        exposures[step % 2] += 1
    assert exposures == [5, 5]


def test_sds_microbatch_matches_full_batch_numerically() -> None:
    """Shared t/noise + summed chunks must match a single full-batch call."""
    adapter = object.__new__(DiffusionAdapter)
    adapter.device = "cpu"
    adapter.dtype = torch.float32
    adapter.sds_objective = "legacy"
    adapter.view_batch_size = None
    adapter.embeddings = {
        "dog": torch.zeros(2, 4, 8),
        "sloth": torch.zeros(2, 4, 8),
    }
    adapter.embed = lambda prompt: adapter.embeddings[prompt]

    def encode_latent(image):
        b, _, h, w = image.shape
        out = torch.zeros(b, 4, h // 8, w // 8)
        for i in range(b):
            out[i] = float(i + 1)
        return out

    adapter.encode_latent = encode_latent

    class FakeScheduler:
        config = SimpleNamespace(num_train_timesteps=1000)
        alphas_cumprod = torch.linspace(0.9, 0.1, 1000)

        def add_noise(self, latents, noise, timesteps):
            return latents + noise * 0.01

    calls: list[int] = []

    class FakeUNet:
        def __call__(self, model_in, timesteps, encoder_hidden_states):
            calls.append(model_in.shape[0])
            # Deterministic prediction from input so grads match across chunks
            return SimpleNamespace(sample=model_in * 0.5)

    adapter.pipe = SimpleNamespace(unet=FakeUNet())
    adapter.scheduler = FakeScheduler()

    derived = torch.rand(2, 3, 16, 16)
    prompts = ["dog", "sloth"]
    weights = [1.0, 1.0]
    gen_full = torch.Generator().manual_seed(0)
    loss_full = adapter.sds_loss_batch(
        derived, prompts, weights, 100.0, gen_full, view_batch_size=None
    )
    gen_micro = torch.Generator().manual_seed(0)
    loss_micro = adapter.sds_loss_batch(
        derived, prompts, weights, 100.0, gen_micro, view_batch_size=1
    )
    assert torch.allclose(loss_full, loss_micro, atol=1e-5)
    # full: one CFG-doubled call (batch 4); micro: two calls of batch 2
    assert calls[0] == 4
    assert calls[1:] == [2, 2]


def test_illusion_config_defaults_objective_legacy() -> None:
    config = IllusionConfig(illusion="flip", prompts=["a", "b"])
    assert config.sds_objective == "legacy"
    assert config.sds_lr == 1e-3
    assert config.dream_lr == 1e-3
    assert config.use_hifa_schedule is False
    assert config.round_robin is False
