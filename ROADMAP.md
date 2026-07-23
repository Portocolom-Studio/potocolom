# Roadmap

A living snapshot of where the project stands and what comes after. Issues and
milestones remain the planning source of truth; this file is the readable
summary. Decisions and their rejected alternatives live in
[docs/decisions.md](docs/decisions.md).

Refreshed 2026-07-23 to cover the upscale, metrics and relicense work merged
after the M2 snapshot. The GitHub issues and milestones remain the authoritative,
grouped (M2-M8) view of what is open.

> Correction: an earlier version of this file listed worker thumbnails (#56),
> lineage columns (#57), self-hosted packaging (#18) under "To close M2" and the
> job watchdog (#61) under "deferred". All four actually shipped (PRs #78/#81/#77,
> 2026-07-14) and are now under Done.

## Done

### Platform (backend)

- Configuration profile (#14): storage, database and public URL settings read
  once at startup. (`GET /api/v1/config` exists; the shipped SPA does not yet
  consume it.)
- Storage seam (#17): one interface, two implementations. LocalStorage uploads
  and serves through `/api/v1/files/{key}`; S3Storage presigns PUT and GET
  (SigV4, MinIO in development). Workers receive an upload target with each job
  and never talk to storage implementations directly.
- Database layer: async SQLAlchemy with alembic migrating on startup; users,
  models, jobs and assets from the architecture data model. A down database
  degrades the API (row endpoints answer 503) instead of failing the health check.
- Job dispatch and history (#16): `POST /api/v1/generations` validates params
  against the model's JSON Schema, queues on the Queues seam (in-process; the
  cloud profile swaps in Redis sorted sets), and dispatches over the fleet socket.
  Row locks serialize job state, a lost worker retries a job exactly once, the
  queue rebuilds from PostgreSQL on restart. Progress streams over SSE
  (`/api/v1/generations/{id}/events`) and rides along in history responses.
  History is `GET /api/v1/generations` (list, cursor paging).
- Model registry (#11): manifests travel in the fleet hello, persist for history
  foreign keys, and `GET /api/v1/models` serves them with schema, capabilities,
  default flag and GPU-time estimates.
- Job execution watchdog (#61): `sweep_stalled_jobs` fails or requeues jobs stuck
  in `running` when a connected worker stops reporting progress.
- Worker thumbnails and generation lineage (#56, #57): WebP thumbnails beside
  originals, and a `parent_asset_id` lineage column (the foundation the future
  lineage-canvas feature builds on).
- Per-model GPU-time estimates (#47): `estimated_gpu_ms_default` on every model,
  plus `estimated_gpu_ms_by_factor` on upscale models, scaled by steps and pixels.
- Self-hosted distribution (#18): docker images and a one-file compose stack;
  local storage served over `/api/v1/files/{key}`.
- Metrics and pipeline reliability: persisted GPU heartbeat samples with a
  time-range endpoint `GET /api/v1/metrics/gpu/history` and 5-minute rollups
  (#94), pipeline phase timings (#96), pipeline overlap via dispatch depth 2
  (#97), poison-pipeline eviction after non-OOM errors (#103), and a studio
  5-minute metrics lane backed by persisted samples (#123).

### Worker (#15, the core of it)

- Manifest loader: a model is a JSON file in MODELS_DIR. Worker-side fields
  (`source`, fp16-safe `vae`, `scheduler` override, `lora`) never cross the wire.
  Schema defaults apply to missing params.
- Engine seam: SimulatedEngine keeps every protocol path runnable without a GPU
  (CI, scripts/simulate.py); DiffusersEngine runs text2img, img2img (#79) and
  realtime frames. One DEVICE setting covers cuda, rocm and cpu.
- Memory ladder (#15): MEMORY_MODE auto/full/model_offload/group_offload takes the
  highest rung that fits at load, with LRU eviction of cold models; realtime is
  gated to full residency.
- Survival on one card: OOM during a load or run evicts other resident models and
  retries once, so model switching works on 16 GB.
- Outputs are lossy WebP quality 80 (PNG masters are planned, #125).
- Models shipped: SDXL Base (default, 1024, DPM++ 2M Karras), SDXL Fast
  (Lightning 8-step), SSD-1B and SSD-1B-Lightning (fast batch tier), DreamShaper-LCM
  (smallest at ~6 GB), and VegaRT (Apache-2.0, the studio-shippable realtime model,
  ~452 ms warm img2img @512). SD Turbo, SDXL Turbo and SDXL Hyper-SD remain
  `benchmark_only` (license or measurement reasons).

### Upscale (post-generation, #76)

- Real-ESRGAN pixel upscale via spandrel, tiled with bounded VRAM (#89); upscale
  API dispatch with lineage and a required source asset, exclusive with diffusion
  capabilities (#90, #105); studio Upscale panel with a factor picker (#91); and a
  compact `realesrgan-fast` manifest as the default fast tier (#108).

### Studio UI (sketch scope, ahead of the frontend track)

- Generate panel (model select, schema-driven controls, count 1-8, non-blocking
  submits, progress thumbnails, viewer), an Upscale panel, a metrics dashboard
  (live GPU sampling plus the 5-minute persisted lane), starred/favorite history,
  sidebar with Models and History, en/es strings throughout.
- Dev loop: the vite server proxies `/api/v1` to the native API.

### Performance, measured on the reference RX 7600 XT

- Fused SDPA attention on RDNA3 (gated behind a flag torch leaves off),
  channels_last UNet/VAE, fp16 weights with fp16-safe VAE, warm pipeline reuse:
  SD Turbo 512 at ~290 gpu_ms/image, SDXL Turbo ~310, SDXL 1024 at 25 steps ~20 gpu_s.
- ROCm notes: torch 2.9.1+rocm6.3 ships gfx1102 kernels natively; no HSA override
  needed.

### Licensing

- Relicensed GPL-3.0 to AGPL-3.0 with commercial dual licensing (#109, COMMERCIAL.md);
  contributions now require DCO sign-off.

### Verification

- Backend and worker test suites (including a fake-worker dispatch flow against
  PostgreSQL and a postgres service in CI), the connection simulation, and a real
  end-to-end acceptance demo through scripts/generate.py and the studio.

## Left / Next

Grouped by milestone; the GitHub issues and milestones carry the authoritative,
tracker-verified detail (open issues and in-flight PRs).

### M2 leftovers

- Tiny-model CPU integration test in CI (the testing ladder's rung 2).
- v0.1 tag: three images plus the compose file, cut together.
- #1 UI component and theme system, #4 multi-tool interface foundation.
- Model routing tier field (designed; not shipped - see decisions.md).
- Optional: flip `TORCH_COMPILE` default on after a CUDA fleet bake-off
  (ROCm A/B in PR #141 was ~0-7% warm gain; left opt-in).

### M3 Real-time drawing

- #3 drawing tool, #19 realtime protocol extensions (prompt_update, queued
  position with the admission queue, idle slot release, browser keepalive),
  #41 pointer and stylus input, #42 canvas input optimizations, #54 stroke-op
  replay log, #55 vector masks and selections, #45 SPA/API version skew, #46
  status banner.

### M4 Accounts

- #5 / #9 auth modes (none, local, oauth behind one dependency seam), #10 account UI.

### M5 Cloud readiness (largest)

- #20 multi-worker scheduler, #21 usage metering and quota seam, #27 content
  safety screening, #28 in-app admin area, #29 usage metrics and opt-out telemetry,
  #48 relay load harness, #60 inference-speed baseline and optimization backlog,
  #95 CLIP output categorizer, #107 one-click benchmark pipeline, and the
  lineage-canvas foundations #124 (persist favorites), #125 (PNG masters), #129
  (lineage on the detail view).
- Inference backlog note (#60): torch.compile warmup and measured realtime slots
  are in-flight in PR #141. That PR recommends against enabling torch.compile by
  default (measured ROCm speedup only 0.8-7.4% against a large cold-load cost).

### M6 Launch and beta

- #40 landing pricing and previews, #49 invite-gated signup, #50 launch, and the
  lineage canvas itself: #130 pannable derivation forest, #131 branch-from-node
  with edge deltas, #132 search and filter overlays.

### M7 Developer API

- #43 developer API: one surface, many principals.

### M8 Illusions (Diffusion Illusions)

- #115 optimizer core (in-flight, PR #118, a standalone worker module not yet on
  `main`), #116 illusion jobs over the worker protocol, #117 studio illusion
  designer, #121 public explainer page, #122 phase-boundary reset, #134
  anagram-consistent joint targets, and #135-#138 (SDS refereeing, gradient
  upgrade, best-of-N seed probes with CLIP triage, prompt pre-screen). The optimizer
  core is on the `issue-115-illusion-optimizer` branch (PR #118).

### Deferred by recorded decision

- Worker-internal batching (StreamDiffusion-class step batching, CFG drop) and
  TAESD preview decode: the trigger is fleet spend, not local speed.
- Dedicated realtime and batch pools: a configuration change at scaling stage 2.
