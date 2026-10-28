# Image generation benchmark (16 GB VRAM)

Curated prompt suite + parameter matrix for comparing models on a consumer GPU
(16 GB). Run with `make benchmark` after `make api` (with
`BENCHMARK_API=1`) and `make worker-rocm` are up.

Measured baselines and the optimization backlog live in [issue #60](https://github.com/portocolom-studio/potocolom/issues/60).

## Current suite (2026-10)

The catalog has 36 prompts. Each model has five parameter variants.
The default matrix runs five models (900 images). Add capped models and all
candidates explicitly for the full 12-model suite (2,160 planned images).
Candidates remain hidden from the studio until their GPU results are reviewed.

| Model | License | Commercial use |
| --- | --- | --- |
| `sdxl-base`, `sdxl-fast`, `sdxl-hypersd` | CreativeML Open RAIL++-M, including the Lightning/Hyper-SD LoRA | Yes, with the use restrictions |
| `ssd-1b` | Apache 2.0 | Yes |
| `ssd-1b-lightning` | Apache 2.0 base + Open RAIL++-M LoRA | Yes, with the LoRA use restrictions |
| `sd35-medium` | Stability AI Community License | Yes below the $1M revenue cap, with its terms |
| `flux2-klein-4b`, `z-image-turbo` | Apache 2.0 | Yes |
| `moss-image-sft`, `kromoss`, `iris-3b` | Apache 2.0 | Yes |
| `flux3-klein` | FLUX Kommunity v1 | Output use below $5M revenue, with filtering or review and legal AI notices; other commercial uses need separate rights |

Exact weight pins, encoder licenses and obligations are in
[third-party-models.md](../docs/third-party-models.md).

`dreamshaper-lcm`, `sana-sprint-06b`, `sd-turbo`, `sdxl-turbo` and `vega-rt` are removed
from the active catalog. Their old measurements below remain historical evidence.

```bash
# Short smoke run: three prompts and one variant per model.
make benchmark BENCHMARK_QUICK=1 BENCHMARK_IDS=1-3 BENCHMARK_INCLUDE_CAPPED=1

# Full suite: all prompts and variants. Keep a new output directory per run.
make benchmark BENCHMARK_CONTINUE=1 BENCHMARK_INCLUDE_CAPPED=1 \
  BENCHMARK_MODELS=sdxl-base,sdxl-fast,ssd-1b,sdxl-hypersd,ssd-1b-lightning,sd35-medium,flux2-klein-4b,z-image-turbo,moss-image-sft,kromoss,iris-3b,flux3-klein
```

Use full residency for the small cached models. Use `MEMORY_MODE=model_offload`
for large candidates on the 16 GB display GPU. Do not let a run enter disk group
offload. Reserve display memory and monitor actual free VRAM throughout the run.
A manifest floor is an estimate, not a memory cap.

`BENCHMARK_QUICK=1` selects the first variant of each model. The candidate
models start with `1024-default`, which exceeded the display reserve for
several models in this run. It does not select a smaller image size.

The published capacity report used an external operator controller that watched
device memory, cancelled batches at the display reserve, and marked remaining
cases at that resolution as skipped. That controller is not bundled here.
`make benchmark` runs the matrix without that guard and records successful or
failed attempts; it cannot reproduce the report's capacity skips on its own.
The native sessions API accepts only those two states. The capacity report is
published as a static report and is not ingested into native session history.

The `gpu_ms` field measures the engine call. It is not a GPU kernel timer.
CPU encoding and component transfers can be inside that interval for the new
adapters. Use `wall_s` for the end-to-end comparison, and record the memory mode.

Master image downloads use `.png`, matching the API's PNG output. Earlier
runs saved PNG bytes under `.webp` names. Historical files keep their original
paths; published example copies use the correct extension without re-encoding.

## Historical measurements

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

## Candidate profiling

The matrix stores Stability models under `capped_commercial` and the other
candidates under `candidates`. Explicit `--models` selects candidates.
The new MOSS, Kromoss, Iris and FLUX 3 adapters support queued text-to-image only.
Their full-residency estimates are 32, 32, 22 and 32 GiB. Their component-offload
estimates are 12, 12, 10 and 12 GiB. GPU tests must confirm these estimates.

MOSS uses its Qwen encoder on CPU, then runs the generator and VAE in separate
GPU stages. Iris stores its generator in BF16 and uses the pinned Qwen3-VL
encoder. FLUX 3 uses the pinned modular Diffusers API without its optional
prompt upsampler; give it a full scene description.

```bash
# Engine-direct latency and VRAM checks. Pinned offload avoids disk fallback.
worker/.venv/bin/python scripts/profile-candidates.py \
  --models moss-image-sft,kromoss,iris-3b,flux3-klein \
  --memory-mode model_offload --save-dir /tmp/candidate-samples

# API smoke test, one variant for each of three prompts.
make benchmark BENCHMARK_MODELS=moss-image-sft,kromoss,iris-3b,flux3-klein \
  BENCHMARK_QUICK=1 BENCHMARK_IDS=1-3 BENCHMARK_CONTINUE=1
```

A model needs actual GPU output and a measured memory limit before promotion.
A text-to-image result does not prove image editing or realtime support.

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
2. **Per model** - explicit load, all prompts and variants, explicit unload.
3. **Cleanup** - unload anything still on the GPU (even after errors).
4. **Summarize** - `results.json`, `report.md`, `report.html` with `load_ms`,
   `gpu_ms`, and `wall_s` per image.
5. **Persist** - after the artifacts are written, best-effort POST the complete
   report to `/api/v1/benchmark/sessions` on the same running install.

Load and unload go through the API, fleet socket and worker `DiffusersEngine`,
so the benchmark controls VRAM instead of relying on lazy load / OOM eviction.

First-time model pulls download from Hugging Face and can stall the log for
several minutes with no new lines - watch the worker terminal or HF cache size.

## Models outside this suite

FLUX.1 Schnell and FLUX.1 Dev are not active manifests. SD 3.5 Large is also
outside the suite. SD 3.5 Medium is included under `capped_commercial`.

## Output layout

```
data/benchmark/<timestamp>/
  images/
    01-rain-soaked-neon-alley/
      sdxl-base__1024-default__1024x1024-s20.png
      ...
  results.jsonl     # durable rows, written after each completed or failed cell
  results.json      # final report, written after the run completes
  report.html       # visual comparison grid
  report.md         # summary tables for diffs and notes
```

Publish to the frontend after a run completes:

```bash
make benchmark-publish
# default source: data/benchmark/full-run
```

## Quick runs

Full explicit matrix: 36 prompts x 12 models x 5 variants = **2,160 planned images**.
A failed load records failed cells with no timing. `--continue-on-error` keeps
running after load or generation errors. Cleanup stays strict: if GPU state is
unknown, the run stops and `results.jsonl` preserves the completed work.

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
