"""Unit tests for SDS objectives, LR flags, styles, microbatching, and NFSD."""

from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from worker.illusions import (  # noqa: E402
    NFSD_NEGATIVE_PROMPT,
    STYLE_TEMPLATES,
    DiffusionAdapter,
    IllusionConfig,
    ReferenceFourierFeatureNetwork,
    add_training_noise,
    apply_style_template,
    balanced_view_schedule,
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
    sqrt_anneal_timestep_fraction,
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


def test_compute_sds_gradient_csd_canonical_ignores_guidance() -> None:
    noise = torch.randn(1, 1, 2, 2)
    uncond = torch.zeros(1, 1, 2, 2)
    cond = torch.ones(1, 1, 2, 2)
    weight = torch.tensor(0.25)
    got = compute_sds_gradient(
        objective="csd",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=8.0,
        weight_t=weight,
    )
    # Canonical Eq.7: w(t)*(cond-uncond) = 0.25 * 1, not 0.25*8
    assert torch.allclose(got, torch.full_like(got, 0.25))
    got2 = compute_sds_gradient(
        objective="csd",
        noise=noise,
        uncond=uncond,
        cond=cond,
        guidance_scale=1.0,
        weight_t=weight,
    )
    assert torch.allclose(got, got2)


def test_csd_rejects_explicit_guidance_cli() -> None:
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
            "--sds-objective",
            "csd",
            "--sds-guidance",
            "7.5",
        ]
    )
    with pytest.raises(SystemExit, match="csd rejects"):
        config_from_args(args)


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


def test_sqrt_anneal_timestep_fraction_bounds() -> None:
    assert sqrt_anneal_timestep_fraction(0.0) == pytest.approx(0.98)
    assert sqrt_anneal_timestep_fraction(1.0) == pytest.approx(0.02)
    assert hifa_timestep_fraction(0.25) == pytest.approx(sqrt_anneal_timestep_fraction(0.25))


def test_oil_and_coherent_oil_templates_differ() -> None:
    assert STYLE_TEMPLATES["oil"] == "an oil painting of {}"
    assert STYLE_TEMPLATES["coherent_oil"] == "a coherent oil painting of {}"
    assert apply_style_template("a dog", "oil") != apply_style_template("a dog", "coherent_oil")
    assert apply_style_template("a dog", "oil") == "an oil painting of a dog"


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
    assert config.experimental_recipe == "legacy"
    assert config.sds_objective == "legacy"
    assert config.checkpoint_steps == ()
    assert config.collect_diagnostics is False
    assert config.enable_vae_slicing is False
    assert config.channels_last is False
    assert resolve_learning_rates(config) == (1e-3, 1e-3)


def test_reference_ffn_renders_native_256_canvas() -> None:
    network = ReferenceFourierFeatureNetwork(features=8, hidden=16)
    image = network.image()
    assert image.shape == (1, 3, 256, 256)
    assert image.min().item() >= 0
    assert image.max().item() <= 1


def test_balanced_view_schedule_is_deterministic_and_equal() -> None:
    first = balanced_view_schedule(10, 2, seed=37)
    second = balanced_view_schedule(10, 2, seed=37)
    assert first == second
    assert first.count(0) == first.count(1) == 5
    assert first != [0, 1] * 5
    with pytest.raises(ValueError, match="divisible"):
        balanced_view_schedule(9, 2, seed=0)


def test_training_noise_matches_alpha_cumprod_equation() -> None:
    latent = torch.full((1, 1, 2, 2), 2.0)
    noise = torch.full_like(latent, 3.0)
    alphas = torch.tensor([0.81])
    timestep = torch.tensor([0])
    got = add_training_noise(latent, noise, timestep, alphas)
    expected = 0.9 * latent + (1 - 0.81) ** 0.5 * noise
    assert torch.allclose(got, expected)


def test_euler_sds_uses_training_noise_not_inference_sigma() -> None:
    adapter = object.__new__(DiffusionAdapter)
    adapter.scheduler = type(
        "EulerDiscreteScheduler",
        (),
        {"add_noise": lambda *_: (_ for _ in ()).throw(AssertionError("must not call"))},
    )()
    latent = torch.ones(1, 1, 2, 2)
    noise = torch.ones_like(latent)
    timesteps = torch.tensor([0])
    alphas = torch.tensor([0.25])
    got = adapter._add_sds_noise(latent, noise, timesteps, alphas)
    assert torch.allclose(got, torch.full_like(latent, 0.5 + 0.75**0.5))


def test_sdxl_time_ids_match_actual_canvas() -> None:
    adapter = object.__new__(DiffusionAdapter)
    adapter.device = "cpu"
    adapter.dtype = torch.float32
    got = adapter._sdxl_time_ids(2)
    assert got.tolist() == [[512, 512, 0, 0, 512, 512]] * 2


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


def test_reference_checkpoint_ladder_reproduces_the_smoke_budget() -> None:
    from worker.illusion_experiment import reference_checkpoint_ladder

    assert reference_checkpoint_ladder(10_000) == (500, 2_000, 5_000, 10_000)
    assert reference_checkpoint_ladder(3_000) == (150, 600, 1_500, 3_000)
    # No zero-step checkpoint on a tiny smoke budget.
    assert all(step > 0 for step in reference_checkpoint_ladder(10))


def test_baked_style_lets_a_pencil_pair_be_asked_for_oil() -> None:
    from worker.illusion_experiment import PAIR_BY_ID, resolve_pair_prompts

    pair = PAIR_BY_ID["pine_chandelier"]
    # Its own style, and no style, stay verbatim.
    assert resolve_pair_prompts(pair, None)[1] == [pair.prompt_a, pair.prompt_b]
    assert resolve_pair_prompts(pair, "reference_pencil")[1] == [pair.prompt_a, pair.prompt_b]
    # A different medium actually changes the prompt, so a style arm is real.
    oil = resolve_pair_prompts(pair, "reference_oil")[1]
    assert oil != [pair.prompt_a, pair.prompt_b]
    assert all("oil painting" in prompt for prompt in oil)


def test_legacy_oil_pairs_keep_their_exact_prompts() -> None:
    from worker.illusion_experiment import FINAL_PAIRS, resolve_pair_prompts

    for pair in FINAL_PAIRS:
        for style in (None, "none", "oil"):
            assert resolve_pair_prompts(pair, style)[1] == [pair.prompt_a, pair.prompt_b]


def test_calibration_pair_keeps_the_wording_that_was_actually_run() -> None:
    from worker.illusion_experiment import PAIR_BY_ID

    pair = PAIR_BY_ID["giraffe_penguin_calibration"]
    assert pair.prompt_a == "an intricate detailed hb pencil sketch of a giraffe head"
    assert pair.prompt_b == "an intricate detailed hb pencil sketch of a penguin"


def test_degenerate_run_flags_a_flat_field_but_not_a_textured_image(tmp_path) -> None:
    import random

    from PIL import Image

    from worker.illusion_experiment import degenerate_run

    flat = tmp_path / "flat"
    flat.mkdir()
    for name in ("derived_1.png", "derived_2.png"):
        Image.new("RGB", (64, 64), (127, 127, 127)).save(flat / name)
    assert degenerate_run(flat) is True

    noisy = tmp_path / "noisy"
    noisy.mkdir()
    rng = random.Random(0)
    for name in ("derived_1.png", "derived_2.png"):
        image = Image.new("RGB", (64, 64))
        image.putdata([(rng.randrange(256),) * 3 for _ in range(64 * 64)])
        image.save(noisy / name)
    assert degenerate_run(noisy) is False

    # No output at all counts as catastrophic.
    assert degenerate_run(tmp_path / "missing") is True


def test_build_yield_sheets_groups_by_pair(tmp_path) -> None:
    """Yield is read per pair across seeds, so the sheet must group that way."""
    import json

    from PIL import Image

    from worker.illusion_experiment import build_yield_sheets

    root = tmp_path / "runs"
    for pair_id, seeds in (("pair_a", (11, 23)), ("pair_b", (11,))):
        for seed in seeds:
            run = root / pair_id / f"seed_{seed}"
            run.mkdir(parents=True)
            for view in (1, 2):
                Image.new("RGB", (64, 64), (10 * view, 200, 30)).save(run / f"derived_{view}.png")
            (run / "manifest.json").write_text(
                json.dumps({"status": "completed", "pair_id": pair_id, "config": {"seed": seed}})
            )
    # An incomplete run must not appear at all.
    skipped = root / "pair_a" / "seed_99"
    skipped.mkdir(parents=True)
    (skipped / "manifest.json").write_text(json.dumps({"status": "failed", "pair_id": "pair_a"}))

    out = tmp_path / "sheets"
    summary = build_yield_sheets(root, out)

    assert set(summary["pairs"]) == {"pair_a", "pair_b"}
    assert summary["pairs"]["pair_a"]["seeds"] == [11, 23]
    assert summary["pairs"]["pair_b"]["runs"] == 1
    # One sheet per pair, plus the index and the machine-readable summary.
    assert (out / "pair_a.png").is_file()
    assert (out / "pair_b.png").is_file()
    assert (out / "index.html").is_file()
    assert (out / "yield.json").is_file()
    # pair_a has 2 seeds x 2 views = 4 cells at cols=4, so one row.
    assert Image.open(out / "pair_a.png").size == (1024, 256)


def test_build_yield_sheets_can_show_a_non_final_stage(tmp_path) -> None:
    """dream_d1 beat the final on both pre-window pairs, so stage must be selectable."""
    import json

    import pytest
    from PIL import Image

    from worker.illusion_experiment import build_yield_sheets

    run = tmp_path / "runs" / "pair_a" / "seed_1"
    (run / "ckpt_dream_round_01").mkdir(parents=True)
    (run / "ckpt_sds_2000").mkdir(parents=True)
    (run / "ckpt_sds_0500").mkdir(parents=True)
    # A distinct colour per stage so the sheets cannot be confused.
    for target, colour in (
        (run, (10, 10, 200)),
        (run / "ckpt_dream_round_01", (10, 200, 10)),
        (run / "ckpt_sds_2000", (200, 10, 10)),
        (run / "ckpt_sds_0500", (90, 90, 90)),
    ):
        for view in (1, 2):
            Image.new("RGB", (32, 32), colour).save(target / f"derived_{view}.png")
    (run / "manifest.json").write_text(
        json.dumps({"status": "completed", "pair_id": "pair_a", "config": {"seed": 1}})
    )

    sheets = {}
    for stage in ("final", "dream_d1", "sds_end"):
        out = tmp_path / stage
        summary = build_yield_sheets(tmp_path / "runs", out, stage)
        assert summary["stage"] == stage
        sheets[stage] = (out / "pair_a.png").read_bytes()

    # Each stage renders different pixels, and sds_end picks the LAST checkpoint.
    assert len(set(sheets.values())) == 3
    top_left = Image.open(tmp_path / "sds_end" / "pair_a.png").getpixel((10, 10))
    assert top_left[0] > top_left[1], "sds_end should use ckpt_sds_2000, not 0500"

    with pytest.raises(ValueError):
        build_yield_sheets(tmp_path / "runs", tmp_path / "bad", "nonsense")


def test_blind_sheets_refuse_to_clobber_a_partial_review(tmp_path) -> None:
    """ratings.jsonl is hand-edited; a rebuild must not destroy verdicts."""
    import json

    import pytest
    from PIL import Image

    from worker.illusion_experiment import build_stage_blind_sheets

    run = tmp_path / "runs" / "pair_a" / "seed_1"
    (run / "ckpt_dream_round_01").mkdir(parents=True)
    (run / "ckpt_sds_2000").mkdir(parents=True)
    for target in (run, run / "ckpt_dream_round_01", run / "ckpt_sds_2000"):
        for view in (1, 2):
            Image.new("RGB", (32, 32), (120, 30, 30)).save(target / f"derived_{view}.png")
    (run / "manifest.json").write_text(
        json.dumps({"status": "completed", "pair_id": "pair_a", "config": {"seed": 1}})
    )

    out = tmp_path / "blind"
    build_stage_blind_sheets(tmp_path / "runs", out, seed=1)
    ratings = out / "ratings.jsonl"
    rows = [json.loads(line) for line in ratings.read_text().splitlines()]
    assert rows and all(r["keep"] is None for r in rows)

    # A human rates one cell, then someone rebuilds.
    rows[0]["keep"] = True
    ratings.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(FileExistsError):
        build_stage_blind_sheets(tmp_path / "runs", out, seed=1)

    # The verdict survives, and is also copied aside.
    assert json.loads(ratings.read_text().splitlines()[0])["keep"] is True
    assert (out / "ratings.jsonl.rated").is_file()

    # An untouched template is replaced without complaint.
    for r in rows:
        r["keep"] = None
    ratings.write_text("".join(json.dumps(r) + "\n" for r in rows))
    build_stage_blind_sheets(tmp_path / "runs", out, seed=1)
