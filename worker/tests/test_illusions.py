"""Arrangement math and prime parameterization for Diffusion Illusions
(issue #115). CPU-only; skipped entirely when torch is not installed
(CI runs the worker without the inference extra)."""

import pytest

torch = pytest.importorskip("torch")

from worker.illusions import (  # noqa: E402  (import needs torch)
    ILLUSIONS,
    FourierFeatureNetwork,
    IllusionConfig,
    image_similarity_loss,
    overlay,
    rot90,
    ssim,
    targets_for,
)


def prime(seed: int) -> "torch.Tensor":
    generator = torch.Generator().manual_seed(seed)
    return torch.rand((1, 3, 16, 16), generator=generator)


def test_specs_match_paper_table() -> None:
    assert ILLUSIONS["flip"].n_primes == 1
    assert len(ILLUSIONS["flip"].weights) == 2
    assert ILLUSIONS["rotate"].n_primes == 2
    assert len(ILLUSIONS["rotate"].weights) == 4
    assert ILLUSIONS["hidden"].n_primes == 4
    assert ILLUSIONS["hidden"].weights == [1.0, 1.0, 1.0, 1.0, 3.0]


def test_flip_is_identity_and_180() -> None:
    image = prime(1)
    first, second = ILLUSIONS["flip"].arrange([image])
    assert torch.equal(first, image)
    assert torch.equal(second, rot90(image, 2))
    assert torch.equal(rot90(second, 2), image)


def test_rotation_overlay_shapes_and_range() -> None:
    derived = ILLUSIONS["rotate"].arrange([prime(1), prime(2)])
    assert len(derived) == 4
    for image in derived:
        assert image.shape == (1, 3, 16, 16)
        assert image.min() >= 0 and image.max() < 1


def test_hidden_overlay_passes_primes_through() -> None:
    primes = [prime(seed) for seed in range(4)]
    derived = ILLUSIONS["hidden"].arrange(primes)
    assert len(derived) == 5
    for original, passed in zip(primes, derived[:4], strict=False):
        assert torch.equal(original, passed)
    assert derived[4].min() >= 0 and derived[4].max() < 1


def test_overlay_is_commutative() -> None:
    a, b = prime(1), prime(2)
    assert torch.allclose(overlay([a, b], 2.0), overlay([b, a], 2.0))


def test_arrangements_are_differentiable() -> None:
    for name, spec in ILLUSIONS.items():
        primes = [prime(seed).requires_grad_(True) for seed in range(spec.n_primes)]
        loss = sum(image.sum() for image in spec.arrange(primes))
        loss.backward()
        for image in primes:
            assert image.grad is not None, name
            assert torch.isfinite(image.grad).all(), name


def test_ffn_renders_expected_shape_and_range() -> None:
    torch.manual_seed(0)
    network = FourierFeatureNetwork(features=16, hidden=16)
    image = network.image(resolution=8)
    assert image.shape == (1, 3, 8, 8)
    assert image.min() >= 0 and image.max() <= 1


def test_ffn_is_deterministic_per_seed() -> None:
    torch.manual_seed(7)
    first = FourierFeatureNetwork(features=16, hidden=16).image(resolution=8)
    torch.manual_seed(7)
    second = FourierFeatureNetwork(features=16, hidden=16).image(resolution=8)
    assert torch.equal(first, second)


def test_ssim_identity_and_similarity_loss() -> None:
    image = prime(3)
    assert ssim(image, image).item() == pytest.approx(1.0, abs=1e-4)
    assert image_similarity_loss(image, image).item() == pytest.approx(0.0, abs=1e-3)
    other = prime(4)
    assert image_similarity_loss(image, other).item() > 0.1


def test_targets_require_one_per_derived_image() -> None:
    config = IllusionConfig(illusion="flip", prompts=["dog"])
    with pytest.raises(ValueError):
        targets_for(config, ILLUSIONS["flip"])
    config = IllusionConfig(illusion="flip", prompts=["dog", "sloth"])
    assert targets_for(config, ILLUSIONS["flip"]) == ["dog", "sloth"]


def test_image_target_fills_final_slot() -> None:
    target = prime(9)
    config = IllusionConfig(
        illusion="hidden",
        prompts=["a", "b", "c", "d"],
        target_image=target,
    )
    targets = targets_for(config, ILLUSIONS["hidden"])
    assert len(targets) == 5
    assert targets[4] is target
