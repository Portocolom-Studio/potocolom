# Third-party model licenses

potocolom ships model manifests that point at weights hosted on Hugging Face.
Each model is governed by its upstream license. This file summarizes obligations
for models bundled in `worker/models/`. It is not legal advice.

## What each model is pinned to

Every repository a manifest names is pinned to a commit, so the license terms
below describe weights that do not change under the installation reading them
(issue #319). A manifest carries a `<field>_revision` beside each reference, and
a manifest that names a repository without a sha does not load.

One repository is pinned to one commit across the fleet: seven manifests share
the fp16 VAE and three share the SDXL base, and pinning one copy without its
siblings would put two revisions of one repository in one worker.

| Repository | Commit | Manifests |
| --- | --- | --- |
| `black-forest-labs/FLUX.2-klein-4B` | `e7b7dc27f91deacad38e78976d1f2b499d76a294` | flux2-klein-4b |
| `ByteDance/Hyper-SD` | `bc08d970a87c74c71209491d64e3525845698863` | sdxl-hypersd |
| `ByteDance/SDXL-Lightning` | `c9a24f48e1c025556787b0c58dd67a091ece2e44` | sdxl-fast, ssd-1b-lightning |
| `Efficient-Large-Model/Sana_Sprint_0.6B_1024px_diffusers` | `aa76e7f4f4928f378716b6716a2130fba3caf5b1` | sana-sprint-06b |
| `Lykon/dreamshaper-8-lcm` | `4645d8bc6a8e6b106d21606d63e8460cdad4f1a6` | dreamshaper-lcm |
| `madebyollin/sdxl-vae-fp16-fix` | `207b116dae70ace3637169f1ddd2434b91b3a8cd` | sdxl-base, sdxl-fast, sdxl-hypersd, sdxl-turbo, ssd-1b, ssd-1b-lightning, vega-rt |
| `madebyollin/taesdxl` | `b20258aaef75ef61e659c1e0f14f251cf0ad153e` | sdxl-turbo, vega-rt |
| `segmind/Segmind-Vega` | `7714c4363e5856ff974a4f4b068e8691f26d0b40` | vega-rt |
| `segmind/Segmind-VegaRT` | `3162e917016cef536fa040ceeeeadd9e09d93e7a` | vega-rt |
| `segmind/SSD-1B` | `60987f37e94cd59c36b1cba832b9f97b57395a10` | ssd-1b, ssd-1b-lightning |
| `stabilityai/sd-turbo` | `b261bac6fd2cf515557d5d0707481eafa0485ec2` | sd-turbo |
| `stabilityai/sdxl-turbo` | `71153311d3dbb46851df1931d3ca6e939de83304` | sdxl-turbo |
| `stabilityai/stable-diffusion-3.5-medium` | `b940f670f0eda2d07fbb75229e779da1ad11eb80` | sd35-medium |
| `stabilityai/stable-diffusion-xl-base-1.0` | `462165984030d82259a11f4367a4eed129e94a7b` | sdxl-base, sdxl-fast, sdxl-hypersd |
| `TencentARC/t2i-adapter-sketch-sdxl-1.0` | `cc3c4e3362296c6825c370b83838306723ece983` | sdxl-turbo, vega-rt |
| `Tongyi-MAI/Z-Image-Turbo` | `f332072aa78be7aecdf3ee76d5c247082da564a6` | z-image-turbo |

`realesrgan` and `realesrgan-fast` are absent because they fetch a release URL,
which already names its own version and has no commit to pin.

Moving a model means editing the sha and committing that, which is the point:
see "A model is pinned to a commit, and moving it costs a commit" in
[decisions.md](decisions.md).

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
