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
| sd-turbo, sdxl-turbo | Stability AI Community | Benchmark reference |
| sdxl-hypersd | Open RAIL++-M base + Hyper-SD LoRA with NO declared license | Benchmark reference (issue #75); not promotable until ByteDance declares terms |
| vega-rt | Apache 2.0 | Benchmark reference (issue #75) |
| ssd-1b-lightning | Apache 2.0 base + Open RAIL++-M Lightning LoRA | Benchmark reference (issue #75; experimental) |

If you later offer any of these in the product, the obligations below apply.

## Stability AI Community License (sd-turbo, sdxl-turbo)

Applies while **you or your affiliates** generate **≤ USD $1,000,000** in
annual revenue (aggregate, from any source). Above that threshold the Community
License terminates and you must stop using these models or obtain an Enterprise
license from Stability AI.

**Before commercial use** (including offering these models in the hosted cloud):

1. Register at [stability.ai/community-license](https://stability.ai/community-license).
2. Comply with the [Stability AI Acceptable Use Policy](https://stability.ai/use-policy).

**When offering a product or service that uses these models**, the license also
requires:

- A **Notice** file distributed with copies, containing:
  `This Stability AI Model is licensed under the Stability AI Community License, Copyright © Stability AI Ltd. All Rights Reserved`
- **Prominent display** of **"Powered by Stability AI"** on the website, user
  interface, or product documentation.

## Unrestricted product models

| Model | License |
| --- | --- |
| sdxl-base, sdxl-fast (base weights) | CreativeML Open RAIL++-M |
| sdxl-fast (Lightning LoRA) | CreativeML Open RAIL++-M (openrail++ tag on the card) |
| ssd-1b | Apache 2.0 |
| dreamshaper-lcm | CreativeML Open RAIL-M |

Open RAIL licenses impose use restrictions (no illegal or harmful outputs) but
no annual revenue cap.
