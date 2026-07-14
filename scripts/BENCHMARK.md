# Image generation benchmark (8 GB VRAM)

Curated prompt suite + parameter matrix for comparing models on a consumer GPU
(RX 7600 class, 8 GB). Run with `make benchmark` after `make api` (with
`BENCHMARK_API=1`) and `make worker-rocm` are up.

Measured baselines and the optimization backlog live in [issue #60](https://github.com/portocolom-studio/potocolom/issues/60).

## Models in the default matrix (unrestricted commercial)

No annual revenue cap. Safe to publish on `/benchmark` without qualification.

| Model | License | VRAM | Resolution | Best for |
| --- | --- | --- | --- | --- |
| **sdxl-base** | Open RAIL++-M | ~8 GB tight | 768 / 1024 | Highest quality |
| **sdxl-fast** | Open RAIL++-M + Lightning LoRA | ~10 GB | 1024 | Near-SDXL quality, ~4 s |
| **ssd-1b** | Apache 2.0 | ~8 GB | 768 / 1024 | Speed/quality balance |
| **dreamshaper-lcm** | Open RAIL-M | ~4-6 GB | 512 / 768 | Illustration, stylized art |

## License-safe turbo candidates (benchmark reference only)

Issue #75: evaluate Hyper-SD and VegaRT against the Stability turbo anchors before
promoting either to the studio. Manifests set `benchmark_only: true` like
sd-turbo and sdxl-turbo.

| Model | License | VRAM | Resolution | Notes |
| --- | --- | --- | --- | --- |
| **sdxl-hypersd** | Base Open RAIL++-M; LoRA has NO declared license | ~10 GB | 1024 | 8-step distillation LoRA; euler-trailing; measure only, not promotable as-is |
| **vega-rt** | Apache 2.0 | ~8 GB | 512 / 1024 | Segmind-Vega + VegaRT LCM LoRA |
| **ssd-1b-lightning** | Apache 2.0 base + Open RAIL++-M LoRA | ~8 GB | 1024 | Experimental combo; load may fail |

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
| **vega-rt** | **381** | 512 / 2 | **License-clean turbo-class winner** - follow-up PR to promote |
| sdxl-fast | 4005 | 1024 / 8 | Open baseline (Lightning) |
| sdxl-hypersd | 3987 | 1024 / 8 | Benchmark only - LoRA has no declared license |
| ssd-1b | 10034 | 1024 / 20 | Batch tier, not realtime |

**Conclusion:** No product manifest in #75. VegaRT (Apache 2.0) matches
turbo-class latency at 512 px (~381 ms median vs ~345 ms sd-turbo) and is the
candidate for a follow-up promotion PR with `realtime` capability and measured
`min_vram_gb`. Hyper-SD stays benchmark-only (license gap on the LoRA weights;
~4 s @ 1024 is on par with Lightning, not a license-clean turbo win). Stability
Community models remain the capped commercial anchors.

**ssd-1b-lightning** was not run in this sweep; run it alone if the combo
mapping question still needs a recorded fail/pass.

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

## Execution flow

1. **Preflight** - `GET /api/v1/benchmark/gpu`. If any model is resident, abort
   (or pass `--force` to unload first).
2. **Per model** - explicit load → all prompts × variants → explicit unload.
3. **Summarize** - `results.json`, `report.md`, `report.html` with `load_ms`,
   `gpu_ms`, and `wall_s` per image.
4. **Cleanup** - unload anything still on the GPU (even after errors).

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
