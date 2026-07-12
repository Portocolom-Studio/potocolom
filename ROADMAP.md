# Roadmap

A living snapshot of where the M2 walking skeleton stands (PR #58) and what
comes after it. Issues and milestones remain the planning source of truth;
this file is the readable summary. Decisions and their rejected alternatives
live in [docs/decisions.md](docs/decisions.md).

## Done

### Platform (backend)

- Configuration profile (#14): storage, database and public URL settings read
  once at startup; `GET /api/v1/config` drives the frontend.
- Storage seam (#17): one interface, two implementations. LocalStorage
  uploads and serves through `/api/v1/files/{key}`; S3Storage presigns PUT
  and GET (SigV4, MinIO in development). Workers receive an upload target
  with each job and never talk to storage implementations directly.
- Database layer: async SQLAlchemy with alembic migrating on startup
  (self-hosted behavior per decisions.md); users, models, jobs and assets
  from the docs/architecture.md data model. A down database degrades the API
  (row endpoints answer 503) instead of failing the health check.
- Job dispatch and history (#16): `POST /api/v1/generations` validates params
  against the model's JSON Schema, queues on the blueprint Queues seam
  (in-process implementation; the cloud profile swaps in Redis sorted sets),
  and dispatches over the fleet socket. Row locks serialize job state
  transitions, a lost worker retries a job exactly once, the queue rebuilds
  from PostgreSQL on restart. Progress streams over SSE with keepalives and
  rides along in history responses while a job runs.
- Model registry (#11): manifests travel in the fleet hello, persist for
  history foreign keys, and `GET /api/v1/models` serves them with schema,
  capabilities and a default flag.
- Wire protocol: dispatch_job, job_progress, job_done, job_failed and session
  params added to docs/connection-handling.md; simulation still green.

### Worker (#15, the core of it)

- Manifest loader: a model is a JSON file in MODELS_DIR. Worker-side fields:
  `source` (weights), `vae` (fp16-safe replacement), `scheduler` (override),
  none of which cross the wire. Schema property defaults apply to missing
  params, so a bare prompt renders with the model's intended settings.
- Engine seam: SimulatedEngine keeps every protocol path runnable without a
  GPU (CI, scripts/simulate.py); DiffusersEngine runs text to image jobs with
  step progress, upload and gpu_ms, and realtime image to image frames.
  One DEVICE setting covers cuda, rocm and cpu.
- Survival on one card: out-of-memory during a load or a run evicts the other
  resident models and retries once, so model switching works on 16 GB.
- Models shipped: SD Turbo and SDXL Turbo (512, realtime class) and
  SDXL (default) at 1024 with DPM++ 2M Karras.

### Studio UI (sketch scope, ahead of the frontend track)

- Generate tab: model select, prompt, schema-driven size options, count 1-8
  with non-blocking submits (jobs queue server side), progress bars on
  working thumbnails, viewer with click-to-pin from the strip or the gallery,
  full-size open in a new tab.
- Sidebar: Playground with Models and History (recent prompts refill the
  form) as nested collapsibles, gallery grid, en/es strings throughout.
- Dev loop: the vite server proxies /api/v1 to the native API.

### Performance, measured on the reference RX 7600 XT

- Fused SDPA attention on RDNA3 (gated behind a flag torch leaves off),
  channels_last UNet/VAE, fp16 weights with fp16-safe VAE, warm pipeline
  reuse: SD Turbo 512 at ~290 gpu_ms/image, SDXL Turbo ~310, SDXL 1024 at
  25 steps ~20 gpu_s. First use of a model pays a one-time weight download.
- ROCm notes: torch 2.9.1+rocm6.3 ships gfx1102 kernels natively (older
  rocm6.2 wheels do not; no HSA override needed or helpful).

### Verification

- Backend 23 tests (including a fake-worker dispatch flow against
  PostgreSQL; CI got a postgres service container), worker 11 tests, the
  connection simulation, and the real acceptance demo: a generated image end
  to end through scripts/generate.py and through the studio.

## Left

### To close M2

- Split PR #58 into per-issue PRs; write decisions.md entries for what the
  sketch added on top of the blueprint (manifest vae/scheduler/default
  fields, resolution enums, NullPool engine, SSE keepalives).
- Worker thumbnails beside originals (#56) and lineage columns (#57).
- Tiny-model CPU integration test in CI (the testing ladder's rung 2).
- Packaging: docker images and the one-file self-hosted compose (#18).
- v0.1 tag: three images plus the compose file, cut together.

### #15 remainder (inference depth)

- Memory ladder (MEMORY_MODE auto/full/model_offload/group_offload), which
  unlocks FLUX.1-schnell on 16 GB cards - the quality ceiling on the
  license shortlist; until then SDXL base is the flagged default.
- Measured realtime slots at warmup (benchmark ms/frame, advertise what
  holds the 2-4 fps bar) and the model routing tier field.
- torch.compile + warmup pass (recorded decision; needs calibration time).

### M3 and later, per their issues

- Auth modes local and oauth behind the same dependency (#5, #9).
- Realtime protocol extensions (#19): prompt_update, queued position with
  the admission queue, idle slot release, browser keepalive.
- The drawing tool and canvas input optimizations (#3, #41, #42).
- Quota seam with metering events; billing integration stays in the private
  repo behind QuotaService.
- Cloud profile: Redis queues and frame bus, scheduler leader election,
  cloud-sim compose validation; content safety (#27); admin area (#28).

### Optimizations deferred by recorded decision

- Worker-internal batching (StreamDiffusion-class step batching, CFG drop)
  and TAESD preview decode: trigger is fleet spend, not local speed.
- Dedicated realtime/batch pools: a configuration change at scaling stage 2.
