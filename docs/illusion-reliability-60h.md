# Illusion Reliability: Author-Reference Campaign

This is an experiment-only reliability program. It does not change
`IllusionConfig` defaults, select a new product default, or alter the PR #118
acceptance gate.

## Decision

The next long window tests the public author recipe, not another SDS objective
ablation. Earlier local cells used a materially different optimizer: 500 Adam
steps at 512px. The reference recipe uses 10,000 SGD steps, a native 256px
Fourier network upsampled to 512px for diffusion, equal shuffled exposure to
both views, weighted SDS with a 0.1 gradient multiplier, guidance 60, and an
SDS learning rate of 1e-4.

The recipe is available only through:

```text
--experimental-recipe author_reference
```

The legacy default remains unchanged.

## Primary matrix

The `reference60h` phase has 36 cells:

- five topology-compatible pencil pairs at seeds 11, 23, 37, 53, 71, 89;
- giraffe/penguin calibration at seeds 11, 23, 37, 53;
- locomotive/eye incompatible control at seeds 11 and 23.

The ordering is breadth-first by seed. The first cell is the
giraffe/penguin seed-11 smoke. Every cell saves SDS checkpoints at 500, 2,000,
5,000, and 10,000 steps, Dream round 1, later Dream checkpoints, and final
images.

The estimated duration is 88 minutes per cell, or 52.8 GPU-hours before small
driver overhead. The unattended driver gets a 58-hour deadline and starts no
cell unless its estimate plus reserve fits.

## Calibration smoke result

The required giraffe/penguin seed-11 smoke completed at implementation commit
`7f8d342`:

| Measurement | Result |
|-------------|-------:|
| Total runtime | 3416.60 s (56.9 min) |
| SDS runtime | 3240.95 s |
| Dream runtime | 173.45 s |
| Peak allocated VRAM | 4331.11 MB |
| Process result | Completed without error |

All required SDS, Dream, and final checkpoints were written. Human review
selected two stage-specific keepers:

- SDS-10000 `prime_1.png` for the strongest raw dual-subject structure;
- final `prime_1.png` for the cleanest presentation.

This is a positive smoke result, not a default-promotion gate. The remaining
35 cells stay paused until explicitly launched. Extrapolating the measured
cell gives about 33.2 GPU-hours remaining; the original 51.3-hour remaining
estimate remains the conservative unattended budget.

The immutable plans and completed smoke are pinned to implementation commit
`7f8d342`. Campaign execution must use a worktree at that exact commit; later
documentation-only commits do not replace the plan's recorded Git SHA.

## Kill gate

Run only the first cell before departure. Stop the primary axis if:

- the SDS-10,000 views do not show two recognizable silhouettes;
- either view is blank, non-finite, or nearly constant;
- the upside-down relationship is not exact;
- Dream round 1 destroys structure already present at SDS-end.

If the smoke fails, use `early-dream-backup`. It contains 48 roughly ten-minute
cells over the calibration pair and five compatible pairs, eight seeds,
legacy round-robin SDS, two Dream rounds, and strengths 0.95 then 0.50.

## Review

Review is stage-separated. The harness builds independent blinded sheets for:

1. the last SDS checkpoint;
2. Dream round 1;
3. final output.

The rating template asks for keep, subject readability, artifacts, and notes.
Do not select a profile from final-only sheets. Do not promote a default from
this campaign without a later human acceptance gate.

## SDXL quarantine

The failed SDXL pilot was not evidence that the backbone is intrinsically
unsuitable. Its Euler scheduler used inference-sigma noising as if it were the
training forward process, without the matching input scaling. The pilot also
claimed a 1024px original size for a 512px canvas. Both are corrected, but
SDXL remains outside this campaign.

Before reopening SDXL, run `worker.illusion_sdxl_diagnostic`. It produces two
direct text-to-image controls, a VAE reconstruction, and conditioning/noising
metrics at timesteps 100, 500, and 900. A failing diagnostic kills SDXL before
any optimizer matrix.
