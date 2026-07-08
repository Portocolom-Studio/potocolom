# Design decisions

One section per decision: what was chosen, why, and the main alternative that was rejected. See [architecture.md](architecture.md) for how the pieces fit together.

## API server: Python with FastAPI

The inference worker is Python regardless, because the model ecosystem (diffusers, PyTorch) is Python. Using FastAPI keeps the whole backend in one language, has first class async and WebSocket support for the real time flow, and generates an OpenAPI schema the frontend client can be generated from.

Rejected alternative: a TypeScript API server sharing types with the frontend. It would add a second backend language next to the unavoidable Python worker.

## Frontend: SvelteKit as a static SPA

The application is a login gated interactive tool (canvas drawing, live previews, tool views), so server side rendering adds nothing. SvelteKit with the static adapter produces one build artifact that the API server can serve when self-hosted and a CDN can serve in the cloud. Runtime configuration comes from the API, never from build flags.

Rejected alternative: React with Vite. Larger ecosystem, but SvelteKit was preferred for this project.

## Inference: custom worker on Hugging Face diffusers

Real time generation needs precise control over streaming, step counts, batching and VRAM, which a custom diffusers based worker provides. Each model family costs some integration work, and the manifest format keeps that work contained in the worker.

Rejected alternative: wrapping ComfyUI. Enormous model coverage immediately, but heavier, harder to drive programmatically and awkward for multi tenant cloud use.

## Cloud GPUs: the same worker image on rented machines

The exact same worker container runs on a self-hoster's GPU and on rented GPU machines (RunPod, vast.ai, GPU VMs) in the cloud, managed by our own orchestration. This maximizes code reuse between the two modes and keeps latency under our control, which matters for real time drawing.

Rejected alternative: calling third party inference APIs (fal.ai, Replicate) in the cloud. Faster to launch, but the cloud path would diverge from the self-hosted path.

## Authentication: built-in module

Email and password plus OAuth providers implemented inside the backend, behind the abstraction issue #5 asks for. Self-hosted installs need no extra service and can disable accounts entirely (issue #9) through the `none` mode.

Rejected alternative: shipping an identity server (Keycloak) with every install. Full featured but a heavy dependency for self-hosters who may not want accounts at all.

## Billing: subscriptions with credit balances

Monthly tiers grant a credit balance consumed by GPU time and images. Real time drawing consumes GPU seconds continuously, so a flat unlimited plan would let heavy users cost more than they pay. Credits bound that risk while keeping revenue predictable.

Rejected alternative: flat subscriptions without metering. Simplest experience, unbounded cost exposure.

## Open source boundary: commercial parts in a private repository

This repository stays fully self-hostable under GPL 3.0. Billing, the credit ledger and the GPU fleet orchestrator live in a private repository and integrate over HTTP service boundaries (QuotaService, metering events). The process boundary avoids GPL derivative work questions and keeps the public project complete on its own.

Rejected alternative: everything public. Maximum transparency, but anyone could clone the entire commercial service.

## Cloud provider: AWS as the reference deployment

The cloud profile is documented against concrete AWS services: Route 53, CloudFront and S3, an Application Load Balancer, ECS Fargate for the API and private services, RDS PostgreSQL, ElastiCache Redis and SES. AWS has a managed version of every piece the architecture needs and the largest documentation and hiring pool. GPU workers intentionally do not run on AWS: rented GPU providers (RunPod, vast.ai) cost several times less per GPU hour, and the fleet connects outbound so it never needs to be inside the VPC. Details in [cloud-infrastructure.md](cloud-infrastructure.md).

Rejected alternatives: Cloudflare plus Hetzner, the cheapest baseline but the database and Redis become self-operated; GCP, comparable but with no advantage that outweighed AWS familiarity.

## Worker connectivity: workers always dial the API

The worker opens one outbound persistent connection to the API server's fleet endpoint in both modes: to the API service on the compose network when self-hosted, to the public API hostname from rented GPU machines. Registration, job dispatch, real time frames and heartbeats multiplex over that connection. GPU machines therefore accept no inbound connections and need no VPN, and self-hosted and cloud share one identical code path.

Rejected alternative: a VPN overlay such as Tailscale or WireGuard between the VPC and the GPU machines. It works, but it adds an operational dependency to every worker and to every self-host install that wants parity.

## Sessions: opaque server-side tokens

Logged in state is a random token in an httpOnly cookie, mapped to a session row in PostgreSQL and cached in Redis in the cloud. This gives instant revocation and a real active-sessions list, which the session management in issue #5 needs; one Redis lookup per request is nothing at the target scale.

Rejected alternative: JWTs. They remove the store lookup, but instant revocation then needs a denylist, which reintroduces the store while keeping the JWT complexity.

## Realtime frame routing: Redis pub/sub between API replicas

The browser's WebSocket and the worker's persistent connection usually terminate on different API replicas. Frames hop between replicas over Redis pub/sub channels keyed by session id: sub millisecond inside the VPC, built on Redis we already run, and it removes any need for sticky sessions. Self-hosted, the relay is an in-process call behind the same interface.

Rejected alternatives: a dedicated realtime gateway service (cleanest latency path, but one more deployment that duplicates auth); having the worker redial the specific replica holding the browser (breaks the workers-dial-one-endpoint rule and fights Fargate networking).

## GPU pool: shared between jobs and realtime, realtime first

One worker pool at launch. Queued jobs fill idle capacity; an arriving realtime session preempts queued work between denoising steps, and jobs resume when sessions end. With one or two GPUs total this is the only shape that neither starves the batch queue nor pays for an idle machine. Pool membership is configuration, so dedicated realtime and batch pools at scaling stage 2 are a config change.

Rejected alternative: dedicated pools from day one. Predictable latency, but at launch scale it means a second always-on GPU.

## Full pool: admission queue with paid tier priority

A session request with no free slot waits in a queue with live position and estimated wait shown; queue length is a scale up signal for the autoscaler. Paid tiers move ahead in the queue once billing exists; active sessions are never preempted.

Rejected alternatives: hard rejection (worst experience, no demand signal); time slice sharing (everyone's frame rate collapses instead of anyone waiting).

## Idle realtime sessions: release after 60 seconds, transparent resume

An idle drawing session releases its slot and stops metering after about 60 seconds without input; the canvas stays intact and the next stroke reacquires a slot, usually instantly. Forgotten tabs therefore cost nothing and block nobody.

Rejected alternative: pinning the slot while the tab is open. Zero resume friction, but forgotten tabs silently drain credits, which is a support complaint machine.

## Model placement: hot set plus on-demand loading

The realtime model and the most used generation models stay pinned on workers; everything else loads on demand with a visible one-time loading state of about a minute, then stays warm. Adding a model remains a manifest drop, never an ops action.

Rejected alternatives: everything on demand (popular models get evicted repeatedly, even the drawing tool cold starts); strictly pinned pools (a new model is unavailable until someone reconfigures the fleet).

## Realtime bar: 2 to 4 fps at 512 px

An explicit target, because the scheduler must know how many sessions one GPU admits. SD-Turbo and LCM class models deliver this on an RTX 4090 class GPU with one or two concurrent sessions.

Rejected alternative: no stated target. A number gets picked implicitly anyway, just without anyone agreeing to it.

## Job failures: retry once, then fail visibly

The job row is the source of truth. A worker dying mid job requeues the job once on another worker; a second failure surfaces as a failed job with a retry button and the reserved credits refunded.

Rejected alternative: retrying until success. Users never see infrastructure failures, but an input that crashes workers burns GPU money forever.

## Resilience posture: single AZ with point in time recovery

RDS runs single AZ with PITR and automated snapshots. The accepted worst case for an availability zone failure is up to five minutes of lost writes and about an hour of manual recovery. Redis is never a source of truth, so its loss degrades features without logging anyone out.

Rejected alternative: Multi-AZ from day one. Roughly 30 USD per month of insurance before there is revenue to protect; it is a checkbox to enable later.

## Content safety: prompt screening and output checking in the cloud

The cloud profile screens prompts before dispatch (blocklist plus lightweight classifier, so refusals cost no GPU time) and runs the standard diffusers safety checker on outputs (flagged images are blocked and never stored). Self-hosted installs have both off by default. GPU providers' terms of service and payment processors force a position here; this is the defensible one.

Rejected alternative: report button and audit trail only. Lowest friction, riskiest with the parties who can turn the service off.

## Cloud trial: small one-time credit grant

New verified signups get credits for a few minutes of drawing and a handful of generations, once per email, with per IP signup caps and a disposable email domain blocklist. Abuse is bounded to pennies per fake account.

Rejected alternatives: a recurring free tier (makes account farming profitable forever); no free tier (the self-hosted version becomes the only trial, which converts poorly).

## Retention: subscribers keep everything, trial assets expire in 30 days

Storage is the only cost that never resets monthly. The library becomes part of what a subscription buys; trial assets expire via `expires_at` plus a cleanup job, with an S3 lifecycle rule as backstop.

Rejected alternative: keep everything forever. Simple, but retroactively adding expiry later is a trust problem.

## Privacy: private by default, opt-in share links

Assets are served through short lived signed URLs; a user can mint a revocable share link that exposes one asset under an unguessable token. No public gallery, so no moderation surface beyond the safety checks.

Rejected alternative: a public community gallery. A strong growth loop that is also a standing moderation commitment, wrong for launch.

## Credit unit: abstract credits

Users see credits, not GPU time: one credit is roughly one GPU second internally, a generation costs a handful of credits by size and steps, and the drawing tool shows the live drain per minute. Pricing survives GPU provider price swings without visible repricing.

Rejected alternatives: raw GPU minutes (honest but unpredictable per image); image-and-minute bundles (two parallel meters, and every model change silently reprices an image).

## OAuth at launch: Google, GitHub and Apple

Google for reach, GitHub for the self-hosting crowd who arrive first, Apple chosen with eyes open: it requires the paid Apple developer account and key rotation, and mainly pays off if a native iOS app ships later.

Rejected alternative: Discord, despite hosting the AI art communities; it can be added when the audience demands it.

## Observability: CloudWatch plus Sentry

CloudWatch for metrics, structured JSON logs and alarms (queue depth, error rates, worker heartbeat gaps); Sentry's free tier for exceptions with stack traces across API, worker and frontend, including errors in users' browsers that would otherwise be invisible.

Rejected alternatives: CloudWatch alone (exception debugging becomes log group archaeology); a Grafana stack (real infrastructure to operate before there are users to justify it).

## Telemetry: none from self-hosted installs

A self-hosted install makes zero calls to project infrastructure, not even an update check. Cleanest possible position for a GPL self-hosting audience; the cost is not knowing install counts or versions in the wild.

Rejected alternatives: a startup update check (mild, but still a phone-home to explain); opt-in anonymous stats (even opt-in draws suspicion in self-host communities).

## Worker testing without GPUs: tiny model on CPU in CI

CI runs the worker's real code path (manifest loading, scheduling, frame streaming, safety checker) against a deliberately tiny diffusion model on CPU: slow, ugly output, real execution. Unit tests mock the pipeline interface; a real GPU smoke test runs manually before releases.

Rejected alternatives: mocking inference entirely (worker code first meets a real model on someone's GPU); a self-hosted GPU runner (standing cost and a security-sensitive surface for PRs from forks).

## Worker protocol: N-1 compatibility

The worker connection carries a protocol version and each API release supports workers from the previous release. Cloud deploys never force a fleet-wide drain, and self-hosters who upgrade the API first get one release of grace with an outdated worker warning.

Rejected alternatives: strict lockstep (every deploy drains the whole fleet, partial self-host upgrades break hard); a wide compatibility window (compatibility branches that must be tested forever).

## Staging: scaled down, same modules

Staging uses the same Terraform modules at minimum sizes: one API task, the smallest RDS instance, no always-on GPU. Roughly 60 to 80 USD per month, and it still exercises the real deploy pipeline.

Rejected alternatives: a full production mirror (roughly 170 USD per month plus GPU time, buying little at this scale); no staging with canary deploys (every infrastructure mistake rehearsed in production).

## GDPR: self-serve deletion and export, 30 day purge

Account settings offer deletion (immediate deactivation, hard delete of rows and assets within 30 days) and a data export (JSON plus an archive of images). GDPR makes both obligations; building them into v1 is far cheaper than retrofitting, the purge window doubles as recovery from account takeover, and self-hosted installs inherit both.

Rejected alternatives: instant hard delete (no recovery from a hijacked account wiping a paying user's library); handling requests over support email (legal but toil, and a bad signal in a privacy conscious market).

## Languages: i18n from day one, English and Spanish at launch

Every user facing string goes through the i18n layer starting with the first component. The team writes both languages natively, so the second language is nearly free once extraction exists; retrofitting extraction into a finished SPA is the expensive path this avoids.

Rejected alternative: hardcoded English now, extraction later. The standard way projects buy a painful year-two refactor.

## Payments: hosted Stripe surfaces with Stripe Tax

Stripe Checkout for purchase, the hosted customer portal for plan changes, cancellations and invoices, and Stripe Tax for EU VAT. Card data never touches project servers, PCI scope stays minimal, and the private billing service shrinks to webhook handling plus the credit ledger.

Rejected alternatives: embedded Stripe Elements (seamless UX for meaningfully more code and compliance surface, and the portal features would need rebuilding); a merchant of record such as Paddle (removes VAT liability entirely but costs around five percent and fits the credit metering model worse).

## Age policy: 18 and older at launch

One attestation checkbox at signup, no parental consent machinery, the simplest defensible terms while the moderation stack is young. Lowering an age limit later is easy; raising one on existing users is not.

Rejected alternatives: 14+ (Spain's digital consent age, but other EU states differ, forcing per country logic) and 13+ (maximum reach, maximum child safety obligation for an image generator).

## Model weights: own mirror on Cloudflare R2

Vetted weights are copied once to R2, which charges no egress; workers pull their assigned models at boot over datacenter links and verify manifest checksums. Boot stays predictably inside the scale up promise, with no Hugging Face rate limits, tokens on untrusted machines, or disappearing repositories in the critical path. Self-hosters pull from Hugging Face directly.

Rejected alternatives: Hugging Face at boot (variable speed and a third party in every scale up); weights baked into the worker image (tens of GB images where every model change is a rebuild and the image pull becomes the new slow path).

## Database migrations: gated step in the cloud, automatic self-hosted

Cloud deploys run Alembic as a one-off task before tasks roll, and every migration must stay compatible with the previous release's code (expand, backfill, contract later), mirroring the worker protocol's N-1 discipline. Self-hosted installs migrate on API startup, safe with a single instance.

Rejected alternatives: migrate on startup everywhere (replicas race, and a bad migration takes down every task at once); manual migrations (self-hosters forget and file confusing bug reports).

## Admin: minimal in-app admin area

An admin role flag unlocks hidden views in the same SPA: worker fleet status, user lookup and disable, job and session debugging. Self-hosters get the same views for their own install, so the work is shared rather than cloud only.

Rejected alternatives: CLI scripts only (fine solo, hostile to anyone who joins later); nothing at launch (every incident handled through psql until the pain forces the admin area anyway).

## Releases: trunk based, one project version

Main stays deployable; a tag cuts all three images plus the compose file together. The N-1 worker protocol promise reads as "this tag talks to the previous tag", and self-hosters reason about one version and one changelog.

Rejected alternatives: per component versions (the compatibility statement becomes a matrix); gitflow (stabilization ceremony for parallel releases this project does not have).

## GPU privacy: accept and disclose

Rented GPU machines process prompts and canvas frames in plaintext during inference. TLS covers transit, nothing persists on the machine beyond the weights cache, results upload straight to S3, and the privacy policy names GPU providers as subprocessors. Industry standard at this price point, stated honestly.

Rejected alternatives: restricting to vetted datacenter tiers (roughly double the GPU price; a defensible middle ground if enterprise demand appears); GPUs inside AWS (several times the cost, reversing the fleet economics decision).

## Autoscaler spend: hard cap with graceful degradation

The fleet autoscaler enforces an absolute machine ceiling and a monthly budget. Approaching either, it stops scaling up and admission queues grow behind a high demand banner; raising the cap is a deliberate configuration change. No failure mode produces an unbounded bill.

Rejected alternatives: alerts only (the exposure window is as long as whoever is on call sleeps); relying on per user credit caps (bounds each account, says nothing about thousands of trial signups at once or a malfunctioning autoscaler).

## Account security: strong base at launch, TOTP as a fast follow

Launch with argon2 password hashing, rate limited logins, email notification on new sign ins and instantly revocable sessions; the schema reserves a TOTP secret so two factor lands later without a migration. OAuth users already carry their provider's two factor.

Rejected alternatives: TOTP at launch (enrollment, recovery codes and reset flows would delay the whole accounts milestone); deferring indefinitely to OAuth (weakest story for the email and password accounts self-hosters prefer).

## User uploaded models: explicitly out of scope

Model manifests stay operator controlled. Fine tune and LoRA uploads are a large security, storage and licensing surface; nothing in the registry, storage or scheduler accommodates them, so a future decision starts from a clean sheet.

Rejected alternatives: leaving pluggable seams now (speculative flexibility that complicates the registry before a single real model is served); creating a post-launch milestone now (gives the idea a gravity well before the core ships).

## Scheduler: leader elected inside the API replicas

One replica holds a short Redis lease and runs the single threaded scheduling loop (admission, dispatch, preemption, idle release); the others forward events. No extra deployable, failover within seconds when the lease lapses, and self-hosted the only process is simply always the leader.

Rejected alternatives: a dedicated scheduler service (cleanest isolation, one more deployment before launch); lock based scheduling in every replica (distributed race bugs concentrated exactly where GPU money is spent).

## Redis topology: one instance, split-ready namespaces

A single instance at launch, but every key belongs to one concern (sessions, queue, rt, rate) and each concern's client reads its own endpoint setting, so moving pub/sub or the queues to dedicated instances later is configuration. Redis is never the source of truth, so its loss degrades features without losing data.

Rejected alternatives: a replica from day one (pays for failover on a component whose loss already cannot lose data); a functional split now (isolation with no load to isolate).

## Queues: sorted sets with Lua pops, PostgreSQL as truth

The job queue and the realtime admission queue are Redis sorted sets scored by tier then enqueue time, popped atomically with a small Lua script. Priority is native, queue position for the waiting room is one ZRANK, and recovery is rebuilding the set from job and session rows.

Rejected alternatives: Redis Streams (delivery tracking that duplicates what the PostgreSQL rows provide, and priority needs a stream per tier); Celery or RQ (assume queue consuming worker processes, but our workers hang off WebSocket connections).

## Realtime wire format: binary frames, JSON control

WebSocket text messages carry JSON control traffic, readable in browser devtools; binary messages carry WebP image payloads behind a small fixed header. No base64, so a third less bandwidth exactly where the 2 to 4 fps flows in both directions.

Rejected alternatives: msgpack for everything (compact but undebuggable control traffic for trivial savings); JSON with base64 images (a third more bandwidth on every frame, forever).

## GPU targets: CUDA and ROCm supported from day one

The worker ships two image variants, CUDA (NVIDIA) and ROCm (AMD), behind one `DEVICE` setting, plus a CPU mode for CI and GPU-less contributors. The cloud fleet stays entirely CUDA, since rented GPU providers are NVIDIA; ROCm serves self-hosters with AMD cards and the reference development desktop, which is AMD and becomes the standing ROCm test machine. The cost accepted knowingly: a second platform to keep working, untestable in CI, verified manually before each release.

Rejected alternatives: CUDA only (the primary development machine could then never run real inference); ROCm as an unofficial best-effort target (would serve the desk but leave AMD self-hosters in an ambiguous, undocumented state).

## Development loop: dependencies in containers, applications native

PostgreSQL, Redis, MinIO and Mailpit run from a dev compose file; the API server, frontend dev server and worker run natively with hot reload and debugger access. The containerized applications are still exercised by the cloud simulation, CI image builds and pre-release runs of the shipped compose file.

Rejected alternatives: everything in containers (closest to what ships, but slower iteration and clumsier debugging every single day); everything native (host setups drift and version differences surface as mystery bugs).

## Cloud testing: simulated topology from generic containers, not AWS emulation

The cloud profile is validated locally by reproducing its topology, nginx in front of two API replicas, Redis, MinIO, Mailpit and a fake QuotaService, exercising exactly the seams the cloud uses. The application code cannot tell nginx from an ALB or MinIO from S3, which is what the seams are for. AWS-specific control plane (Terraform, IAM, ALB behavior, CloudFront signing, SES deliverability) is validated once on the real scaled-down staging, only when the cloud launch is being prepared. Until then the infrastructure cost of development is zero. Details in [local-development.md](local-development.md).

Rejected alternative: LocalStack or similar AWS emulators. The two AWS APIs the application touches (S3, SMTP) are covered better by MinIO and Mailpit; the rest is control plane that emulators reproduce poorly, giving confidence that staging would immediately contradict.

## Database access: async SQLAlchemy, migrations from the first table

SQLAlchemy 2.0 in asyncio mode with asyncpg, because the backend is already async end to end (FastAPI endpoints, the realtime relay, the scheduler loop) and a sync engine would reintroduce threadpool hops exactly where latency matters. Alembic manages the schema from the very first table, so the startup auto-apply hook and the cloud's gated migration task exist from day one and every self-hosted install has an upgrade path, which the portability story in [deployment-profiles.md](deployment-profiles.md) depends on.

Rejected alternatives: sync SQLAlchemy in FastAPI's threadpool (better debugged ecosystem, but the pure-async scheduler and realtime paths would need executor wrappers around every query); `create_all` until the schema settles (less migration churn during the walking skeleton, but anyone running v0.1 would be stranded at the first schema change).

## Model registry: persistent rows with a live availability flag

Models registered by workers persist in PostgreSQL, and `GET /api/v1/models` returns every known model with an `available` flag computed from live worker registrations. The UI greys out what cannot serve right now instead of having models flicker in and out on worker restarts, and history rows can always resolve the name and schema of the model that produced them.

Rejected alternatives: listing only live models (simpler response, but a worker restart makes models vanish from the UI and orphans old history); returning the stored registry with no signal (the user discovers unavailability by a failed generation).

## Stored outputs: PNG

Generated images are stored as PNG: lossless, universal, no quality knobs to decide, written by Pillow with no extra dependency. Cloud storage cost is bounded by the retention decision rather than the format. The realtime wire keeps WebP; transport and storage are different concerns with different constraints.

Rejected alternatives: WebP lossless storage (a quarter to a third smaller, worth revisiting when egress bills exist, not before); format as a request parameter (two code paths and a decision pushed onto every caller, for flexibility nobody asked for).

## Model manifests: JSON

Manifests are JSON files. The `parameters` field is JSON Schema, so the manifest is JSON all the way down, the standard library parses it, and the API can return it verbatim from `GET /api/v1/models`.

Rejected alternatives: YAML (nicer to hand-edit, but a pyyaml dependency and JSON Schema embedded in a second syntax); TOML (stdlib readable, but deeply nested schema objects are genuinely awkward in it).

## Drawing surface: bitmap canvas

The drawing tool paints strokes directly onto one `<canvas>` element; frame capture for the realtime loop is a native `canvas.toBlob("image/webp")`. Undo is a snapshot stack, and eraser and fill are plain pixel operations. The wire protocol already fixed rasterized frames, so the cheapest path from stroke to encoded frame wins at 2 to 4 fps.

Rejected alternative: an SVG vector layer rasterized to a hidden canvas per frame (individually editable strokes, but every frame pays a serialize, draw and encode pipeline, plus hit-testing complexity, for editing semantics the realtime loop does not need).

## First public release: after the walking skeleton, API level

v0.1 tags when the M2 acceptance demo passes: a generation POSTed against the real worker completes end to end and CI's tiny-model CPU path is green. Self-hosters get the compose file and a working generation API, clearly marked pre-alpha. The point is early outside installs exercising the risky part, GPU setup on CUDA and ROCm, months before the UI is impressive.

Rejected alternatives: first tag after M3 drawing (a better first impression, but zero outside feedback on installation pain in the meantime); after M4 accounts (`AUTH_MODE=none` already covers the single-user install, so accounts gate nothing).

## Frontend foundation: CSS custom properties and a hand-rolled i18n store

The theme system (issue #1) is built on hand-rolled design tokens as CSS custom properties, with dark and light driven by `prefers-color-scheme` plus a `data-theme` override; components are plain Svelte. Internationalization is two JSON dictionaries behind a tiny store, roughly thirty lines. Unit tests run under Vitest; a browser end-to-end rig arrives with the drawing issue, when there are real flows worth driving.

Rejected alternatives: Tailwind (fast iteration, but the theme system, which is the entire point of issue #1, becomes Tailwind's); a component library (fastest to decent, hardest to make not look templated); Paraglide or svelte-i18n (typed messages and ICU plurals for what is today two flat dictionaries of static strings, adopt one the day plural-heavy content appears).

## Low VRAM operation: the diffusers offloading ladder, not airLLM

Models larger than a card's VRAM run through a per-model memory ladder in the worker: full residency when the pipeline fits, model CPU offload when only the largest component fits, group offloading with stream prefetch (and disk spill when system RAM is short) below that. All rungs are native diffusers and accelerate features, so the ladder is configuration of the already-chosen inference stack, not new machinery. The rung is picked automatically from measured free VRAM at model load and can be pinned with the worker's `MEMORY_MODE` setting. Only full residency meets the 2 to 4 fps realtime bar, so lower rungs advertise the model without its `realtime` capability; queued jobs tolerate the slowdown, drawing sessions never see it. This mainly serves self-hosters on consumer GPUs; the cloud fleet rents cards sized for full residency. Details in [architecture.md](architecture.md).

Rejected alternatives: adopting airLLM itself (it targets transformer LLMs through the transformers library and cannot drive diffusion pipelines; its layer streaming and prefetching ideas are exactly what diffusers group offloading already implements for our models); a custom layer streamer (reimplements accelerate's hook machinery for zero gain); requiring full residency (locks self-hosters with 4 to 8 GB cards out of larger generation models, on the exact deployment the project exists to serve).

## Model routing: request tiers resolved in the API, no difficulty classifier

When a generation request does not pin a `model_id`, the API resolves the cheapest registered model whose tier, capabilities and parameters satisfy the request; manifests carry a `tier` field (`draft`, `standard`, `premium`). Our workloads announce their own difficulty through the interface: a drawing stroke is realtime and lands on a turbo-class model, a refine click is a queued job and routes to a heavier one. The router is a small selection policy inside the existing dispatch path.

Rejected alternatives: an ML difficulty classifier in front of the models (burns GPU time to guess what the UI action already states, and misclassification is user-visible); a separate routing proxy service (a deployment and a failure mode for what is one function in the API).

## Worker performance: compile and channels_last at warmup

Hot-set models are warmed with `torch.compile` and `channels_last` memory format when loaded, worth roughly a fifth to a third off denoising time on diffusion pipelines for zero new dependencies. The extra compile minute hides inside the existing model loading state. The attention backend is configuration through diffusers' `set_attention_backend`.

Rejected alternatives: skipping compilation (leaves the single largest free speedup on the table, and GPU seconds are the priced resource); TensorRT or similar vendor toolchains (real gains, but a per-vendor build matrix against our CUDA plus ROCm promise, revisit if fleet economics demand it).

## Image codecs off the event loop

WebP and PNG encoding and decoding, the main per-frame CPU cost in both the worker and the API relay, always run in a thread executor, never on the asyncio loop. This keeps frame pacing and WebSocket heartbeats steady at the 2 to 4 fps bar. Binding for issues #15, #16 and #19.

Rejected alternative: SIMD image libraries (pillow-simd, libvips) before a profile shows Pillow in an executor is the bottleneck.

## Job placement: offloaded workers first, micro-batching deferred

When several workers can take a queued job, the scheduler prefers workers serving the model on a lower memory ladder rung, keeping fully resident workers free for realtime admission, which only they can serve. One comparator in worker selection. Micro-batching same-model queued jobs is deliberately deferred until a real cloud fleet exists: it raises throughput but complicates slot accounting and preemption, and at a one or two GPU scale there is nothing to batch.

Rejected alternative: latency or geography aware placement (a single-region fleet at launch scale has nothing to optimize).

## Supporting defaults

Chosen as conventional defaults rather than debated decisions:

- PostgreSQL with SQLAlchemy and Alembic migrations. One database engine in every mode; docker compose makes it trivial for self-hosters.
- Redis only in the cloud profile (queue, session scheduling, rate limiting). Self-hosted installs do not need it.
- Object storage behind an adapter: local filesystem by default, S3 compatible in the cloud.
- Docker Compose as the self-hosted distribution format.
- VRAM requirements are per model metadata (min_vram_gb in the manifest), applying across GPU vendors.
- Monorepo: frontend, backend, worker, deploy and docs live in this repository.
- Documentation diagrams are written in Mermaid, which GitHub renders as drawn diagrams; UI wireframes stay ASCII because they sketch screen layouts.
