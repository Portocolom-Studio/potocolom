# Third-party model licenses

potocolom ships model manifests that point at weights hosted on Hugging Face.
Each model is governed by its upstream license. This file summarizes obligations
for models bundled in `worker/models/`. It is not legal advice.

## Benchmark-only models (not offered to users)

These manifests set `benchmark_only: true`. The worker can load them for the
benchmark harness; `GET /api/v1/models` omits them so the studio UI cannot
select them. Reference timings may still appear on `/benchmark`.

| Model | License | Product status |
| --- | --- | --- |
| sdxl-hypersd | Open RAIL++-M base + CreativeML Open RAIL++-M LoRA | Benchmark reference (issue #75); no license blocker, held back on measurement (see below) |
| sd-turbo | Stability AI Community | Benchmark speed anchor; hidden from studio for quality |
| sdxl-turbo | Stability AI Community | Benchmark speed anchor; hidden from studio for quality |
| dreamshaper-lcm | CreativeML Open RAIL-M | Benchmark / self-host reference; hidden from studio for quality |

### Hyper-SD carries three licenses in one file

Earlier revisions of this file and of `docs/decisions.md` recorded that the
Hyper-SD LoRA has no declared license. That was wrong, and it is corrected here
(2026-08-24). `ByteDance/Hyper-SD` ships a single `LICENSE.md` that assigns
terms per model family rather than one license for the repository:

| Checkpoints in the repo | License |
| --- | --- |
| FLUX.1-dev Hyper LoRAs | FLUX.1 [dev] Non-Commercial. Commercial use only by discretionary grant from Black Forest Labs. No revenue tier. |
| SD3 Hyper checkpoints | Stability AI Community. Free commercial use under USD $1,000,000 annual revenue. |
| All other SD checkpoints, which is where `Hyper-SDXL-1step-lora` and `Hyper-SDXL-2steps-lora` sit | CreativeML Open RAIL++-M, dated 2024-04-11, ByteDance Inc. |

`sdxl-hypersd` fuses an SDXL LoRA, so it falls in the third row: the same
license family as `sdxl-base` and the `ssd-1b-lightning` Lightning LoRA already
shipped, with use restrictions but no revenue cap and no attribution banner.

The manifest keeps `benchmark_only: true` anyway. The reason is now purely a
measurement one: `sdxl-hypersd` runs 3.77 s at 1024/8, within 0.01 s of the
`sdxl-fast` path already in the studio, so promoting it would add a model
without adding a capability. See
[gpu-performance.md](gpu-performance.md).

Read the license file per family before adopting any other Hyper-SD variant. A
repository-level license tag is not a reliable summary when the file is
structured this way.

## Stability Community License product models

| Model | License | Product status |
| --- | --- | --- |
| sd35-medium | Stability AI Community | Studio quality tier (issue #151). Gated weights, `HF_TOKEN` required; runs on the model-offload rung on a 16 GB card at roughly 56 s per 1024 px image |

## Stability AI Community License (sd-turbo, sdxl-turbo, sd35-medium)

Applies while **you or your affiliates** generate **≤ USD $1,000,000** in
annual revenue (aggregate, from any source). Above that threshold the Community
License terminates and you must stop using these models or obtain an Enterprise
license from Stability AI.

**Before commercial use** (including offering these models from a commercial
benchmark or product surface):

1. Register at [stability.ai/community-license](https://stability.ai/community-license).
2. Comply with the [Stability AI Acceptable Use Policy](https://stability.ai/use-policy).

**When offering a product or service that uses these models**, the license also
requires:

- A **Notice** file distributed with copies, containing:
  `This Stability AI Model is licensed under the Stability AI Community License, Copyright © Stability AI Ltd. All Rights Reserved`
- **Prominent display** of **"Powered by Stability AI"** on the website, user
  interface, or product documentation.

The studio satisfies the display obligation per model: the generate panel
renders a manifest's `requires_attribution` string beneath the model picker
whenever it is non-empty, so the credit appears exactly when a Stability model
is selected and never implies Stability powers an Apache-licensed model.

Hub access is a separate gate from commercial registration. `sd35-medium`
additionally requires accepting the model license on Hugging Face before the
weights can be downloaded at all; see
[self-hosting.md](self-hosting.md) for the `HF_TOKEN` procedure.

## Unrestricted product models (no Stability revenue cap)

| Model | License |
| --- | --- |
| sdxl-base, sdxl-fast (base weights) | CreativeML Open RAIL++-M |
| sdxl-fast (Lightning LoRA) | CreativeML Open RAIL++-M (openrail++ tag on the card) |
| ssd-1b | Apache 2.0 |
| vega-rt (base + VegaRT LCM LoRA) | Apache 2.0 |
| ssd-1b-lightning (SSD-1B + SDXL Lightning LoRA) | Apache 2.0 base + CreativeML Open RAIL++-M LoRA |
| realesrgan (RealESRGAN_x2plus / x4plus weights) | BSD-3-Clause (xinntao/Real-ESRGAN) |
| realesrgan-fast (realesr-general-x4v3 weights) | BSD-3-Clause (xinntao/Real-ESRGAN) |

Open RAIL licenses impose use restrictions (no illegal or harmful outputs) but
no annual revenue cap.
