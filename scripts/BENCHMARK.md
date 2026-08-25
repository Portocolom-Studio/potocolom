# Image generation benchmark (16 GB VRAM)

Curated prompt suite + parameter matrix for comparing models on a consumer GPU
(16 GB). Run with `make benchmark` after `make api` (with
`BENCHMARK_API=1`) and `make worker-rocm` are up.

Measured baselines and the optimization backlog live in [issue #60](https://github.com/portocolom-studio/potocolom/issues/60).

## Models in the default matrix (unrestricted commercial)

No annual revenue cap. Safe to publish on `/benchmark` without qualification.

| Model | License | VRAM | Resolution | Best for |
| --- | --- | --- | --- | --- |
| **sdxl-base** | Open RAIL++-M | ~8 GB tight | 768 / 1024 | Highest quality |
| **sdxl-fast** | Open RAIL++-M + Lightning LoRA | ~10 GB | 1024 | Near-SDXL quality, ~4 s |
| **ssd-1b** | Apache 2.0 | ~8 GB | 768 / 1024 | Speed/quality balance |
| **ssd-1b-lightning** | Apache 2.0 base + Open RAIL++-M LoRA | ~8 GB | 1024 | Fast batch @ 1024 (~2.8 s, issue #85) |
| **dreamshaper-lcm** | Open RAIL-M | ~4-6 GB | 512 / 768 | Illustration, stylized art |
| **vega-rt** | Apache 2.0 | ~8 GB | 512 / 1024 | Realtime drawing (issue #84) |

## License-safe turbo candidates (benchmark reference only)

Issue #75: evaluate Hyper-SD against the Stability turbo anchors. VegaRT was
promoted to the product in issue #84; the remaining rows stay
`benchmark_only: true` like sd-turbo and sdxl-turbo.

| Model | License | VRAM | Resolution | Notes |
| --- | --- | --- | --- | --- |
| **sdxl-hypersd** | Open RAIL++-M base; LoRA also CreativeML Open RAIL++-M | ~10 GB | 1024 | 8-step distillation LoRA; euler-trailing; benchmark-only on measurement, not on licensing |

```bash
# Issue #75 comparison run (smoke: 3 prompts, include Stability anchors)
make benchmark BENCHMARK_QUICK=1 BENCHMARK_INCLUDE_CAPPED=1 \
  BENCHMARK_MODELS=sdxl-hypersd,vega-rt,sdxl-fast,ssd-1b,sd-turbo,sdxl-turbo

# Full candidate sweep
make benchmark BENCHMARK_INCLUDE_CAPPED=1 \
  BENCHMARK_MODELS=sdxl-hypersd,vega-rt,sdxl-fast,ssd-1b,sd-turbo,sdxl-turbo

# The experimental combo runs alone: the harness aborts a run when a model
# fails to load, and a failed load is a possible (and valid) outcome here.
make benchmark BENCHMARK_MODELS=ssd-1b-lightning
```

Measured numbers decide whether a follow-up PR promotes a winner to the product
manifests (realtime capability, `min_vram_gb` from measurement).

### Issue #75 decision (RX 7600 XT, 2026-07-14)

Quick run: 3 prompts, 1 variant each (`BENCHMARK_QUICK=1`), models
`sdxl-hypersd`, `vega-rt`, `sdxl-fast`, `ssd-1b`, `sd-turbo`, `sdxl-turbo`.
Authoritative raw output: `data/benchmark/issue-75-rerun/` (gitignored). An earlier
`issue-75-run/` pass had inflated cold-start times while `ai-assistant-ollama`
held ~14 GB VRAM; stop competing GPU workloads before benchmarking.

| Model | Median gpu_ms | Resolution / steps | Verdict |
| --- | ---: | --- | --- |
| sd-turbo | 345 | 512 / 2 | Benchmark anchor (Stability cap) |
| sdxl-turbo | 296 | 512 / 1 | Benchmark anchor (Stability cap) |
| **vega-rt** | **381** | 512 / 2 (t2i) | Promoted in #84 (license-clean realtime) |
| sdxl-fast | 4005 | 1024 / 8 | Open baseline (Lightning) |
| sdxl-hypersd | 3987 | 1024 / 8 | Benchmark only - on par with sdxl-fast, no speed win |
| ssd-1b | 10034 | 1024 / 20 | Batch tier, not realtime |

**Conclusion:** VegaRT (Apache 2.0) matches turbo-class t2i latency at 512 px
(~381 ms median vs ~345 ms sd-turbo) and was promoted in #84. Hyper-SD stays
benchmark-only because ~4 s @ 1024 is on par with Lightning, not a turbo-class
win. The LoRA license is not the reason: it is CreativeML Open RAIL++-M, the
same family as the Lightning LoRA already shipped
([docs/third-party-models.md](../docs/third-party-models.md)). Stability
Community models remain the capped commercial anchors.

### Full-suite rerun (RX 7600 XT, 2026-07-16, published)

The authoritative published dataset: 24 prompts x 5 variants across all nine
models (1080 images, 0 failures), run with depth-2 dispatch on main and
recorded in the persistent metrics store. Highlights: vega-rt 512/2 median
263 ms (fastest in the suite), sd-turbo 292, sdxl-turbo 405, ssd-1b-lightning
2168 median @ 1024/8 vs sdxl-fast 3757. Raw output:
`data/benchmark/full-rerun-20260716` plus the vega-rt rerun merged into
`full-rerun-20260716-combined` (gitignored).

Lesson recorded: the original vega-rt pass failed 117/120 with an fp16/fp32
dtype mismatch - Segmind-Vega inherits SDXL's stock VAE, which force-upcasts
at decode, and the manifest was missing the `madebyollin/sdxl-vae-fp16-fix`
override every other SDXL-family manifest carries. 512-only smoke runs never
trip the upcast: benchmark new SDXL-family models at 1024 at least once
before promoting. The fp16 VAE also cut the 512/2 median from 381 to 263 ms.

**ssd-1b-lightning** solo run (`data/benchmark/ssd-1b-lightning-run/`, clean GPU,
2026-07-14): **load succeeded** - SDXL Lightning LoRA fuses onto the pruned
SSD-1B UNet. 3/3 @ 1024/8step, median **2777 ms** gpu_ms (vs ~10 s for plain
ssd-1b @ 1024/20, vs **4005 ms** for sdxl-fast in the #75 clean rerun).
**Promoted in #85** alongside sdxl-fast: comparable quality on the shared
three-prompt suite, ~31% faster, same Lightning LoRA licensing as sdxl-fast.
Batch tier only - not realtime.

### Issue #84 realtime frame gate (RX 7600 XT, 2026-07-14)

Clean GPU (~13.4 GB free VRAM). `engine.frame` img2i path @ 512 px, strength
0.7, after model load:

| Metric | Value |
| --- | ---: |
| Frame 1 (warmup) | 669 ms |
| Frames 2-5 | 454, 452, 444, 442 ms |
| **Warm median** | **452 ms (~2.2 fps)** |

Within the M3 realtime bar (2-4 fps). `model_timings.json` keeps the t2i
baseline (381 ms @ 512/2) for job estimates; realtime frames are img2i and run
slightly slower.

## Capped commercial models (benchmark reference only)

Models under a **$1M annual revenue cap** (Stability AI Community License).
They live in `capped_commercial` in `benchmark-matrix.json`
and in `worker/models/` with **`benchmark_only: true`** - the worker can run them
for benchmarks, but **`GET /api/v1/models` hides them** so users cannot select
them in the app.

| Model | License | VRAM | Notes |
| --- | --- | --- | --- |
| **sd-turbo** | Stability Community | ~8 GB | ~290 gpu_ms @ 512 (issue #60) |
| **sdxl-turbo** | Stability Community | ~10 GB | ~310 gpu_ms @ 512 |

Timings from these models **can appear on `/benchmark`** (reference hardware
metrics). They are **not offered** in the studio UI.

```bash
# Capped models only (smoke)
make benchmark BENCHMARK_MODELS=sd-turbo BENCHMARK_IDS=1-3

# Full unrestricted + capped
make benchmark BENCHMARK_INCLUDE_CAPPED=1
```

License obligations if you ever ship them to users: [docs/third-party-models.md](../docs/third-party-models.md).

## Apache candidates under evaluation (2026-08)

Research snapshot and full license analysis: local
`.local/model-research-2026-08.md` (gitignored). All three candidates carry no
revenue cap, no registration, and no attribution requirement, unlike every row
above. `flux2-klein-4b` and `z-image-turbo` are Apache 2.0 end to end.
`sana-sprint-06b` is Apache 2.0 for the weights and the code, but its
Gemma-2-2B-IT text encoder is governed by the Google Gemma Terms of Use and
Prohibited Use Policy: no revenue cap, but use restrictions in the RAIL mould.
They ship as `benchmark_only: true` manifests and live in a `candidates` group
in `benchmark-matrix.json`, reachable only through an explicit `--models`
filter.

| Model | Params | Steps | VRAM (manifest) | Notes |
| --- | --- | --- | --- | --- |
| **flux2-klein-4b** | 4B + Qwen3-4B encoder | 4 distilled | 13 GB BF16 | Unified FLUX.2 family; editing/i2i path deliberately not declared yet |
| **z-image-turbo** | 6B S3-DiT | 8 NFEs | 20 GB BF16 (offload rung on a 16 GB card) | Photorealism focus; native img2img pipeline; no CFG |
| **sana-sprint-06b** | 0.6B DiT + Gemma-2-2B-IT encoder | 1-4, step-adaptive | 10 GB BF16 | DC-AE 32x latent compression; linear attention; 1024 px checkpoints only |

`sana-sprint-06b` additionally declares `"pipeline": "SanaSprint"`. The engine
resolves a manifest's pipeline classes through `AutoPipelineFor*` by default,
and diffusers 0.39 has no mapping entry for `SanaSprintPipeline`, so
`AutoPipeline` raises for it while `ZImagePipeline` and `Flux2KleinPipeline`
resolve fine. The stem names the class family, and the engine appends
`Pipeline` and `Img2ImgPipeline`. `test_autopipeline_cannot_resolve_sana_sprint`
guards the premise: when diffusers adds the mapping, that test fails and the
override can be deleted.

The other two are bf16-native flow transformers: their manifests declare
`"dtype": "bfloat16"` so `_from_pretrained` loads them without the fp16 cast.
The `dtype` manifest field is worker-side only and never crosses the wire.
Their text encoders are Qwen3, not CLIP: the declared 512-token window matches
the pipelines' `max_sequence_length` so the studio warns past it, and
`_prompt_kwargs` lets diffusers encode whenever an encoder is outside the CLIP
family instead of applying the CLIP chunker.

Run these on a clear GPU, and never at the default `auto` memory mode on the
reference card. On 2026-08-24 `profile-candidates.py --models flux2-klein-4b`
at 1024 px ran for over four hours, produced no image, held 16.85 GB of a
16 GB card that also drives the display, and wedged the amdgpu driver in an
unkillable D state that only a reboot cleared. The rung is not decided once at
load: `_demote_rung` slides `full -> model_offload -> group_offload` on OOM, so
a model that picks `full` can still end up streaming every leaf module from
disk mid-run. `profile-candidates.py` therefore defaults to
`--memory-mode full`, which pins the rung so an oversized model raises instead
of degrading, and it refuses outright if a run still lands on `group_offload`.
One model failing no longer cancels the others in the same invocation.

Declared `min_vram_gb` against roughly 16 GB free tells you what to expect:
`sana-sprint-06b` (10) and `vega-rt` (8) select `full`, `z-image-turbo` (20)
selects `model_offload`, and `flux2-klein-4b` (13) selects `full` but is the
one that OOMed and degraded in practice.

The profiler also checks free VRAM before each phase and skips that phase if
under 2 GB remain. The engine's own guard cannot cover this: `_ensure_vram`
sizes a model by `min_vram_gb`, which describes weights at rest with no term
for resolution or activations, it only evicts and never refuses, and under a
pinned `--memory-mode` it does not run at all. Phases are also banked as they
complete, so a phase that still runs out of memory costs its own numbers
rather than the whole model's.

Two candidate-specific traps, both `sana-sprint-06b`:

- `guidance` is the model's own embedded guidance, not CFG, so the manifest
  defaults it to 4.5. The other distilled candidates default it to 0. Do not
  "correct" it to 0; that is not the no-CFG case, it is a different conditioning
  value.
- 1024 is the only resolution it has. `use_resolution_binning` maps a request
  onto `ASPECT_RATIO_1024_BIN`, a 512x512 request bins to 1024x1024, and the
  whole table holds no entry below 704x1344. There is no 512 or 768 checkpoint
  either, so the manifest offers 1024 alone. `_generate_i2i` now passes the
  size to any pipeline whose signature accepts one, and omits it for SD and
  SDXL img2img which take neither, but that cannot defeat the binning: forcing
  512 would need `use_resolution_binning=False` and would run the model off
  the distribution it was trained on.

```bash
# Latency + VRAM envelope per rung, no API needed (engine-direct):
worker/.venv/bin/python scripts/profile-candidates.py \
  --models flux2-klein-4b,z-image-turbo,sana-sprint-06b \
  --save-dir /tmp/candidate-samples

# Queued-job quality suite through the running stack:
make benchmark BENCHMARK_MODELS=flux2-klein-4b,z-image-turbo,sana-sprint-06b \
  BENCHMARK_QUICK=1
```

### Measured: sana-sprint-06b does not beat vega-rt (2026-08-25)

First numbers on the reference RX 7600 XT, `--memory-mode full`, weights cached,
clear GPU:

| Phase | Result |
| --- | --- |
| load | 13.39 s, rung `full`, 15.93 GB free before |
| t2i 1024 / 2 steps / guidance 4.5 | **1469 ms median**, 1470 ms p95, peak 11.97 GB |
| i2i (frame analog) | OOM: wanted 4.50 GiB with 14.79 GiB already resident |

`vega-rt` is 219.8 ms p95 at 512 with TAESD and a 452 ms warm realtime frame.
SANA-Sprint is **6.7x** the former and **3.2x** the latter, at a resolution it
cannot go below. Even a hypothetical 4x saving at 512 lands near 367 ms, still
no win, and 512 is not reachable for this model anyway. The DC-AE latent-token
argument (a 1024 px image is a 32x32 latent against vega-rt's 64x64 at 512) did
not survive contact with the hardware.

The published 0.31 s on an RTX 4090 against 1469 ms here is a 4.7x gap, which is
in the plausible range for the two cards and does not need another explanation.

Second finding: image-to-image does not fit on this card at all. t2i peaks at
11.97 GB, and i2i adds a VAE encode of the source on top, which is the 4.50 GiB
that fails. So `sana-sprint-06b` stays `benchmark_only`, and is not a realtime
candidate on 16 GB hardware. Retest only on a card with materially more VRAM.

Promotion bar, same rule as #75/#84: measured frame-analog p95 under the
500 ms realtime bar at 512 px plus acceptable i2i quality is what earns a
follow-up PR that flips capabilities to include `realtime` and re-measures
`min_vram_gb`. For flux2-klein-4b that PR also decides the conditioning path
(reference-image editing vs latent strength blending) before any `realtime`
or `image_to_image` capability ships.

## Execution flow

1. **Preflight** - `GET /api/v1/benchmark/gpu`. If any model is resident, abort
   (or pass `--force` to unload first).
2. **Per model** - explicit load → all prompts × variants → explicit unload.
3. **Cleanup** - unload anything still on the GPU (even after errors).
4. **Summarize** - `results.json`, `report.md`, `report.html` with `load_ms`,
   `gpu_ms`, and `wall_s` per image.
5. **Persist** - after the artifacts are written, best-effort POST the complete
   report to `/api/v1/benchmark/sessions` on the same running install.

Load and unload go through the API → fleet socket → worker `DiffusersEngine`,
so the benchmark controls VRAM instead of relying on lazy load / OOM eviction.

First-time model pulls download from Hugging Face and can stall the log for
several minutes with no new lines - watch the worker terminal or HF cache size.

## Models excluded entirely

| Model | Why excluded |
| --- | --- |
| **FLUX.1 Schnell** | Apache 2.0 (good license) but needs 12-16 GB+ or memory ladder (#15) |
| **FLUX.1 Dev** | Non-commercial license |
| **SD 3.5 Medium/Large** | Community License ($1M cap) or 12 GB+ VRAM |

When the worker memory ladder lands, FLUX.1 Schnell becomes the top candidate to
add - it is the planned quality ceiling on the license shortlist (see ROADMAP).

## Output layout

```
data/benchmark/<timestamp>/
  images/
    01-rain-soaked-neon-alley/
      sdxl-base__1024-default.webp
      ...
  results.json      # machine-readable run log
  report.html       # visual comparison grid
  report.md         # summary tables for diffs and notes
```

Publish to the frontend after a run completes:

```bash
make benchmark-publish
# default source: data/benchmark/full-run
```

## Quick runs

Full matrix: 24 prompts × 4 models × 5 variants = **480 images** (~hours on GPU).

```bash
# Smoke: 3 prompts, 1 variant each, all default-matrix models
make benchmark BENCHMARK_QUICK=1

# Subset of prompts
make benchmark BENCHMARK_IDS=1,4,10

# Single model
make benchmark BENCHMARK_MODELS=sdxl-fast,ssd-1b
```

## References

- [Issue #60 - Inference speed baseline and backlog](https://github.com/portocolom-studio/potocolom/issues/60)
- [Issue #75 - License-safe turbo candidates (Hyper-SD, VegaRT)](https://github.com/portocolom-studio/potocolom/issues/75)
- Measured timings on RX 7600 XT in ROADMAP.md
- VRAM guidance from Stability AI / Black Forest Labs docs, Hugging Face model cards, and community tables
