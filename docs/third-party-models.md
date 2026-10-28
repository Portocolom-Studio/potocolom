# Third-party model licenses

potocolom ships model manifests that point at weights hosted on Hugging Face.
Each model is governed by its upstream license. This file summarizes obligations
for models bundled in `worker/models/`. It is not legal advice.

## What each model is pinned to

Every repository a manifest names is pinned to a commit, so the license terms
below describe weights that do not change under the installation reading them
(issue #319). A manifest carries a `<field>_revision` beside each reference, and
a manifest that names a repository without a sha does not load.

One repository uses one commit across the fleet. Manifests that share a
repository also share its pin.

| Repository | Commit | Manifests |
| --- | --- | --- |
| `OpenMOSS-Team/MOSS-Image-SFT` | `f972bd4b4b9e3852133cc5da954d87bc32cd8fb9` | moss-image-sft |
| `lodestones/Kromoss` | `a4fe97449297af2e3be7841e1c18e431f3b06031` | kromoss |
| `speridlabs/iris-3b` | `adc05b909fb05d4276d26aae5a52b36406b2ef26` | iris-3b |
| `black-forest-labs/flux-3-image-klein` | `337337621b8526e120f2275d250242f4a6bedd75` | flux3-klein |
| `black-forest-labs/FLUX.2-klein-4B` | `e7b7dc27f91deacad38e78976d1f2b499d76a294` | flux2-klein-4b |
| `ByteDance/Hyper-SD` | `bc08d970a87c74c71209491d64e3525845698863` | sdxl-hypersd |
| `ByteDance/SDXL-Lightning` | `c9a24f48e1c025556787b0c58dd67a091ece2e44` | sdxl-fast, ssd-1b-lightning |
| `google/siglip2-base-patch16-224` | `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2` | output categorizer (worker/worker/categorize.py) |
| `madebyollin/sdxl-vae-fp16-fix` | `207b116dae70ace3637169f1ddd2434b91b3a8cd` | sdxl-base, sdxl-fast, sdxl-hypersd, ssd-1b, ssd-1b-lightning |
| `segmind/SSD-1B` | `60987f37e94cd59c36b1cba832b9f97b57395a10` | ssd-1b, ssd-1b-lightning |
| `stabilityai/stable-diffusion-3.5-medium` | `b940f670f0eda2d07fbb75229e779da1ad11eb80` | sd35-medium |
| `stabilityai/stable-diffusion-xl-base-1.0` | `462165984030d82259a11f4367a4eed129e94a7b` | sdxl-base, sdxl-fast, sdxl-hypersd |
| `Tongyi-MAI/Z-Image-Turbo` | `f332072aa78be7aecdf3ee76d5c247082da564a6` | z-image-turbo |

`realesrgan` and `realesrgan-fast` are absent because they fetch a release URL,
which already names its own version and has no commit to pin. The categorizer row is
not a manifest either: the worker's output categorizer names its weights and
pins them in code (worker/worker/categorize.py), under the same full-sha rule.

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
| flux2-klein-4b | Apache 2.0 | Benchmark only; not in the studio picker |
| z-image-turbo | Apache 2.0 | Benchmark only; 20 GB floor; not in the studio picker |
| moss-image-sft, kromoss | Apache 2.0 | Queued text-to-image candidates; hidden until GPU output and memory checks pass |
| iris-3b | Apache 2.0 | Queued text-to-image candidate; CPU text encoding on the offload rung |
| flux3-klein | FLUX Kommunity v1.0 | Gated evaluation candidate; commercial-output exception has conditions |

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

## Stability AI Community License (sd35-medium)

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
| ssd-1b-lightning (SSD-1B + SDXL Lightning LoRA) | Apache 2.0 base + CreativeML Open RAIL++-M LoRA |
| realesrgan (RealESRGAN_x2plus / x4plus weights) | BSD-3-Clause (xinntao/Real-ESRGAN) |
| realesrgan-fast (realesr-general-x4v3 weights) | BSD-3-Clause (xinntao/Real-ESRGAN) |

Open RAIL licenses impose use restrictions (no illegal or harmful outputs) but
no annual revenue cap.

## Output categorizer license (SigLIP 2 base)

| Model | License |
| --- | --- |
| output categorizer (`google/siglip2-base-patch16-224`) | Apache 2.0 |

The worker's output categorizer loads its weights through transformers
directly, not through a manifest (see the pin table above). Apache-2.0 is
declared in the weight repository's card, the card carries no restriction
sentence, and there is no revenue cap and no attribution requirement. The
repository ships no LICENSE file, so the card front matter is the licence
evidence, the same convention `license_url` follows for `flux2-klein-4b`.

Training data is separate from the weights: the card states no licence for
the WebLI corpus the model was trained on.

## MOSS-Image, Kromoss, and Iris-3B

The MOSS-Image SFT and Kromoss repositories publish their weights and inference
code under Apache 2.0. Their Qwen3.5-9B text encoder is also Apache 2.0.
Iris-3B and its Qwen3-VL-4B-Instruct text encoder use Apache 2.0. Commercial
hosting, modification, and redistribution are permitted without a revenue cap.
Distribution must retain the license and required notices and identify changed
files. Trademark rights are not granted.

The Iris inference source is vendored from
`speridlabs/iris-3b` commit `13fcf6a4f7fcb36561eac3daa5ad30d0b0a1822e`.
Its LICENSE, NOTICE, and source list are in `worker/vendor/iris3b/`.
The adapter pins the separate encoder to
`Qwen/Qwen3-VL-4B-Instruct` commit `ebb281ec70b05090aa6165b016eac8ec08e71b17`.
MOSS custom pipeline code uses the same repository and revision as its weights.

Sources: [MOSS license](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT/blob/f972bd4b4b9e3852133cc5da954d87bc32cd8fb9/LICENSE),
[Kromoss license statement](https://huggingface.co/lodestones/Kromoss/blob/a4fe97449297af2e3be7841e1c18e431f3b06031/README.md),
[Iris notice](https://github.com/speridlabs/iris-3b/blob/13fcf6a4f7fcb36561eac3daa5ad30d0b0a1822e/NOTICE),
and [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B).

## FLUX 3 Image Klein

FLUX 3 uses the FLUX Kommunity License v1.0. It is not Apache 2.0.
Section 2.a permits production use to generate commercial outputs only for
qualifying users. Section 1.e defines that limit as less than USD 5,000,000 in
gross annualized revenue, counting affiliates. This is an exclusive threshold.
The manifest carries the threshold for display; it does not enforce the license.

Section 2.e requires content filters or output review and legally required AI
disclosures. The commercial-output exception does not grant general commercial
rights to create or distribute modified models. Other uses require a separate
agreement. Section 4 also restricts uses, including using outputs to improve a
similar model. Distribution has license and notice duties under section 3.

The bundled manifest is an evaluation candidate. It does not enable a paid
production path or supply the controls required for that path. Hugging Face
access requires the operator to accept the upstream terms. The worker reads
`HF_TOKEN`; it does not accept terms on the operator's behalf.

Source: [exact pinned FLUX license](https://huggingface.co/black-forest-labs/flux-3-image-klein/blob/337337621b8526e120f2275d250242f4a6bedd75/LICENSE.md).
