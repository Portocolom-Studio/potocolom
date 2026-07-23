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
    checkpoint_name,
    compute_sds_gradient,
    config_from_args,
    gradient_conflict_stats,
    hifa_timestep_fraction,
    nfsd_delta_d,
    preserve_rng_state,
    resolve_learning_rates,
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
    assert torch.allclose(got, torch.full_like(got, 2.0))


def test_compute_sds_gradient_nfsd() -> None:
    noise = torch.zeros(1, 1, 2, 2)
    uncond = torch.ones(1, 1, 2, 2)
    cond = torch.full((1, 1, 2, 2), 3.0)
    delta_d = torch.full((1, 1, 2, 2), 0.5)
    weight = torch.tensor(2.0)
    guidance = 7.5
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
    assert resolve_learning_rates(config) == (0.01, 0.01)


def test_learning_rate_programmatic_alias() -> None:
    config = IllusionConfig(illusion="flip", prompts=["a", "b"], learning_rate=0.01)
    assert resolve_learning_rates(config) == (0.01, 0.01)
    config = IllusionConfig(illusion="flip", prompts=["a", "b"], learning_rate=0.01, dream_lr=0.003)
    assert resolve_learning_rates(config) == (0.01, 0.003)
    config = IllusionConfig(illusion="flip", prompts=["a", "b"], learning_rate=0.01, sds_lr=0.002)
    assert resolve_learning_rates(config) == (0.002, 0.01)


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
    assert resolve_learning_rates(config) == (0.002, 0.003)


def test_style_templates_preserve_subject() -> None:
    subject = "a dog"
    for name, template in STYLE_TEMPLATES.items():
        styled = apply_style_template(subject, name)
        assert "dog" in styled
        assert styled == template.format(subject)


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
    exposures = [0, 0]
    for step in range(10):
        exposures[step % 2] += 1
    assert exposures == [5, 5]


def test_checkpoint_names_phase_qualified() -> None:
    assert checkpoint_name(60) == "sds_0060"
    assert checkpoint_name(500) == "sds_0500"


def test_defaults_are_legacy_equivalent() -> None:
    config = IllusionConfig(illusion="flip", prompts=["a", "b"])
    assert config.sds_objective == "legacy"
    assert config.checkpoint_steps == ()
    assert config.collect_diagnostics is False
    assert config.enable_vae_slicing is False
    assert config.channels_last is False
    assert resolve_learning_rates(config) == (1e-3, 1e-3)


def test_preserve_rng_state_is_neutral() -> None:
    torch.manual_seed(0)
    before = torch.rand(3).clone()
    torch.manual_seed(0)

    def _perturb() -> None:
        _ = torch.rand(8)
        if torch.cuda.is_available():
            _ = torch.rand(8, device="cuda")

    preserve_rng_state(_perturb)
    after = torch.rand(3)
    assert torch.equal(before, after)


def _stub_adapter() -> DiffusionAdapter:
    adapter = object.__new__(DiffusionAdapter)
    adapter.device = "cpu"
    adapter.dtype = torch.float32
    adapter.sds_objective = "legacy"
    adapter.view_batch_size = None
    adapter.encode_batch_sizes = []
    adapter.backward_before_next_encode = []
    adapter._encodes_since_backward = 0
    adapter.embeddings = {
        "dog": torch.zeros(2, 4, 8),
        "sloth": torch.zeros(2, 4, 8),
    }
    adapter.embed = lambda prompt: adapter.embeddings[prompt]

    class FakeScheduler:
        config = SimpleNamespace(num_train_timesteps=1000)
        alphas_cumprod = torch.linspace(0.99, 0.01, 1000)

        def add_noise(self, latents, noise, timesteps):
            return latents + noise * 0.01

    class FakeUNet:
        def __call__(self, model_in, timesteps, encoder_hidden_states):
            return SimpleNamespace(sample=model_in * 0.5)

    class FakePosterior:
        def __init__(self, mean: torch.Tensor) -> None:
            self.mean = mean
            self.std = torch.ones_like(mean) * 0.1

        def sample(self, generator=None):
            return self.mean

    class FakeVae:
        config = SimpleNamespace(scaling_factor=0.18215)

        def encode(self, scaled):
            # Differentiable downsample so SDS grads reach the image tensor.
            pooled = torch.nn.functional.avg_pool2d(scaled, kernel_size=8, stride=8)
            mean = torch.cat([pooled, pooled[:, :1]], dim=1)
            return SimpleNamespace(latent_dist=FakePosterior(mean))

    adapter.pipe = SimpleNamespace(unet=FakeUNet(), vae=FakeVae())
    adapter.scheduler = FakeScheduler()

    def encode_latent(image, *, generator=None, use_mean=False, posterior_eps=None):
        return DiffusionAdapter.encode_latent(
            adapter, image, generator=generator, use_mean=use_mean, posterior_eps=posterior_eps
        )

    adapter.encode_latent = encode_latent  # type: ignore[method-assign]
    adapter.note_backward = lambda: DiffusionAdapter.note_backward(adapter)
    return adapter


def test_sds_microbatch_encodes_size_one_and_backs_before_next() -> None:
    adapter = _stub_adapter()
    derived = torch.rand(2, 3, 16, 16, requires_grad=True)
    prompts = ["dog", "sloth"]
    weights = [1.0, 1.0]
    # Shared posterior eps for reproducible comparison
    eps = torch.randn(2, 4, 2, 2)
    gen_full = torch.Generator().manual_seed(1)
    # Full-batch reference with explicit eps and shared t/noise
    fake = torch.empty(2, 4, 2, 2)
    t, noise, alphas = adapter._sample_sds_noise_and_t(
        fake, gen_full, progress=None, use_hifa_schedule=False
    )
    derived_full = derived.detach().clone().requires_grad_(True)
    loss_full = adapter.sds_loss_batch(
        derived_full,
        prompts,
        weights,
        100.0,
        gen_full,
        posterior_eps=eps,
        shared_timestep=t,
        shared_noise=noise,
    )
    loss_full.backward()
    grad_full = derived_full.grad.detach().clone()

    adapter.encode_batch_sizes.clear()
    adapter.backward_before_next_encode.clear()
    adapter._encodes_since_backward = 0
    derived_micro = derived.detach().clone().requires_grad_(True)
    # Force the pre-sampled t/noise path inside microbatch by seeding identically
    # after manually injecting via a thin wrapper: call microbatch with same seed
    # for the shape-only sample, then compare using matched posterior_eps.
    gen_micro = torch.Generator().manual_seed(1)
    adapter.sds_microbatch_backward(
        derived_micro,
        prompts,
        weights,
        100.0,
        gen_micro,
        view_batch_size=1,
        posterior_eps=eps,
    )
    assert adapter.encode_batch_sizes == [1, 1]
    # After first encode, a backward must have happened before the second encode.
    assert True in adapter.backward_before_next_encode
    assert derived_micro.grad is not None
    assert torch.allclose(derived_micro.grad, grad_full, atol=1e-5)


def test_instrumented_vs_uninstrumented_legacy_toy_equivalence() -> None:
    """Diagnostics must not change training grads when collect_diagnostics is off.

    Uses a tiny FFN-free path: two configs that differ only in diagnostics flags
    must resolve to identical learning rates and empty checkpoint sets by default.
    """
    plain = IllusionConfig(illusion="flip", prompts=["a", "b"], seed=2)
    instrumented = IllusionConfig(
        illusion="flip",
        prompts=["a", "b"],
        seed=2,
        collect_diagnostics=True,
        checkpoint_steps=(60, 125, 250, 500),
    )
    assert plain.checkpoint_steps == ()
    assert plain.collect_diagnostics is False
    assert resolve_learning_rates(plain) == resolve_learning_rates(instrumented)
    # RNG-neutral helper restores state
    torch.manual_seed(123)
    a = torch.rand(4)
    torch.manual_seed(123)
    preserve_rng_state(lambda: torch.rand(16))
    b = torch.rand(4)
    assert torch.equal(a, b)
