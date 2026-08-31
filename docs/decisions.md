# Design decisions

One section per decision: what was chosen, why, and the main alternative that was rejected. See [architecture.md](architecture.md) for how the pieces fit together.

## API server: Python with FastAPI

The inference worker is Python regardless, because the model ecosystem (diffusers, PyTorch) is Python. Using FastAPI keeps the whole backend in one language, has first class async and WebSocket support for the real time flow, and generates an OpenAPI schema the frontend client can be generated from.

Rejected alternative: a TypeScript API server sharing types with the frontend. It would add a second backend language next to the unavoidable Python worker.

Re-examined against a Go port and reaffirmed. FastAPI's native plumbing (uvloop over libuv, httptools over llhttp, pydantic-core in Rust) covers I/O, parsing and validation while handlers stay interpreted Python; Go's real web-tier advantage therefore optimizes the wrong bottleneck, because GPU seconds are the priced resource and the relay at target scale moves only thousands of small messages per second. A port would trade away the integrated validation, OpenAPI generation and DI, re-prove both WebSocket protocols, and still leave a two-language backend since the worker cannot leave Python. Go enters where its shape pays instead: the pre-planned relay gateway (see "Realtime relay") and the private billing service.

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

This repository stays fully self-hostable under AGPL-3.0 (originally GPL-3.0; relicensed by issue #109 / PR #110, see "License: AGPL-3.0 with commercial dual licensing" below). Billing, the credit ledger and the GPU fleet orchestrator live in a private repository and integrate over HTTP service boundaries (QuotaService, metering events). The process boundary avoids copyleft derivative work questions and keeps the public project complete on its own. <!-- corrected 2026-07-23: was "under GPL 3.0" -->

Rejected alternative: everything public. Maximum transparency, but anyone could clone the entire commercial service.

## Cloud provider: AWS as the reference deployment

The cloud profile is documented against concrete AWS services: Route 53, CloudFront and S3, an Application Load Balancer, ECS Fargate for the API and private services, RDS PostgreSQL, ElastiCache Redis and SES. AWS has a managed version of every piece the architecture needs and the largest documentation and hiring pool. GPU workers intentionally do not run on AWS: rented GPU providers (RunPod, vast.ai) cost several times less per GPU hour, and the fleet connects outbound so it never needs to be inside the VPC. Details in [cloud-infrastructure.md](cloud-infrastructure.md).

Rejected alternatives: Cloudflare plus Hetzner, the cheapest baseline but the database and Redis become self-operated; GCP, comparable but with no advantage that outweighed AWS familiarity.

## Worker connectivity: workers always dial the API

The worker opens one outbound persistent connection to the API server's fleet endpoint in both modes: to the API service on the compose network when self-hosted, to the public API hostname from rented GPU machines. Registration, job dispatch, real time frames and heartbeats multiplex over that connection. GPU machines therefore accept no inbound connections and need no VPN, and self-hosted and cloud share one identical code path.

Rejected alternative: a VPN overlay such as Tailscale or WireGuard between the VPC and the GPU machines. It works, but it adds an operational dependency to every worker and to every self-host install that wants parity.

## Sessions: opaque server-side tokens

Logged in state is a random token in an httpOnly cookie (invisible to page JavaScript, so a script injection cannot exfiltrate the session), mapped to a session row in PostgreSQL and cached in Redis in the cloud. This gives instant revocation and a real active-sessions list, which the session management in issue #5 needs; one Redis lookup per request is nothing at the target scale.

Rejected alternative: JWTs. They remove the store lookup, but instant revocation then needs a denylist, which reintroduces the store while keeping the JWT complexity.

## Realtime frame routing: Redis pub/sub between API replicas

The browser's WebSocket and the worker's persistent connection usually terminate on different API replicas. Frames hop between replicas over Redis pub/sub channels keyed by session id: sub millisecond inside the VPC, built on Redis we already run, and it removes any need for sticky sessions. Self-hosted, the relay is an in-process call behind the same interface.

Rejected alternatives: a dedicated realtime gateway service (cleanest latency path, but one more deployment that duplicates auth); having the worker redial the specific replica holding the browser (breaks the workers-dial-one-endpoint rule and fights Fargate networking).

> Shipped status (2026-07-30): **not yet implemented.** The current relay keeps workers and sessions in process-local dictionaries and directly awaits socket sends; the backend has no Redis dependency or FrameBus. The target is governed by "Realtime and queue Redis seam: optional, behaviorally equivalent" and the issue "Redis-optional Queues and FrameBus contracts".

The rejected gateway is pre-planned as a scale-stage extraction with an explicit trigger; see "Realtime relay: planned extraction into a Go gateway at scale" below.

## GPU pool: shared between jobs and realtime, realtime first

One worker pool at launch. Queued jobs fill idle capacity; an arriving realtime session preempts queued work between denoising steps, and jobs resume when sessions end. With one or two GPUs total this is the only shape that neither starves the batch queue nor pays for an idle machine. Pool membership is configuration, so dedicated realtime and batch pools at scaling stage 2 are a config change.

Rejected alternative: dedicated pools from day one. Predictable latency, but at launch scale it means a second always-on GPU.

## Full pool: admission queue with paid tier priority

A session request with no free slot waits in a queue with live position and estimated wait shown; queue length is a scale up signal for the autoscaler. Paid tiers move ahead in the queue once billing exists; active sessions are never preempted.

Rejected alternatives: hard rejection (worst experience, no demand signal); time slice sharing (everyone's frame rate collapses instead of anyone waiting).

> Shipped status (2026-07-30): **not yet implemented.** The current realtime handler hard-rejects a full pool with close code 4003. Queue admission remains the target under issue #19, "Real-Time Generation Protocol", and the issue "Redis-optional Queues and FrameBus contracts".

## Idle realtime sessions: release after 60 seconds, transparent resume

An idle drawing session releases its slot and stops metering after about 60 seconds without input; the canvas stays intact and the next stroke reacquires a slot, usually instantly. Forgotten tabs therefore cost nothing and block nobody.

Rejected alternative: pinning the slot while the tab is open. Zero resume friction, but forgotten tabs silently drain credits, which is a support complaint machine.

> Shipped status (2026-07-30): **not yet implemented.** The current browser handler records no last-input time and pins a ready slot until the connection closes. Issue #19, "Real-Time Generation Protocol", owns idle release and resume controls; issue #20, "Multi-Worker Scheduling", owns reacquisition and priority.

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

## OAuth at launch: Google and GitHub

Google for reach, GitHub for the self-hosting crowd who arrive first. Amended after peer review on the docs PR: Apple, originally in the launch set, is deferred - it requires the paid Apple developer account and key rotation, and mainly pays off if a native iOS app ships later, so it buys nothing for the beta.

Rejected alternatives: Apple at launch (the cost above, ahead of any native app); Discord, despite hosting the AI art communities; both can be added when the audience demands them.

## Observability: CloudWatch plus Sentry

CloudWatch for metrics, structured JSON logs and alarms (queue depth, error rates, worker heartbeat gaps); Sentry's free tier for exceptions with stack traces across API, worker and frontend, including errors in users' browsers that would otherwise be invisible.

Rejected alternatives: CloudWatch alone (exception debugging becomes log group archaeology); a Grafana stack (real infrastructure to operate before there are users to justify it).

## Telemetry: none from self-hosted installs

A self-hosted install makes zero calls to project infrastructure, not even an update check. Cleanest possible position for a GPL self-hosting audience; the cost is not knowing install counts or versions in the wild.

Rejected alternatives: a startup update check (mild, but still a phone-home to explain); opt-in anonymous stats (even opt-in draws suspicion in self-host communities).

Superseded by "Telemetry: opt-out anonymous aggregates from self-hosted installs" below, when usage analytics became a product requirement.

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

An admin role flag unlocks hidden views in the same SPA: worker fleet status, user lookup and disable, job and session debugging. Every admin endpoint enforces the role server-side; hiding the views is presentation, never the authorization. Self-hosters get the same views for their own install, so the work is shared rather than cloud only.

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

> Shipped status (2026-07-30): **partially implemented.** An in-process loop dispatches queued generation jobs, but there is no Redis lease, realtime admission queue, preemption, idle release, or cross-replica recovery. Issue #20, "Multi-Worker Scheduling", and "Redis-optional Queues and FrameBus contracts" govern the remaining design.

## Redis topology: one instance, split-ready namespaces

A single instance at launch, but every key belongs to one concern (sessions, queue, rt, rate) and each concern's client reads its own endpoint setting, so moving pub/sub or the queues to dedicated instances later is configuration. Redis is never the source of truth, so its loss degrades features without losing data.

Rejected alternatives: a replica from day one (pays for failover on a component whose loss already cannot lose data); a functional split now (isolation with no load to isolate).

## Queues: sorted sets with Lua pops, PostgreSQL as truth

The job queue and the realtime admission queue are Redis sorted sets scored by tier then enqueue time, popped atomically with a small Lua script. Priority is native, queue position for the waiting room is one ZRANK, and recovery is rebuilding the set from job and session rows.

Rejected alternatives: Redis Streams (delivery tracking that duplicates what the PostgreSQL rows provide, and priority needs a stream per tier); Celery or RQ (assume queue consuming worker processes, but our workers hang off WebSocket connections).

> Shipped status (2026-07-30): **partially implemented.** Generation jobs use an in-process heap rebuilt from PostgreSQL. Redis sorted sets, the realtime admission queue, cancellation, fairness, and adapter parity are not implemented; the governing issue is "Redis-optional Queues and FrameBus contracts".

## Realtime wire format: binary frames, JSON control

WebSocket text messages carry JSON control traffic, readable in browser devtools; binary messages carry WebP image payloads behind a small fixed header. No base64, so a third less bandwidth exactly where the 2 to 4 fps flows in both directions.

Rejected alternatives: msgpack for everything (compact but undebuggable control traffic for trivial savings); JSON with base64 images (a third more bandwidth on every frame, forever).

## GPU targets: CUDA and ROCm supported from day one

The worker ships two image variants, CUDA (NVIDIA) and ROCm (AMD), behind one `DEVICE` setting, plus a CPU mode for CI and GPU-less contributors. The cloud fleet stays entirely CUDA, since rented GPU providers are NVIDIA; ROCm serves self-hosters with AMD cards and the reference development desktop, which is AMD and becomes the standing ROCm test machine. The cost accepted knowingly: a second platform to keep working, untestable in CI, verified manually before each release.

Rejected alternatives: CUDA only (the primary development machine could then never run real inference); ROCm as an unofficial best-effort target (would serve the desk but leave AMD self-hosters in an ambiguous, undocumented state).

## Development loop: dependencies in containers, applications native

PostgreSQL, Redis, MinIO and Mailpit run from a dev compose file; the API server, frontend dev server and worker run natively with hot reload and debugger access. Only PostgreSQL starts by default, since the native loop keeps its queue and relay in process and stores assets locally; the other three are cloud-profile substitutes behind `--profile cloud-sim`. The containerized applications are still exercised by the cloud simulation, CI image builds and pre-release runs of the shipped compose file.

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

## Stored outputs: PNG masters, WebP thumbnails and frames

<!-- corrected 2026-07-23: header was "Stored outputs: PNG" but the code shipped WebP -->
<!-- corrected 2026-07-29: issue #125 shipped, so the code matches the original decision again -->

The stored master is **lossless PNG**, written by Pillow with no extra dependency: universal, no quality knobs, and the archival copy of the user's work. Cloud storage cost is bounded by the retention decision rather than by the format.

Everything that exists to be looked at rather than kept is **WebP**: the derived thumbnails the gallery displays, and the realtime frame stream, where bytes on the wire decide the frame rate.

Masters written before this shipped are still WebP and stay that way. There is no backfill: history and share links serve whatever `mime` and `storage_key` the asset row records, so a mixed bucket is normal and nothing reading an asset may assume the extension.

One consequence worth recording: the largest master the fleet can produce is a 4x upscale of a 1024 px image, which measures about 19 MB losslessly and reaches 50 MB on incompressible detail. The local upload route's ceiling exists to bound abuse and has to stay clear of that, so it moved from 20 MB to 64 MB with this change.

Measured on a real 1024 px generation, so the next person reconsidering this does not have to re-derive it. At 1024 px, and at 4096 px for the largest upscale the fleet produces:

| Format | 1024 px | 4096 px | Encode at 4096 | Pixels |
|---|---|---|---|---|
| PNG | 2.19 MB | 19.20 MB | 4.0 s | lossless |
| WebP lossless | 1.58 MB | 12.64 MB | 7.0 s | identical to PNG, verified |
| WebP lossy q80 | 0.26 MB | 1.17 MB | 0.8 s | lossy, permanent |

Lossless WebP is therefore 28 to 34 percent smaller than PNG for byte-identical pixels, at roughly twice the encode time, and it would fit the original 20 MB upload ceiling. It is the obvious move if stored bytes ever cost real money, and it pairs with converting to PNG only on an explicit download, which is lossless from that source. It is not worth a format migration and a conversion path today, when retention already bounds the cost.

Rejected alternatives: format as a request parameter (two code paths and a decision pushed onto every caller, for flexibility nobody asked for); JPEG for the master (lossy, and no alpha); keeping WebP for the master too, which is what shipped between 2026-07-23 and this change, and which left the archival copy quietly lossy; storing lossy WebP and converting to PNG on download, which sounds like a saving but hands the user PNG's size wrapping already-discarded detail, and compounds on every edit and upscale that re-encodes from the master.

## Model manifests: JSON

Manifests are JSON files. The `parameters` field is JSON Schema, so the manifest is JSON all the way down, the standard library parses it, and the API can return it verbatim from `GET /api/v1/models`.

Rejected alternatives: YAML (nicer to hand-edit, but a pyyaml dependency and JSON Schema embedded in a second syntax); TOML (stdlib readable, but deeply nested schema objects are genuinely awkward in it).

## Drawing surface: bitmap canvas

<!-- corrected 2026-07-30: issue #54's operation-journal direction supersedes the earlier snapshot-stack undo choice; the live bitmap choice stands -->

Use one 512 by 512 bitmap canvas for live interaction and encode complete WebP frames with native `canvas.toBlob()` for the realtime wire. The browser records the same canonical pointer samples as ordered, stable-ID operations and stores compressed raster checkpoints to bound replay time. Undo replays operations after the nearest checkpoint, and refine rerasterizes the journal at the requested target resolution. Vector paths are reserved for selections, masks, text, shapes, and authored objects that need later transforms; model-generated output remains raster.

Rejected alternatives: a raw snapshot stack (1 MiB per 512 by 512 RGBA level before overhead, without target-resolution rerasterization); an SVG live surface (still requires rasterization before every model frame and does not give generated pixels semantic object identity); a pure vector document (cannot faithfully represent paint, eraser, smudge, imported rasters, and diffusion output).

> Shipped status (2026-07-30): **not yet implemented.** Issue #3, "Drawing interface", owns the live bitmap tool and issue #54, "stroke-op replay log", owns the operation journal, checkpoints, replay, and undo.

## First public release: after the walking skeleton, API level

v0.1 tags when the M2 acceptance demo passes: a generation POSTed against the real worker completes end to end and CI's tiny-model CPU path is green. Self-hosters get the compose file and a working generation API, clearly marked pre-alpha. The point is early outside installs exercising the risky part, GPU setup on CUDA and ROCm, months before the UI is impressive.

Rejected alternatives: first tag after M3 drawing (a better first impression, but zero outside feedback on installation pain in the meantime); after M4 accounts (`AUTH_MODE=none` already covers the single-user install, so accounts gate nothing).

## Frontend foundation: CSS custom properties and a hand-rolled i18n store

The theme system (issue #1) is built on hand-rolled design tokens as CSS custom properties, with dark and light driven by `prefers-color-scheme` plus a `data-theme` override; components are plain Svelte. Internationalization is two JSON dictionaries behind a tiny store, roughly thirty lines. Unit tests run under Vitest; a browser end-to-end rig arrives with the drawing issue, when there are real flows worth driving.

Rejected alternatives: Tailwind (fast iteration, but the theme system, which is the entire point of issue #1, becomes Tailwind's); a component library (fastest to decent, hardest to make not look templated); Paraglide or svelte-i18n (typed messages and ICU plurals for what is today two flat dictionaries of static strings, adopt one the day plural-heavy content appears).

The Tailwind and component-library rejections are superseded by "Frontend components: shadcn-svelte on Tailwind" below, after the first hand-rolled version was judged against a real deployment. The i18n and Vitest choices stand.

## Low VRAM operation: the diffusers offloading ladder, not airLLM

Models larger than a card's VRAM run through a per-model memory ladder in the worker: full residency when the pipeline fits, model CPU offload when only the largest component fits, group offloading with stream prefetch (and disk spill when system RAM is short) below that. All rungs are native diffusers and accelerate features, so the ladder is configuration of the already-chosen inference stack, not new machinery. The rung is picked automatically from measured free VRAM at model load and can be pinned with the worker's `MEMORY_MODE` setting. Only full residency meets the 2 to 4 fps realtime bar, so lower rungs advertise the model without its `realtime` capability; queued jobs tolerate the slowdown, drawing sessions never see it. This mainly serves self-hosters on consumer GPUs; the cloud fleet rents cards sized for full residency. Details in [architecture.md](architecture.md).

Rejected alternatives: adopting airLLM itself (it targets transformer LLMs through the transformers library and cannot drive diffusion pipelines; its layer streaming and prefetching ideas are exactly what diffusers group offloading already implements for our models); a custom layer streamer (reimplements accelerate's hook machinery for zero gain); requiring full residency (locks self-hosters with 4 to 8 GB cards out of larger generation models, on the exact deployment the project exists to serve).

## Model routing: request tiers resolved in the API, no difficulty classifier

When a generation request does not pin a `model_id`, the API resolves the cheapest registered model whose tier, capabilities and parameters satisfy the request; manifests carry a `tier` field (`draft`, `standard`, `premium`). Our workloads announce their own difficulty through the interface: a drawing stroke is realtime and lands on a turbo-class model, a refine click is a queued job and routes to a heavier one. The router is a small selection policy inside the existing dispatch path.

> Shipped status (2026-07-23): **not yet implemented.** The wire `Manifest` has no `tier` field, and `POST /api/v1/generations` requires an explicit `model_id`; there is no routing path. This entry describes a designed policy, not current behavior.

Rejected alternatives: an ML difficulty classifier in front of the models (burns GPU time to guess what the UI action already states, and misclassification is user-visible); a separate routing proxy service (a deployment and a failure mode for what is one function in the API).

## Worker performance: compile and channels_last at warmup

Hot-set models use `channels_last` memory format when loaded. `torch.compile` and diffusers' `set_attention_backend` are implemented behind worker settings (`TORCH_COMPILE`, `ATTENTION_BACKEND`) and stay off by default: on the reference ROCm card they measured only ~0-7% warm denoise gain against multi-minute cold loads (PR #141), so the earlier "fifth to a third" expectation is CUDA-oriented and not the self-host default. Operators may opt in; a CUDA fleet bake-off can flip the default if the priced GPU seconds justify the cold-start tax.

> Shipped status (2026-07-23): `channels_last` is applied at load. `torch.compile` and `set_attention_backend` exist behind env settings and default off after the ROCm A/B (PR #141; tracking #60). The default path stays PyTorch SDPA plus the ROCm AOTriton env flag.

Rejected alternatives: forcing compile on for all devices after the ROCm measurement (pays startup cost for noise-level realtime wins); TensorRT or similar vendor toolchains (real gains, but a per-vendor build matrix against our CUDA plus ROCm promise, revisit if fleet economics demand it).

## Image codecs off the event loop

WebP and PNG encoding and decoding, the main per-frame CPU cost in both the worker and the API relay, always run in a thread executor, never on the asyncio loop. This keeps frame pacing and WebSocket heartbeats steady at the 2 to 4 fps bar. Binding for issues #15, #16 and #19.

Rejected alternative: SIMD image libraries (pillow-simd, libvips) before a profile shows Pillow in an executor is the bottleneck.

## Job placement: offloaded workers first, micro-batching deferred

<!-- corrected 2026-07-30: the single-region rejection is superseded by "Realtime fleet: regional, pool-partitioned, and warm" below; the offloaded-worker preference and scheduler-level micro-batching deferral stand -->

When several workers can take a queued job, the scheduler prefers workers serving the model on a lower memory ladder rung, keeping fully resident workers free for realtime admission, which only they can serve. One comparator in worker selection. Micro-batching same-model queued jobs is deliberately deferred until a real cloud fleet exists: it raises throughput but complicates slot accounting and preemption, and at a one or two GPU scale there is nothing to batch.

Rejected alternative: latency or geography aware placement (a single-region fleet at launch scale has nothing to optimize).

## License: GPL-3.0 stays, AGPL rejected

> **Superseded** by "License: AGPL-3.0 with commercial dual licensing" below (issue #109, PR #110, 2026-07-17). Kept for the record; the present-tense claims in this entry are no longer in force.

The public repository remains GPL-3.0. The cloud runs the same unmodified GPL images, so GPL's lack of a network clause costs the project nothing; the closed layer (billing, autoscaler, infrastructure) is protected by being separate processes in a private repository behind HTTP boundaries, not by the license. Full analysis in [repository-boundary.md](repository-boundary.md).

Rejected alternative: AGPL-3.0 as a defense against competitors hosting the product. A competitor hosting unmodified AGPL code owes nothing beyond pointing at already-public source; AGPL only forces disclosure of modifications, while its adoption stigma (many organizations ban AGPL dependencies) would hurt exactly the self-hosted community the license exists to serve. The moat is the closed business layer and operations, not copyleft strength.

## Cloud infrastructure code: private repository

The Terraform environments, state, sizes and account wiring live in the private repository alongside the billing service and autoscaler; they are commercial operational data. The public [aws-setup.md](aws-setup.md) guide stays, documenting how anyone could stand up their own cloud. The public repository's `deploy/` carries compose files only.

Rejected alternative: public Terraform under `deploy/terraform/` (as earlier drafts sketched). It would publish the commercial deployment's exact shape and sizes for zero community benefit, since a self-hoster deploying to AWS follows the guide with their own parameters anyway.

## Usage metrics: per-event user-linked rows plus a CLIP output categorizer

Every completed job and closed realtime session writes one user-linked row (action, model, tier, output category, gpu_ms, duration) to a `usage_events` table in the deployment's own PostgreSQL, in both modes; the worker attaches a category from a CLIP zero-shot pass over the output image at generation time. Per-event user-linked rows are what retention, cohort and funnel analysis need, which is what investors ask; the CLIP pass is nearly free because SD-class pipelines already hold a CLIP encoder and the image is already in memory. No prompts, images, IPs or user agents are stored; rows die with the account purge and appear in the GDPR export. Specified in [metrics.md](metrics.md).

Rejected alternatives: a third party analytics product (PostHog, Amplitude: client side trackers and data sharing contradict the no-cookies posture and add a dependency); daily aggregates only (privacy-trivial but cannot answer retention or cohort questions); classifying the prompt text instead of the output (prompts are short, misleading or absent in drawing and enhance flows); pseudonymous ids (loses the join to plan and cohort, which is the point of the exercise).

## Usage event retention: 90-day raw rows plus daily user rollups

Raw `usage_events` are retained from the UTC midnight 90 days before maintenance
runs. Before older complete days are pruned, the existing five-minute maintenance
loop replaces an idempotent `usage_event_rollups` row for each user, UTC day,
kind, action, model, tier and category with the raw count and numeric sums. The
rollup and prune commit in one transaction. Ninety days keeps a substantial
recent window of event-level session and funnel detail while placing a direct
bound on the table daily telemetry and the future admin view scan most often.
The window is fixed because both profiles need the same metric semantics;
event-rate differences change the bounded raw volume rather than the retention
contract.

The UTC alignment means 90 to 91 days of arrivals remain, or about 90 x `R` to
91 x `R` raw rows at an average `R` completed events per day.

The daily per-user grain preserves the question the table exists to answer: did
this user return in a later period. Daily presence can be regrouped into DAU,
WAU, calendar periods or signup-relative first-week cohorts; a coarser stored
bucket cannot recover those boundaries. The dimensions preserve category, model,
tier and action retention, while count, category-score count and sum, gpu_ms,
duration and frames preserve the additive usage measures. Per-user annual row
count is the sum of distinct dimension tuples used on each active day: 365 rows
for a daily user with one tuple, or 365 x `D` when that user uses `D` tuples every
day. The rollup is long-lived, but its growth is periodic and dimension-bounded
rather than per completed event.

`usage_event_rollups.user_id` uses the same `ON DELETE CASCADE` as raw events.
The rollup is personal data, dies with the account purge, and belongs in the GDPR
export when issue #10 implements it. This retains the existing privacy
commitment; an aggregate-only rollup would not, because a later purge could not
remove that user's contribution.

The existing `usage_events_created_at` index remains: it serves both the
maintenance rollup/prune range and telemetry's previous-day range. The unique
rollup key serves the idempotent conflict update and user-scoped cohort/GDPR
reads; its leading `user_id` also supports the cascade lookup. A separate
`usage_event_rollups_bucket_date` index serves cross-user period scans for cohort
and admin aggregation.

Rejected alternatives: no retention, which leaves per-event growth unbounded;
plain deletion after the raw window, which destroys returning-user and cohort
history; aggregate-only rollups, which cannot identify a returning user and
cannot remove one person's contribution on purge; weekly or monthly user
rollups, which cannot reconstruct daily activity or signup-relative first weeks.

## Telemetry: opt-out anonymous aggregates from self-hosted installs

Supersedes "Telemetry: none from self-hosted installs". Self-hosted installs post one anonymous daily aggregate (counts by action, category and tier, active user count, worker device and memory mode, version, random install id) to a project ingest endpoint, on by default, disabled with `TELEMETRY=false`. Three properties keep opt-out defensible to a GPL audience: the payload is aggregates only and joinable to no person, the exact payload is documented publicly and previewable locally, and the API logs the destination and the off switch at every startup. A failed send is dropped, never queued. Specified in [metrics.md](metrics.md).

Rejected alternatives: keeping zero phone-home (the original decision: cleanest position, but it makes the install base invisible exactly when install counts and usage mix are the numbers the project needs to show); opt-in (single digit opt-in rates make the data unusable); local-only metrics with no reporting (same blindness with extra steps).

## Credit lifecycle: balance resets each billing period

Each paid invoice sets the balance to the tier's grant; unused credits expire when the period ends. A failed renewal claws nothing back: the remaining balance stays spendable until the period ends, Stripe's retries and dunning emails run in that window, and if payment never lands the subscription cancels and the account drops to the free tier with a zero balance. No one-time top-up packs at launch; heavy users change tier through the hosted portal. One payment flow, the simplest possible ledger, and liability bounded by one month's grant per user.

Rejected alternatives: capped rollover (friendlier to light users, but more ledger rules and standing liability; revisit if churn data blames expiry); credits that never expire while subscribed (unbounded accrued liability and a dunning claw-back problem); top-up packs at launch (a second Checkout flow, fulfillment webhook, refund path and expiry rule before any real user has hit a ceiling).

## Payment processing: idempotent by construction

Stripe delivers webhooks at least once and out of order, and HTTP calls between the API and the billing service will be retried, so nothing about payments may depend on exactly-once delivery. Three rules make every money path safe to replay. Webhook events are recorded in a table keyed by the Stripe event id and inserted in the same transaction that processes them, so a redelivery hits the unique constraint and no-ops; handlers read state from the event's object, never infer it from event ordering. The credit ledger is append-only: every balance change is one row (user, delta, reason, source type, source id) with a unique constraint on the source, so a renewal grant keyed by its invoice id, a trial grant keyed by the user, and a spend keyed by its reservation id physically cannot apply twice; the balance is a cached column rebuilt from the ledger. Refunds claw back as negative entries keyed by the refund id (a balance may go negative, which only blocks new reservations until it recovers), and a chargeback dispute suspends the account pending manual review.

Rejected alternatives: deduplicating in handler code without constraints (works until a crash lands between the side effect and the marker); the balance column as the source of truth (unauditable and unrepairable when it drifts).

## Quota contract: caller-supplied reservation ids with expiry

`reserve` carries a reservation UUID generated by the API and a TTL, so a timed-out call can be retried with the same id and at most one reservation exists. A reservation is a one-way state machine, reserved to exactly one of committed, refunded or expired: repeating a transition is a no-op, a conflicting one is an error, and the billing service expires uncommitted reservations after the TTL and returns the credits, so a crash between reserve and enqueue cannot strand them. Realtime sessions meter through the same contract in chunks: reserve roughly 60 GPU-seconds at admission, extend chunk by chunk while Active, commit actuals at idle release or close. A failed extension ends the session gracefully with an out of credits message, so overdraft exposure is bounded by one chunk. The fake QuotaService in cloud-sim implements these exact semantics; they are part of the versioned `/v1` contract, not private implementation detail.

Rejected alternatives: server-generated reservation ids (a timeout on reserve leaves the caller unable to retry safely, which is the whole failure mode); per-tick metering for sessions (couples billing to the frame loop and multiplies contract calls for no precision that matters); trusting commit to always arrive (a crashed worker or API would leak reserved credits forever without the TTL).

## Billing outage posture: reserve fails closed, settlement retries through an outbox

When the billing service is unreachable, `reserve` fails closed: the user sees a billing-unavailable error and no GPU time is granted on credit. `commit` and `refund` fail open: they enqueue in an outbox table in the API's PostgreSQL and retry until acknowledged, and the ledger's unique source keys make redelivery harmless, so settlement is effectively exactly-once. A billing outage therefore never hands out free GPU time and never loses a finished generation's charge, in that order of importance.

Rejected alternatives: failing open on reserve (an outage becomes a free GPU faucet precisely when nobody is watching); synchronous retries without persistence (an API restart mid-retry loses the charge).

## Realtime relay: planned extraction into a Go gateway at scale

<!-- corrected 2026-07-30: superseded by "Realtime relay: Go gateway required for the 1000-active-session target" below; kept for the record because its ownership split and Go rationale still stand -->

The Redis pub/sub relay between API replicas stands, and no gateway code exists today. This entry records the exit plan for the day profiling shows relay frame pacing threatening the 2 to 4 fps bar or relay work crowding API replicas: a stateless gateway service terminates the browser realtime socket and the worker fleet socket, relays binary frames in memory when both legs land on the same instance (affinity by session id) and over Redis pub/sub otherwise, and forwards JSON control traffic to the API, which keeps the scheduler and all authority. Browsers authenticate to it with short-lived tickets minted by the API; workers keep their fleet tokens. The ALB already routes by path, so `/api/v1/realtime` and `/api/v1/fleet` move to the gateway's target group without touching anything else; the FrameBus seam and the no-stickiness design are what make the split configuration plus one new service rather than a redesign. The gateway is written in Go: many sockets, small messages, no model code, a static binary, exactly the shape Go serves best. The API stays Python per its own decision; within this repository the gateway is the only planned Go component. The private repository's services choose their own stack behind the HTTP contracts.

Rejected alternatives: building the gateway now (a deployment and a duplicated auth surface before any profile justifies it, the same reason the frame routing decision rejected it); a full Go port of the API (recreates the two-language backend the FastAPI decision exists to avoid, spending a rewrite on headroom the GPU-bound economics cannot use, since session count and therefore relay load track fleet size, which tracks revenue).

## Cloud delivery: Terraform and push-based pipelines, no Kubernetes

All infrastructure is Terraform in the private repository, and git is the source of truth for both the infrastructure and the deployed image digests; nothing changes in the console outside break-glass. Delivery is push-based: GitHub Actions assumes per-environment IAM roles through OIDC (no long-lived AWS keys exist), `terraform plan` posts on every pull request, merging applies to staging, and production waits for a manual approval on the pipeline. Services roll with ECS's native rolling update plus the deployment circuit breaker and alarm-based rollback; the ALB's 120 second deregistration delay drains WebSockets during deploys. A scheduled `terraform plan` fails loudly on drift, which is the useful half of GitOps done as a nightly check instead of a resident controller.

Rejected alternatives: EKS with ArgoCD or Flux (pull-based GitOps needs a Kubernetes cluster to reconcile; that is a monthly control plane bill and standing cluster operations for three stateless services and one migration task, while the GPU fleet lives outside AWS and outside Kubernetes reach anyway); AWS CodePipeline (a second CI system next to GitHub Actions for no capability gain); blue/green through CodeDeploy (doubled capacity during deploys and extra machinery for rollback the circuit breaker already provides at this scale); automatic promotion to production after a staging soak (trusts alarm coverage that does not have history yet; revisit once it does).

## AWS accounts: an Organization with staging and production members

An AWS Organization with two member accounts, staging and production; the management account holds consolidated billing, the organization CloudTrail and nothing else. Account boundaries make blast radius and IAM trivial - a staging mistake cannot touch production by construction - and each account gets its own OIDC deploy roles and its own Terraform state bootstrap. Humans go through IAM Identity Center with short-lived credentials: read-only for daily inspection, administrator as break-glass only. The full access model is in [cloud-delivery.md](cloud-delivery.md).

Rejected alternatives: a single account separated by names and tags (soft IAM boundaries, and splitting into accounts later is a painful migration); Control Tower (audit and log-archive accounts plus SCP guardrails are enterprise machinery this scale does not pay for; guardrails can be added to the plain Organization later).

## Content safety enforcement: strikes without prompt retention

Deepens "Content safety: prompt screening and output checking in the cloud". The screen is normalize (unicode folding, homoglyphs), then curated combination rules, then a lightweight CPU classifier, in that order, before quota reserve and on every realtime prompt update - mechanics in [blueprint.md](blueprint.md). The enforcement posture: hard-category attempts (above all, any sexualization of minors) are refused with one deliberately generic message and recorded as strikes holding category and timestamp only - prompt text is never retained anywhere, consistent with the metrics privacy posture. Repeated strikes suspend the account behind the same pending-review flag the payment dispute path uses. Soft categories get a clear message and no strike. The baseline rule list is public so self-hosted installs that enable `SAFETY_CHECKS` get real protection; the cloud appends a private supplementary list.

Rejected alternatives: storing flagged prompts for human review (creates exactly the sensitive archive the no-prompt posture exists to avoid, and GDPR-scopes it); LLM-based moderation per prompt (latency and cost in a path that must also gate 2 to 4 fps prompt updates); silent shadow-banning (a support nightmare that teaches abusers nothing and honest users less); detailed refusal messages for hard categories (an oracle for evasion testing).

## GPU session density: calibrated slots now, worker-internal batching later

<!-- corrected 2026-07-30: the density-research deferral is superseded by "GPU session density: capacity-critical at 1000 active sessions" below; calibration and the scheduler slot abstraction stand -->

Realtime slots per worker stop being a configured guess: at model warmup the worker times single frames on the resident realtime model and advertises the largest session count whose serialized inter-frame time still meets the 2 fps floor (floor of the 500 ms budget over single-frame p95), capped by the configured REALTIME_SLOTS. Sessions serialize on the worker GPU lock; a batch-size sweep waits on the deferred cross-session batching ladder below. This lands with the real inference issue and replaces the most expensive guess in the system with a measurement. The density ladder beyond that is designed and deliberately deferred: cross-session frame batching inside the worker (frames from concurrent same-model sessions collected in a ~30-50 ms window and run as one batch - invisible to the scheduler, because it lives below the slot abstraction, which is why this does not reopen the deferred scheduler-level micro-batching decision), then StreamDiffusion-class pipeline work (batched denoising steps across consecutive frames, dropping classifier-free guidance on turbo models, tiny-autoencoder decode for the live preview with full VAE on refine). Trigger: when fleet spend makes density the cheapest capacity, which is measurable from the machine-hour accounting.

Rejected alternatives: implementing batching in the launch scope (worker complexity before concurrent users exist to batch, on a one or two GPU fleet); keeping static guessed slots (leaves per-GPU economics unmeasured through exactly the period when pricing is being validated).

## Fleet card: chosen by bake-off, not assumption

When real inference lands, rent an RTX 4090, an RTX 5090 and an A40 for an afternoon and measure the numbers that matter: sessions held at the realtime bar per dollar-hour, and queued images per dollar-hour. The winner becomes the launch card. The metric is $/session-at-bar, not $/hour - a cheaper card that fails the bar or a pricier card that doubles sessions can each win. The scheduler and autoscaler are card-agnostic (slots are the only currency), so this is fleet configuration, not code.

Rejected alternatives: committing to the 4090 unmeasured (probably right, but "probably" on the number that dominates COGS); optimizing pure $/hour (the A40 is cheapest per hour and likely worst per session at the bar).

## AWS baseline: Graviton, Valkey, S3 endpoint, NAT instance

Four cost decisions that touch no architecture: ECS tasks run on Graviton (ARM64, multi-arch images, roughly 20 percent off Fargate compute); ElastiCache runs the Valkey engine (protocol-compatible with Redis, zero application change, 20 to 30 percent cheaper); an S3 gateway VPC endpoint (free) keeps S3 traffic off NAT data charges; and outbound NAT is a small NAT instance (fck-nat pattern on t4g.nano) instead of the managed NAT gateway, saving roughly 30 USD per month as the one deliberately accepted pet in an otherwise pet-free design. A CloudWatch log-ingestion alarm guards the classic runaway. Together roughly 30 percent off the pre-GPU baseline.

Rejected alternatives: Fargate Spot for API replicas (WebSocket churn on every reclaim for about 10 USD per month at launch scale; revisit with the relay gateway); keeping the managed NAT gateway (simplest, but 35 USD per month for a single-AZ launch posture that already accepts bigger risks than a NAT instance).

## GPU floor: scheduled, not always-on

The always-on worker floor follows a schedule: floor 1 during European waking hours, floor 0 overnight at launch. A quiet-hour first session sees the existing waiting room for the one to two minutes a machine takes to boot - a bounded, honest UX cost that saves roughly a third of the floor machine's monthly cost. The schedule is autoscaler configuration per environment; raising it as the user base spreads across timezones is a config change informed by the admission-wait metric.

Rejected alternatives: a 24/7 floor (best first impression at every hour, full cost from day one before there are night users to impress); pure scale-to-zero (daytime users also hit cold starts whenever demand gaps outlast the idle timeout).

## Fleet metrics: over the heartbeat, aggregates in CloudWatch

GPU hardware metrics (utilization, VRAM, temperature, power) are sampled by the worker via NVML or amd-smi and ride the existing 30 second heartbeat - rented machines export monitoring over the one outbound connection they already hold and are never AWS principals. The API fans each heartbeat out three ways: the worker's Redis hash (the live admin fleet view and the autoscaler), fleet-level CloudWatch aggregates (worker count, slot usage, average and max utilization, minimum free VRAM), and one JSON log line per worker for history. Multi-GPU machines run one worker process per GPU by device index, so every GPU is individually one connection, one heartbeat and one slot set. Frame-loop numbers (per-model p95 frame time at worker and relay, drop rate) are first-class metrics because the slot calibration and the gateway-extraction trigger read them. Specified in [metrics.md](metrics.md).

Rejected alternatives: a metrics agent on rented machines (the CloudWatch agent needs AWS credentials on untrusted hardware - never); Prometheus and Grafana (already rejected under Observability; the revisit trigger is fleet size making aggregate-level CloudWatch blindness expensive, around scaling stage 2); per-worker CloudWatch dimensions (ephemeral worker ids times metric names is a paid cardinality explosion, and Redis plus logs already hold the per-worker detail).

## Frontend components: shadcn-svelte on Tailwind

Supersedes the Tailwind and component-library rejections in "Frontend foundation" above, decided after the first fully hand-rolled landing shipped and its styling read as generic. shadcn-svelte vendors component source into the repository (the code is owned, not imported), sits on headless bits-ui primitives, and its semantic token system (`--background`, `--primary`, ...) plays the role the hand-rolled custom properties played - the theme is still ours, expressed as one variables block in `app.css`. Dark values live on `:root` (dark is the designed theme; light later is a variable block, not a redesign). The hand-rolled i18n store, the bundled Space Grotesk, and the canvas hero survive unchanged; glowing shadows do not, and the hero headline gradient is the one deliberate flourish kept.

Rejected alternatives: keeping everything hand-rolled (every new surface repays the same styling tax, and the first attempt demonstrated the failure mode); a styled component library like Skeleton or Flowbite (themes owned by the library, harder to leave); headless bits-ui directly without the shadcn layer (saves nothing - shadcn is that layer, pre-written).

## Stability Community License models in the product

Updated 2026-07-25: the operator holds Stability AI Community License registration, but `sd-turbo` and `sdxl-turbo` both ship with `benchmark_only: true`. Quality at the shipped resolutions is not good enough for the studio picker; they remain loadable as benchmark speed anchors (issue #60). Revisit only if a higher default resolution (e.g. 1024 for SDXL Turbo) is measured and accepted.

The same $1M annual revenue cap still applies (Stability AI Community License); above that threshold the community license terminates and an enterprise license is required. Commercial use still requires registration at stability.ai/community-license and prominent "Powered by Stability AI" attribution. Manifest fields (`license_id`, `commercial_max_revenue_usd`, `requires_attribution`) cross the wire for future cloud-side gating. Details in [third-party-models.md](third-party-models.md).

Earlier shipping briefly set both to studio-visible after registration. Rejected alternative: deleting the manifests. They still give honest comparison points on `/benchmark`.

## Cloud asset storage: one bucket, prefix per tier

All cloud images live in one private S3 bucket. Subscriber objects sit under `users/{user_id}/`; trial objects under `trial/{user_id}/` so an S3 lifecycle rule can expire the trial prefix after 30 days as a backstop to `expires_at` on asset rows. The API authorizes access: it mints short-lived CloudFront signed URLs only for assets the session owns. History queries the `assets` table, never `ListBucket`.

Paying does not create AWS permissions for the user. Quota changes happen in the billing service over HTTP; storage authorization stays at the API layer.

This entry records the cloud-profile design. The current S3 backend still uses the self-hosted key shape (`{user_id}/{job_id}.webp`) and presigned S3 GET URLs; the prefixes and CloudFront signing arrive with billing tiers and the CDN.

Rejected alternatives: per-user IAM roles or buckets (account limits near one thousand of each, privileged control-plane calls on signup, authorization at the wrong layer); per-user S3 Access Points (ten-thousand cap, same wrong layer).

## Fast batch tier: SSD-1B + Lightning alongside SDXL Fast

`ssd-1b-lightning` is a studio-shippable fast batch model. Issue #85 compared it against `sdxl-fast` on the shared three-prompt photorealistic suite at 1024/8step (clean GPU, RX 7600 XT): median 2777 ms gpu_ms vs 4005 ms for sdxl-fast (~31% faster), with comparable visual quality. Licensing matches the existing fast path (Apache 2.0 pruned base plus the same ByteDance SDXL Lightning LoRA `sdxl-fast` already fuses). It ships alongside `sdxl-fast`, not as a replacement: full SDXL base may retain edge-case quality; SSD-1B + Lightning wins on speed and fits the 8 GB floor.

Rejected alternative: keep `ssd-1b-lightning` benchmark-only after the successful fuse (issue #75 expected failure; the measurement would be lost).

## License-clean realtime model: VegaRT

`vega-rt` is the studio-shippable realtime model. Issue #75 measured median 381 ms gpu_ms at 512/2 t2i on the RX 7600 XT (clean GPU), within turbo-class range of the Stability benchmark anchors, under Apache 2.0 with no revenue cap. Issue #84 verified the realtime img2i frame path at warm median 452 ms (~2.2 fps) @ 512 with strength 0.7 on the same hardware. The manifest exposes `text_to_image`, `image_to_image`, and `realtime` with an LCM scheduler and fused VegaRT LoRA.

Rejected alternatives: Hyper-SD (sdxl-hypersd) as the fast SDXL path (issue #75). Corrected 2026-08-24: the reason originally recorded here, that the Hyper-SD LoRA carries no declared license, was wrong. `ByteDance/Hyper-SD` ships one multi-license `LICENSE.md` keyed by model family; the SDXL and SD1.5 LoRAs fall under CreativeML Open RAIL++-M, the same family as `sdxl-base` and the `ssd-1b-lightning` LoRA already shipped, with no revenue cap (see [third-party-models.md](third-party-models.md)). The rejection stands on measurement instead: `sdxl-hypersd` runs 3.77 s at 1024/8, within 0.01 s of `sdxl-fast`, so it is not a turbo-class win and promoting it would add a model without adding a capability. sd-turbo / sdxl-turbo stay benchmark-only for quality (see "Stability Community License models in the product"). VegaRT is the license-clean realtime default without a revenue cap.

## License: AGPL-3.0 with commercial dual licensing

Supersedes "License: GPL-3.0 stays, AGPL rejected". The public repository moves to AGPL-3.0 and the project sells commercial exceptions (COMMERCIAL.md). The earlier entry rejected AGPL as a competitive moat, and that reasoning still holds: AGPL does not stop a competitor hosting unmodified code, and the moat remains the closed business layer. The license changes anyway because the goal changed: companies that modify and operate the platform as a service must now either publish their changes or engage us commercially, turning the license into a funnel rather than a wall. Self-hosting, private use, internal use and contribution are unaffected, and the project's own cloud is unaffected because it runs unmodified images and the project holds the copyright. Dual licensing depends on retaining relicensing rights, so contributions require DCO sign-off from this point on (CONTRIBUTING.md).

Rejected alternatives: staying GPL-3.0 (leaves the modified-network-service path entirely open, and relicensing only gets harder as outside contributions accumulate); BUSL-1.1 with a revenue-threshold use grant or PolyForm Noncommercial (closest to a Stability-style community license, but both are source-available rather than open source, and the project's positioning spends that credibility everywhere from the hero badge to the whitepaper).

## SD 3.5 Medium: the quality tier, loaded through AutoPipeline

Issue #151. Stable Diffusion 3.5 Medium enters the roster as the quality text-to-image model, and it needs no new loader machinery: diffusers maps the repository's `model_index.json` to `StableDiffusion3Pipeline` through the `AutoPipelineForText2Image` the worker already calls, so the manifest declares nothing about its architecture. It is the first gated repository the project ships; the Hugging Face client reads `HF_TOKEN` from the environment, so the whole credential path is one compose variable and a self-hosting section, with no code in the worker. It ships `text_to_image` only. The Community License attribution obligation is met per model, rendering a manifest's `requires_attribution` beneath the studio model picker, which credits Stability exactly when a Stability model is selected. This does not reopen "Stability Community License models in the product": the turbo models stay benchmark-only for quality, and this entry adds a Community License model that earns its place on quality instead of speed.

Measured on the reference RX 7600 (gfx1102, 15.98 GiB) on 2026-07-26: full residency OOMs in both fp16 and bf16, because the fp16 component set is about 15.15 GB of weights before a single activation, so the issue's "full encoders comfortable on 16 GB" is wrong. The model-offload rung is the shipped 16 GB configuration, peaking at 12.09 GB (fp16) and 10.21 GB (bf16). `min_vram_gb` is 24: it keeps its documented full-residency meaning, it cannot be measured exactly on a card that OOMs, and it is bounded below by the measurement that 16 GiB is insufficient. That value makes the existing 0.55 largest-component fraction select model offload with a 13.2 GB threshold against a 12.09 GB measured peak, which is why no ladder override field was needed. Timing is 56 s at 20 steps and 89 s at 40 at 1024 px, a 22.6 s offload floor plus about 1.67 s per step, so 20 is the default: the quality difference against 40 at a fixed seed did not justify 33 seconds. The studio picker already renders the estimated time beside the model name, so the cost is visible before selection. bf16 saves 1.9 GB and one second with visually identical output, which did not justify a per-manifest dtype field.

Rejected alternatives: a `pipeline_family` manifest field or inferring the family from `source` (AutoPipeline already dispatches correctly, source inference breaks for mirrors and local paths, and the field would be a speculative seam maintained against upstream configuration); a `largest_component_vram_gb` override for the memory ladder's 0.55 largest-component fraction, whose UNet-dominant assumption T5-XXL breaks in principle (the measurement showed the heuristic picking the correct rung with margin, so the field would have been schema without a problem to solve); shipping `realtime` or `image_to_image` (realtime would need a distilled SD3 and would run a 21-frame calibration at every startup; i2i doubles the acceptance surface for no motivation in the issue); a site-wide attribution banner (it would credit Stability for output from Apache-licensed models too); teaching the engine to descend a rung after an OOM (a general failure-path state machine that changes every model's behavior, and a separate issue if measurement shows it is needed).

## SD 3.5 Medium: int8 T5 with bounded memory rung demotion

Issue #155 supplies the measurement that the earlier SD 3.5 decision required
before changing the failure path. The worker quantizes only the manifest-named
`text_encoder_3` with torchao weight-only int8, before moving the pipeline to
the device. T5-XXL falls from 9.12 GB to 4.57 GB, the resident pipeline is
10.93 GB, and generation peaks at 13.44 GB. `min_vram_gb` is therefore 14,
rounded up from the measured generation peak rather than from resident weights.
Full residency also makes `torch.compile` safe from accelerate offload hooks,
so a quantized full-resident manifest requires compile even when the global
opt-in is off. The measured reference path is 28.0 s at 1024 px and 20 steps,
against 49.5 s for the previous fp16 model-offload path.

The 13.44 GB peak leaves little room for a desktop session to vary. Under
`MEMORY_MODE=auto`, an out-of-memory error while loading descends from `full`
to `model_offload`, then to `group_offload`. A generation out-of-memory error
first keeps the existing behavior of evicting other residents and retrying. If
that retry also fails, the worker descends exactly one rung, reloads and tries
the job once more. Operator-pinned memory modes never descend, because a pin
is an explicit choice rather than a heuristic to correct.

Rejected alternatives: bitsandbytes (its gfx1102 kernel fails with
`Error invalid device function at line 432 in file /src/csrc/ops.cu`);
quantizing the other models (they already fit and measured 1.1 to 3.6 percent
slower); unbounded generation descent or retry (one bad job could repeatedly
reload a large pipeline); demoting an operator-pinned rung (silently ignores
explicit configuration).

## Prerendering: every known route is rendered at build time

Supersedes the `ssr = false` client-rendered shell in "Frontend: SvelteKit as a static SPA" above, without changing what that decision settled: there is still no server rendering at request time, and the build is still one static artifact that the API serves when self-hosted and a CDN serves in the cloud. What changed is that the same artifact also serves the public marketing site, where a shell containing no title, description or heading is the whole product a crawler and a social card ever see. SvelteKit prerenders known routes into complete documents, the client hydrates them, and the studio behaves exactly as before.

Two consequences are accepted deliberately. The prerendered language is English, so the locale preference is restored after hydration rather than during module initialization, which means a Spanish visitor sees English for one frame. Benchmark results load after hydration rather than being inlined into the prerendered document, keeping the page small at the cost of the results table not being crawlable; the surrounding explanation and the model specifications are.

Rejected alternatives: leaving the marketing routes client-rendered and accepting an empty shell in search results and social cards (the reason this project has a landing page at all is discovery); a separate marketing site or branch (rejected earlier and still rejected, since one codebase serving both surfaces is the point); server-side rendering at request time (needs a running server in front of the CDN, which the cloud profile deliberately avoids).

## Favorites: a timestamp on the job row

Favorites are persisted as nullable `jobs.starred_at`. The timestamp is both membership and newest-first ordering, the existing job owner scopes every operation, and job retention remains authoritative. This gives the implicit self-hosted user and future account users the same endpoint and database path.

Rejected alternatives: keeping UUIDs in browser localStorage, which strands favorites across browsers, reinstalls and regenerated rows; a separate favorites join table, which adds a join and lifecycle without buying many-to-many ownership because assets and their jobs have one owner.

## Benchmark history: PostgreSQL sessions and measurements

Supersedes issue #107's static-JSON-only position. Each completed suite run is one benchmark session with ordered per-model measurements in PostgreSQL, and reads are install-scoped because a benchmark measures the shared GPU rather than one person's work. The existing JSON artifacts remain a portable report and the public static page's fallback, while an installed studio can list and compare every retained run in either deployment profile.

Rejected alternatives: keeping session history solely as committed static JSON, where each run overwrites the previous report, publishing runtime data requires a source-control operation, and the session picker can never show more than one run; scoping sessions to the account that ran them, which would hide an install's own hardware history from everyone but whoever happened to trigger the benchmark.

## Roles: three tiers on the user row

Access is a single `role` column on `users` with three values, checked by a dependency beside `current_user` rather than inside each endpoint: `admin` (everything, including install configuration), `user` (the member tier: generate, star, upload, manage their own work) and `viewer` (read-only, including the metrics section and benchmark history). The `AUTH_MODE=none` local user is an admin, and an existing local user is promoted on startup, so a single-user self-hosted install behaves exactly as before. This covers the requirement that a friend on someone's install may look without spending their GPU, and the cloud requirement that install-wide reads are not open to every customer.

Rejected alternatives: a per-resource permission table, and admin-assignable per-tab grants, both of which add a matrix nobody has asked for and can be layered on these tiers later if a real need appears; no roles at all, which cannot express read-only access and leaves install-wide endpoints open to any authenticated account once the cloud has more than one.

## Model timings: observed per-install medians supersede shipped constants

A new install starts with the shipped reference-card GPU timings. Once a model has five eligible succeeded jobs, the median observed GPU speed from its latest 50 jobs supersedes that reference speed for the install. Five leaves four representative observations when one job is pathological without delaying convergence for a lightly used model; 50 smooths ordinary workload variance while allowing the cache to follow a hardware change, and only jobs finished within the last 30 days count, so the refresh reads a bounded slice rather than the whole of an install's history and a stale timing cannot outlive the hardware it described. Observations are normalized for each job's steps, dimensions or upscale factor before the median is taken, and the existing five-minute maintenance loop refreshes the derived in-memory cache. Jobs do not record their worker or memory mode, so the cache is keyed by model alone rather than inventing an unreliable join.

Rejected alternatives: shipping one machine's constants forever, which is wrong on every other hardware profile; calibrating on every boot, which spends GPU time and measures idle synthetic conditions rather than the real workload.

The refresh reads the newest succeeded jobs per model, which `jobs_user_created` cannot answer because it leads with `user_id`, so migration `0010` adds a partial index on `(model_id, finished_at DESC)` limited to the succeeded rows with a positive GPU time, which is the query's own filter.

## SPA/API compatibility: N-1 through expand-contract

Each API release tolerates the previous release's SPA. Response shapes follow the same expand-contract discipline as the worker protocol and database migrations: expand with new fields, move clients off old fields, then contract only after the N-1 window has passed. New-build polling offers the user a reload, but compatibility does not depend on taking it.

Rejected alternative: breaking response changes plus a forced reload. That makes SPA and API deploys lockstep and can discard in-flight work.

## Prompt token window: declared per manifest, silent when undeclared

The text encoder window a prompt is measured against comes from the manifest field `prompt_token_limit`, not from a constant in the frontend. The shipped CLIP based models declare 77; a model whose encoder differs declares its own figure and the studio warning follows it without a frontend change. An absent or zero value means the window is unknown and no warning appears, so a manifest that forgets the field fails back to the behaviour before the warning existed instead of asserting a limit its encoder does not have. Upscale manifests take no prompt and leave it unset. The count itself is estimated in the browser from words and punctuation rather than tokenized exactly, because the real CLIP tokenizer means shipping roughly a megabyte of BPE vocabulary to phrase a warning that only needs to be right within a few tokens.

Rejected alternatives: hardcoding 77 in the studio, which is correct only for the models shipped today and silently wrong for the first model with a larger encoder; defaulting the field to 77 so existing manifests need no edit, which turns a forgotten declaration into a confident false warning rather than silence; asking the worker for an exact count per keystroke, which spends a round trip on a hint; and bundling a real tokenizer, which costs more transfer than the feature is worth.

## Python dependencies: bounded ranges without a lockfile

Direct Python dependencies use bounded ranges from the supported floor to the next major version. This keeps contributor installs within tested release series while preserving the worker's device-specific installation path: CUDA wheels come from PyPI, while ROCm and CPU torch wheels come from the matching `download.pytorch.org` index.

Rejected alternatives: a lockfile, `uv` or `pip-tools`. A single resolved dependency tree cannot express the worker's different torch indexes by device, so these options would fight the documented install path.

## Realtime relay: Go gateway required for the 1000-active-session target

Supersedes "Realtime relay: planned extraction into a Go gateway at scale" above. The earlier entry's ownership split, FrameBus seam, no-stickiness design, and choice of Go stand; only the conditional timing is no longer in force.

The accepted design target of 1000 or more concurrently active drawing sessions makes the Go realtime gateway a prerequisite for that capacity, rather than an extraction attempted only after the Python API relay is saturated. The stateless gateway terminates browser realtime sockets and worker fleet sockets, relays binary frames through bounded per-session writers in memory when both legs are local and over the FrameBus otherwise, and forwards ordered JSON control to the Python API. The API retains authentication authority, authorization, quota, admission, scheduling, and durable state. Browsers use short-lived API-minted tickets; workers retain fleet credentials; correctness never depends on load-balancer stickiness.

At 1000 active sessions, 2 to 4 fps in each direction is a calculated 4000 to 8000 complete frames per second. Planning ranges of 10 to 40 KB for canvas inputs and 40 to 65 KB for generated outputs produce 100 to 420 MB/s of logical application payload, or 0.8 to 3.36 Gbps. If every frame crosses instances, the FrameBus receives and emits 200 to 840 MB/s, or 1.6 to 6.72 Gbps, before protocol and transport overhead. These figures are calculations from repository planning ranges, not load measurements.

Profiling remains the capacity and release gate. The 1000-session, 2 and 4 fps load sweep sizes the gateway and FrameBus fleet and measures relay p95/p99, drops, socket backlog, CPU, memory, network, control latency, and slow-peer isolation. It no longer decides whether the gateway exists.

Rejected alternatives: retaining the Python API relay until production saturation, which moves gateway extraction into the capacity ramp and leaves the accepted target without its required data plane; a full Go port of the API or gateway-owned authority, which would rewrite unrelated control-plane logic and break the recorded ownership boundary; load-balancer stickiness as a correctness mechanism, which cannot guarantee both socket legs remain together through reconnects and failures.

## Realtime and queue Redis seam: optional, behaviorally equivalent

Deepens "Realtime frame routing: Redis pub/sub between API replicas", "Redis topology: one instance, split-ready namespaces", and the Redis supporting default below. It defines the behavior shared by the existing in-process and Redis-backed paths without changing Redis as the cloud default.

Queues and FrameBus have in-process and Redis-backed implementations selected only by configuration. Both preserve admission states, priority and fairness, channel names, at-most-once complete-frame delivery, destination-owner-only subscriptions, explicit cancellation, and one latest value per session and direction. Redis legitimately adds multi-process scope, leader election, cross-replica delivery, shared rate limits, and shared invalidation. Without Redis, exactly one socket-owning process is permitted and PostgreSQL remains the account-session source of truth. No wire or user-visible queue behavior changes when Redis is enabled.

Rejected alternatives: requiring Redis in every deployment, which adds an unnecessary service to ordinary self-hosting; allowing more than one socket-owning process without Redis, which would silently partition queues, routes, and invalidation; weaker in-process ordering, fairness, or backpressure semantics, which would make deployment mode change user-visible behavior and leave the simple profile unable to exercise the distributed contract.

## GPU session density: capacity-critical at 1000 active sessions

Supersedes only the research deferral in "GPU session density: calibrated slots now, worker-internal batching later" above. Its calibration method and scheduler-facing slot abstraction remain in force.

The accepted 500 to 1000 GPU-process design satisfies the recorded spend trigger for worker-internal density work. Cross-session batching and other preview-path density experiments enter the capacity-critical research path. A technique is adopted only when production end-to-end p95 stays within the realtime bar, quality is accepted, and dollars per calibrated slot-hour improve after warmup and headroom. The scheduler continues to consume calibrated slots; batching remains internal to the worker.

The current formula admits one slot when complete-frame p95 is at most 500 ms and two slots when it is at most 250 ms. With a raised configured cap, the calculated thresholds are 166.67 ms for three slots and 125 ms for four. At 1000 active sessions, one slot requires 1000 GPU processes and two slots require 500. Using the repository planning range of $0.35 to $0.70 per process-hour, that projects to $0.005833 to $0.011667 per active session-minute at one slot and $0.002917 to $0.005833 at two, or $175 to $700 per fleet-hour. These are calculations from planning inputs, not measurements under concurrent load or current market prices.

StreamDiffusion (arXiv:2312.12491) reports 91.07 fps image-to-image on an RTX 4090 and 59.56x the throughput of its Diffusers `AutoPipeline` baseline, the same abstraction this worker calls today (`worker/worker/engine.py:518-525`). Its reported components include about 1.5x from Stream Batch, which batches a stream's denoising steps instead of running them sequentially; up to 2.05x from residual classifier-free guidance, which reduces negative conditional denoising to one step or zero; and an input-output queue that absorbs mismatched input and model rates. These are paper results on other hardware, not project measurements, and have not been reproduced here. Even after heavy discounting for a consumer card, 512 px production conditions, and the shared GPU lock, headroom of that order could cross several integer slot boundaries: the recorded formula requires p95 at or below 250 ms for two slots and 125 ms for four, while cost is process-hour price times ceil(active sessions / calibrated slots). At 1000 sessions, four slots mean 250 processes and $87.50 to $175.00 per fleet-hour, eight mean 125 and $43.75 to $87.50, and sixteen mean ceil(1000 / 16) = 63 and $22.05 to $44.10, compared with the existing $175 to $700 one-to-two-slot range. The paper also reports that its stochastic similarity filter reduces energy by 2.39x on an RTX 3060 and 1.99x on an RTX 4090; presenting that as a power optimization independently agrees with this repository's arithmetic that skipping unchanged frames saves energy and duty cycle, but not reserved-slot cost.

Measured on the reference development card (Radeon RX 7600 XT, gfx1102), with a clear GPU and 13.4 GB free after model load, using the worker's own calibration path so the numbers mean what the scheduler means by them: before issue #195, on 2026-08-02, `vega-rt` at full residency returned a single-frame p95 of 285.9 ms and 278.8 ms across two passes, with a mean of 282.4 ms. After issue #195 moved WebP decode and encode out of the GPU critical section, eight passes in one process on 2026-08-04 produced a p95 mean of **274.2 ms**, a standard deviation of **3.3 ms**, and a range of 271.9 to 281.8 ms; per-pass medians stayed near 270 ms. The result still earns **one** slot, not two. Raising the configured cap from 2 to 8 changes nothing, because p95 binds and the cap does not. Two consequences follow. The cost range in this entry should be read at its pessimistic end for hardware of this class, since `ceil(1000 / 1)` is 1000 processes rather than 500. And the next density boundary remains close: the original 285.9 ms figure needed a 13 percent reduction to reach the 250 ms two-slot threshold, the predicted issue #195 result implied about 6 percent would remain, and the measured 274.2 ms mean leaves 24.2 ms, or **8.8 percent** of the serialized region. Reaching four slots from the measured mean would require a 2.19x latency reduction. This is one consumer card and not the fleet card, so the absolute figures do not transfer; the structure does.

The consequence is that a worker-side pipeline change remains the highest-value cost lever, ahead of relay, gateway, and transport work. Issue #195 delivered about 8 ms against a prediction of 19.3 ms. That prediction timed `encode_webp` on a 512 px crop of a photograph with more high-frequency detail than this model produces; a smoother two-step LCM output encodes in roughly half the time. Codec cost must therefore be estimated on representative model output, not stock imagery. The first post-change pair, 291.3 ms and 271.4 ms, appeared to show no improvement, but the eight-pass standard deviation of 3.3 ms shows that a two-sample comparison cannot resolve an effect of this size. Future density claims should quote a distribution rather than a pair, and every density and quality claim still needs measurement on the selected fleet card.

Rejected alternatives: continuing to defer worker-internal density research, because the projected 500 to 1000 process fleet satisfies the decision's spend trigger; scheduler-level frame batching, which reopens slot accounting and couples the scheduler to worker internals; adopting an optimization on isolated throughput or latency alone, which does not establish accepted quality, production p95, or lower dollars per calibrated slot-hour; spot or preemptible capacity for realtime slots. Secondary-source market reports describe nominal discounts of 40 to 70 percent but also 2026 convergence between on-demand and spot prices, interruption rates below 5 percent for H100-class capacity and 15 to 20 percent for A100-class capacity, and notice of 30 seconds to 2 minutes; their consensus is that spot fits batch and asynchronous inference rather than latency-sensitive synchronous serving such as a person watching a canvas at 2 to 4 fps. The resilience decision makes preemption survivable through transparent resume, but the user still pays a resume and queue wait, so spot remains defensible for queued generation capacity and poor for realtime slots.

## Realtime fleet: regional, pool-partitioned, and warm

Supersedes the single-region rejection in "Job placement: offloaded workers first, micro-batching deferred" and the later-stage timing for regional and split pools. The offloaded-worker preference and scheduler-level micro-batching deferral still stand.

At the 1000-active-session target, each active region has its own gateway, FrameBus, admission queue, scheduler lease, and worker pools. Realtime pools keep the realtime model fully resident; batch and other model families use separate pools. Admission assigns one region before one worker and compares browser locality, ready compatible slots, queue wait, measured ready lead time, failure headroom, and configured cost policy. Active sessions are not live-migrated for balancing. Regional failure reacquires capacity elsewhere and uses the browser's complete-canvas resend. Warm capacity is counted in ready calibrated slots, never running machines.

This is the architecture required for the accepted target, not a claim that regional load, ready lead time, or failure recovery has been measured at that scale.

Rejected alternatives: retaining one region and a shared realtime/batch pool until later scale stages, which leaves the accepted target without regional frame planes, model residency, or explicit realtime headroom; one global FrameBus or live migration for routine balancing, which puts cross-region traffic and movement into the per-frame path; counting booting or loading machines as warm capacity, which overstates capacity during the one-to-three-minute readiness interval.

## Realtime authorization: bind once, invalidate explicitly

Deepens "Sessions: opaque server-side tokens" and "Roles: three tiers on the user row" by applying their identity and role decisions to realtime connections.

Authenticate and authorize a browser realtime connection before queueing, reserving quota, or assigning a GPU. Bind user id, account-session id, role, and quota subject from the server-side opaque session. User and admin may create and control realtime sessions; viewer is read-only and cannot consume a slot. Image frames are authorized against the bound connection and session, not by repeating a session-store lookup per frame. Logout, revocation, user disable, deletion, or role change cancels queued work and closes indexed live connections. A gateway validates only short-lived API-minted tickets; the API remains the authority.

At 1000 active sessions and 2 to 4 canvas fps, authenticating each input frame would create a calculated 2000 to 4000 cache or database decisions per second. This rate is derived from the target and cadence, not measured traffic.

Rejected alternatives: accepting a socket before authentication or trusting identity fields in browser messages, which permits scarce work without a server-derived principal; repeating session-store authorization for every frame, which adds 2000 to 4000 decisions per second without improving the immutable connection binding; making gateway tickets carry durable quota or authorization authority, which would duplicate API policy and make revocation depend on ticket expiry.

## Household fairness: only if bounded waiting is required

Deepens "Full pool: admission queue with paid tier priority". Its default that active sessions are never preempted remains in force.

Within each priority class, admission is fair by authenticated principal, with one active-or-waiting realtime request per principal by default. Duplicate tabs do not multiply queue share. This policy prevents queue amplification but does not guarantee bounded wait while an active user draws continuously. An operator who needs bounded household fairness may enable a session-turn lease that releases only at a session boundary while another principal waits; it never time-slices frames.

Rejected alternatives: keying fairness by IP address, cookie, or browser tab, which lets duplicate tabs multiply queue share and conflates users behind one network; enabling bounded session turns by default, which would reverse the recorded no-active-preemption policy for every deployment; frame time slicing or mid-session preemption, which degrades the realtime bar and interrupts active drawing instead of applying fairness at a session boundary.

## Realtime autoencoder: distilled on the way out, never on the way in

> Shipped status (2026-08-19): merged in #316. `vega-rt` and `sdxl-turbo` both name `preview_decoder` `madebyollin/taesdxl` (issue #323). On the reference RX 7600 XT the shipped `vega-rt` path measures 219.8 ms p95 against 336.4 ms p95 with the full VAE. The default's full-VAE path measured 274.3 ms p50 and 276.4 p95; with the same decoder it measured 157.0 and 160.2. An earlier revision of this entry claimed three slots from a 184.9 ms frame; that was wrong arithmetic and is corrected below.

The realtime frame path decodes latents with a distilled autoencoder and encodes the incoming canvas with the model's full VAE. The asymmetry is deliberate and measured.

Profiling a `vega-rt` frame on the reference card, before any of this work, found the autoencoder costing more than twice the diffusion it exists to serve: 72.1 ms to encode the canvas, 71.8 ms for the UNet and scheduler across its steps, and 122.2 ms to decode the result. The intuition that the diffusion model is the expensive part is wrong for a 512 px, low-step realtime frame.

Replacing the decoder with `madebyollin/taesdxl`, 2.45M parameters against roughly 84M and MIT licensed, takes the decode from 123.7 ms to 8.3 ms. End-to-end `frame()` falls from about 274 ms to 184.9 ms, and calibration moves from one slot to two: slots are `floor(500 / p95)`, so a third would need a frame under 166.7 ms. An earlier revision of this entry claimed three, which its own 184.9 ms contradicts. Measured again on the shipped path once the decode was wired into the conditioned realtime branch, `vega-rt` at its four steps runs 216.5 ms p50 and 219.8 p95 against 331.9 and 336.4 with the full VAE, which is the same one-slot-to-two move at the step count the manifest ships. Decoded output is visually near-identical: composition, colour and brushwork hold, with slightly softer fine texture, which is an easy trade for a frame that is replaced two to four times a second.

Replacing the encoder as well is faster still and must not be done. Swapping the whole autoencoder brings a frame to 94.0 ms, which would be five slots, but the image changes subject rather than losing detail: in a fixed-seed comparison the lighthouse the prompt asked for was replaced by a flat-topped rock, at a mean per-channel difference of 22.4/255, which is larger than the difference from adding a whole new clause to the prompt. The cause is structural. The encoder fixes the latent the UNet denoises from, and at strength 0.7 with roughly one effective step there is almost no denoising left to correct an approximate starting point, so the encoder's error survives into the output. A realtime preview is what the user draws against, so a preview that does not show what is being made is worse than a slower one.

A decoder that fails to load or to decode falls back to the full VAE for that frame and is retried after a pause, so no failure stops frames rendering, and the calibrated slot count is cleared because it was measured on the faster path. That clearing takes effect at the next registration, since `realtime_slots` is sent in the worker's hello and heartbeats do not carry it, so a long-lived connection keeps the old number until it reconnects. Closing that window needs a protocol change and is tracked separately.

Stored generations keep the full VAE in both directions. `vega-rt` also serves `text_to_image` and `image_to_image`, and those produce the archival PNG masters of the stored-outputs decision.

Rejected alternatives: a distilled encoder as well, for the reason above, and it is why the manifest field is named `preview_decoder` rather than for the autoencoder as a whole, since naming it for the VAE would invite exactly the substitution this rejects; hand-preparing latents around the pipeline rather than letting it decode, which is how two defects arrived, a dropped batch dimension and a missing inference context, both of which the pipeline had been handling; and treating the published realtime techniques as the starting point, since Stream Batch and residual classifier-free guidance both address the UNet, which this profile shows is the smaller half. With the decode distilled, the remaining frame is roughly 72 ms of encode, 72 ms of UNet and 8 ms of decode, so a fourth slot needs about 27 ms from somewhere other than the encoder.

## Gallery: the derivation forest is the gallery

The gallery's primary view is an infinite pannable canvas that lays generation history out as lineage trees: every generation with a `source_asset_id` hangs off its parent, siblings are alternative takes on the same base, and zoom level selects detail - a time-clustered constellation far out, tidy trees with action-labeled edges in the middle, cards with prompt and parameter deltas against the parent up close. Any node can be branched: opening it as the source of a new generation grows the tree in place, which makes history the working surface rather than an archive. Histories without derivation chains degrade to a time-ordered grid, so prompt-only users never see a broken graph. The in-flow history strip stays; the canvas replaces only the flat gallery grid. Trees contain persisted assets only (realtime frames never join), and a deleted or expired parent leaves a ghost placeholder that preserves the structure of its descendants. Ghosts constrain deletion: the lineage foreign key is SET NULL on a hard delete, which would sever the subtree, so purging an asset removes its bytes and marks the row rather than deleting it (issue #129 lands the mechanics). The provenance columns have recorded the forest since v0.1 ("Generation lineage", issue #57) precisely so this view could ship later; the phased issues are #129 through #132.

Rejected alternatives: a flat grid with a per-image lineage popover (hides the differentiator - the same base re-prompted five ways never becomes visible structure); a force-directed graph (unstable positions destroy the spatial memory a canvas exists to build; tidy trees are deterministic); a canvas or graph rendering library as a new dependency (the interaction set - transform-based pan and zoom, level-of-detail tiles, hover falloff - is already proven in-house by the landing hero field, and d3-hierarchy for layout ships with layerchart).

## Starred canvas filter: roots select complete trees

The canvas starred filter composes `starred=true` with `roots_only=true`. A starred root includes its complete subtree, including unstarred derivatives, because the tree is the canvas unit and those descendants provide the provenance and comparison context that makes the view useful. A starred derivative whose root is not starred does not appear in the filtered canvas. It remains available in favorites and can be reached in the unfiltered forest.

Rejected alternatives: showing only individually starred nodes, which severs edges and turns a forest into disconnected cards; including every tree containing any starred descendant, which cannot be paginated by the existing root query and would require an ancestor-expansion API; treating `starred=true` as a replacement for `roots_only=true`, which returns list pages whose derivative rows cannot be packed as independent trees.

## Fleet token verification: static shared secret first, signed tokens with the cloud

Fixes the order the two halves of `FLEET_TOKEN_KEY` land in. The unauthenticated fleet socket is disclosed in [README.md](../README.md) and [self-hosting.md](self-hosting.md) and mitigated by deployment posture; this entry covers authenticating it.

`FLEET_TOKEN_KEY` verifies worker tokens on `/api/v1/fleet`. It ships as a static shared secret compared in constant time, which is the whole of what a self-hosted deployment needs: one value in the compose file, present on both the API and the worker. Signed short-lived tokens are the cloud shape, and their minting side lives in the private repository's fleet autoscaler, so the open repository would be verifying signatures no producer creates yet. Verification lands with the autoscaler, not before it.

An unset `FLEET_TOKEN_KEY` leaves the socket open rather than refusing to start. The one-command self-host path is a documented promise, and a hard failure on upgrade would break every existing install that never set the variable. The API logs a warning at startup instead, and the trusted-LAN warning in `README.md` stays until the default flips. Flipping it to closed-by-default is a breaking change and belongs to a release boundary, not to the issue that introduces the setting.

Origin validation is a separate control and not a substitute: it keeps browsers off both sockets, which is what makes the trusted-LAN posture true, but it does nothing about a process on the LAN. That is what the token is for.

Rejected alternatives: shipping signature verification alongside the static secret, which writes a code path with no producer and fixes a token format before the service that mints it exists; refusing to start when the key is unset, which is safe-by-default but breaks the documented `docker compose up` story for existing installs on upgrade; treating the Origin check as sufficient and deferring the token entirely, which leaves any process on the LAN able to register as a worker and receive dispatched prompts.

## Permissive fleet mode is confined to peers that cannot route from the internet

Refines the entry above rather than reversing it. That entry keeps the socket open when `FLEET_TOKEN_KEY` is unset, and rests the safety of doing so on deployment posture: the host "must be a trusted LAN". Nothing in the code held the operator to that. `deploy/compose/compose.yml` publishes `8080:8080`, which Docker binds on `0.0.0.0`, and `FLEET_SECRET` has no default, so a self-hosted install on a machine with a public address and no firewall accepted worker registrations from anywhere. A registered worker is dispatched real sessions, so it reads other people's prompts and canvas frames and returns whatever images it likes.

Permissive mode now additionally requires a peer address that is not globally routable: loopback, RFC 1918, carrier and link-local space, and IPv6 ULA. The test is "not globally routable" rather than "private" on purpose. Carrier-grade NAT space, `100.64.0.0/10`, is neither private nor global, and it is what Tailscale hands out, so a private-only test would refuse every worker reached over a mesh VPN, which is a normal way to run one away from the LAN. A compose worker on the bridge network, a worker on an IPv4 LAN address and one reached over a mesh VPN are all unaffected, which is the whole of the one-command promise. A worker holding a global IPv6 address is refused even on the same LAN, because nothing distinguishes it from a remote one; that worker needs the secret. An unparseable address counts as local and is logged, while a peer with no address at all is refused, because uvicorn reports that for a unix socket, which in practice means a proxy sits in front and every public request would otherwise look local. Refusing it was tried and reverted: uvicorn copies `X-Forwarded-For` into the peer address verbatim when it is told to trust the forwarding peer, validating nothing, so a forged header can present an arbitrary string (measured: with trusted hosts of `172.18.0.0/16`, a peer in that range sending `not-an-address` arrives as `("not-an-address", 0)`) - but an attacker in that position would send a parseable `127.0.0.1` instead, which no notation rule can distinguish from a real one. Both branches are equally exposed to that configuration, so the configuration is what gets fixed, and refusing unparseable addresses would only have cost every test client a fabricated address. Setting the secret restores unrestricted reach, so an operator who genuinely runs a remote worker configures the thing that was always meant to authenticate it.

This is not the deferred flip. The default is still permissive and nothing refuses to start; the change only stops permissive mode from applying to a network the documentation already declared out of scope. The one install this does change is one that was running a worker from a public address without a secret, which now has to set one, and such a setup was already outside the trusted-LAN posture the previous entry described. Closed-by-default remains a breaking change for a release boundary.

Two limits are deliberate. Behind a reverse proxy or load balancer the peer is usually the proxy rather than the client, so a fronted deployment gains little here and must set the secret; whether the original address survives depends on the proxy and on what uvicorn is told to trust, which is not something this check can rely on. And the check is about reachability, not identity: any process already on the LAN still registers, which is exactly what the entry above says the token is for.

One interaction is worth stating exactly, because it decides whether the check holds. This code never reads `X-Forwarded-For`, but uvicorn does: `proxy_headers` defaults to on, and it overwrites the peer address from that header for any client covered by `forwarded_allow_ips`, which defaults to `127.0.0.1`. Measured against uvicorn 0.50.1: with the default, a peer at `8.8.8.8` sending `X-Forwarded-For: 127.0.0.1` still arrives as `8.8.8.8`, so the check holds; with `FORWARDED_ALLOW_IPS=*`, the same peer arrives as `127.0.0.1` and permissive mode is open to the internet again. That pairing is silently unsafe, and setting it is ordinary advice for a fronted deployment, so the API warns at startup when the wildcard is set and the key is unset. It stays a warning rather than a refusal for the same reason the default itself stays permissive.

IPv4-mapped IPv6 is the other form a public peer plausibly arrives in, because a dual-stack listener reports IPv4 clients as `::ffff:A.B.C.D`. CPython 3.11 classifies those by the address they map, so `::ffff:8.8.8.8` is global and `::ffff:192.168.1.5` is not; a test pins both directions, since older CPython did not always agree.

The honest limit of this control, measured on Docker 29.6.1 with a published `8099:8099` and an IPv4-only bridge. A client reaching the port on the host's routable IPv4 address arrives with that address intact, because published IPv4 ports are forwarded by iptables DNAT: the container saw `192.168.1.143`. A client arriving over IPv6, or over the host's own loopback, is carried by the userland proxy instead, which terminates the connection and opens a new one from the bridge gateway: the container saw `172.17.0.1` in both cases. So on a host with a public IPv6 address and an IPv4-only bridge, an internet client is indistinguishable from a worker on the compose network and permissive mode admits it. This control therefore closes the direct IPv4 path and the trusted-LAN premise, and does not close IPv6 on the shipped compose topology. It is a mitigation, not a boundary; `FLEET_SECRET` is the boundary, and any host with a public address of either family needs it. Closing IPv6 as well means either the closed-by-default flip or an IPv6-enabled bridge, and the flip is the one that does not depend on deployment topology. This measurement is what moved that flip from "eventually, at a release boundary" to filed work: issue #245.

Rejected alternatives: flipping to closed-by-default now, which is the breaking change the entry above assigns to a release boundary; binding the published port to loopback in compose, which also removes the studio from every other machine on the LAN and so breaks a legitimate self-hosted setup to fix the fleet socket; gating on a new development-only flag, which is closed-by-default wearing a different name and still makes every existing install edit its environment on upgrade; trusting `X-Forwarded-For` so proxied deployments could be distinguished, which lets the peer assert its own trustworthiness.

## Self-hosted installs are multi-user

Sharpens "Authentication: built-in module", "OAuth at launch: Google and GitHub" and "Roles: three tiers on the user row" into one statement about who uses a self-hosted install, because those three entries each describe a piece and none says the shape.

A self-hosted install is not assumed to be one person. The operator runs it, holds `admin`, and configures the install; the people they invite hold `user` and generate, star and manage their own work; `viewer` stays read-only for someone who should see the gallery without spending the GPU. That is the existing three-tier model unchanged, applied to a household or a small team rather than to the cloud alone.

Those people sign in with a local email and password, or with Google or GitHub. Both halves ship for self-hosters, not only for the cloud: local accounts serve an install with no external dependency, and the providers serve people who would rather not hold another password. `AUTH_MODE=none` remains the default and the zero-configuration path for a single operator who wants no accounts at all.

Generic OIDC against an operator's own identity provider is rejected. Authentik, Keycloak, Authelia and Entra would each be reachable through one OIDC client implementation, and it is the obvious request from a self-hoster who already runs an identity provider, but it is a larger surface than the two providers plus local accounts, it needs discovery, key rotation and claim mapping to be correct rather than merely working, and nobody has asked for it. It layers on the same seam later if they do.

Rejected alternatives: treating self-hosted as single-user and putting accounts in the cloud only, which is what the current code implies and which makes a shared install impossible without sharing one identity; a fourth tier between `user` and `viewer` for people who may generate but not manage their own history, which nobody asked for and which the three tiers already approximate; generic OIDC now, for the reasons above.

## First run generates its own secrets, and that is what closes the fleet socket

Resolves the tension between "Fleet token verification: static shared secret first, signed tokens with the cloud", which keeps the fleet socket open when `FLEET_TOKEN_KEY` is unset, and "Permissive fleet mode is confined to peers that cannot route from the internet", which measured how far that concession actually reaches.

Both entries defend permissive mode with the same argument: closing it would break `docker compose up` for an operator who never set a secret. That argument holds only because nothing in the install ever sets one. `deploy/compose/.env.example` ships `POSTGRES_PASSWORD=change-me` and an empty `FLEET_SECRET`, and the quick start says to copy the file and edit it, so an operator who does not edit gets a guessable database password and an unauthenticated fleet socket. The weak default is not a considered trade; it is the absence of a step.

First run therefore generates its secrets. The preflight script writes `deploy/compose/.env` when none exists, with `openssl rand -hex 32` for `FLEET_SECRET` and the database password, fills those keys when they are empty, refuses to overwrite a non-empty value, and prints what it wrote. Hex because Compose expands `$NAME` inside an unquoted value and an HTTP header carries ASCII only, both of which a generated secret has to survive.

With that step, a new install always has a fleet secret, so permissive mode applies only to an install that predates this. Closed-by-default follows immediately for new installs rather than waiting for a release boundary: the socket refuses an unset key, and the only deployments affected are existing ones, which is the scope the breaking-change worry was ever really about. An operator upgrading gets a startup error naming the variable and the command that generates it, not a silent downgrade.

The same step is where accounts begin when issues #5 and #9 land. A self-hosted install is multi-user by design, and asking a self-hoster to hand-craft the first admin credential is the same mistake as asking them to invent a fleet secret: first run mints it and prints it once.

Rejected alternatives: keeping permissive-when-unset and relying on the address restriction, which the measurement in the entry above shows does not close IPv6 on the shipped compose topology and was explicitly called a mitigation rather than a boundary; generating the secret inside the API at startup and persisting it, which hides a credential the operator needs in order to configure a worker on another machine and makes the compose file and the running process disagree; shipping a fixed default secret, which is a published credential; and prompting interactively, which breaks the unattended install that scripted deployments depend on.

This is the decision. Preflight writes the secrets on first run. An unset `FLEET_TOKEN_KEY` refuses the handshake and refuses to start. An existing install that still has an empty `FLEET_SECRET` must fill that file before the API will start.

## Realtime canvas conditioning: a sketch T2I-Adapter on VegaRT, not img2img

The realtime canvas is structural conditioning for a fresh text-to-image latent, not the starting image of an img2img pass. The worker keeps the accepted VegaRT base, its LCM scheduler and the few-step frame path, and applies the Apache-2.0 `TencentARC/t2i-adapter-sketch-sdxl-1.0` to each complete browser WebP. The manifest field naming the adapter is worker-only and never reaches the browser; the adapter composes onto the existing pipeline with `from_pipe` so the UNet, both text encoders and the VAE are the same objects, and the load fails rather than proceeding if that sharing stops, because a duplicated UNet would exhaust the card. A new `structure_strength` parameter maps to the adapter conditioning scale. The wire is unchanged: the same 17 byte header and complete WebP.

Img2img was not merely worse, it has no useful setting. Measured on the reference RX 7600 XT with a line drawing and a scene prompt, `strength` 0.70, 0.85 and 0.95 all returned the drawing essentially unchanged, and 1.00 returned a scene with the drawing's structure gone. A white page carrying thin strokes has almost no tonal information, so its latent is dominated by flat white and the denoiser rebuilds flat white until the latent is noised completely, by which point nothing remains to steer with.

The adapter beat a full SDXL ControlNet on measurement, not on principle. Complete-frame p95 over sixty frames: adapter 315.6 ms at two steps and 407.9 ms at four, peak 3.77 GiB, cold load 27 s; `xinsir/controlnet-scribble-sdxl-1.0` 409.4 ms at two, 509.7 at three and 555 to 568 at four, peak 6.01 GiB, cold load 180 s. The mechanism explains the shape: the adapter computes its conditioning features once before the denoising loop, while a ControlNet runs its network at every step, so its cost grows with the step count and it clears the realtime bar only at two steps. The ControlNet was the more literal on sparse strokes and the adapter the more coherent, which did not outweigh 90 ms, 2.2 GiB and six times the load.

Defaults are four steps and conditioning scale 0.7. Four steps at 407.9 ms p95 stays inside the realtime bar and interprets a drawing far better than two. Scale 0.7 renders a drawn sun as a sun and a drawn ridge as mountains, where 1.0 traces the strokes literally and 0.3 ignores them. Realtime calibration exercises this path with a sparse sketch map rather than the previous flat gray at img2img strength 0.7: a uniform map gives the adapter nothing to condition on, so calibrating on it would size `realtime_slots` against a workload no session runs.

A session carries one seed, generated at open when the client supplies none and honoured when it does. Without it every frame sampled a fresh latent, so an unchanged canvas re-rolled the image: 85.9 percent of pixels changed with nothing drawn, and 94.2 percent when one small stroke was added. With it an unchanged canvas is bit-identical and one small stroke changes 10.4 percent. This is worth stating precisely because it looks like a transport problem and is not: sending only changed regions or vector deltas would not have helped, since the model re-denoises the whole latent whatever arrives on the wire. The residual is the changed conditioning map re-rendered globally, is deterministic, and would need masked re-denoise or compositing to reduce, which is a separate mechanism.

What this does not do is interpret a drawing semantically. A stick figure is traced as strokes at scale 1.0, becomes an incidental shape at 0.7 and disappears at 0.3, because an edge conditioner encodes no limbs or joints. A stick figure is instead the canonical input of a pose conditioner, and adding one is separate work with its own latency budget.

The threshold reads the darkest of a pixel's three channels rather than its luminance. The two agree on the black strokes on white the canvas started with, so this changes no measurement above. They stop agreeing once the canvas has a palette: green weighs 150 and yellow 226 against the 128 threshold, so those strokes reached the adapter as blank paper while red at 76 and blue at 29 drew, and which colours a person could draw with was decided by a constant nobody chose for that. The darkest channel is 0 for all four and is the grey itself for a grey pixel. What colour a stroke is still carries no meaning to a sketch adapter, and evaluating colour as a second conditioning channel is issue #266; this decides only whether the adapter sees a stroke at all. A pale tint is still paper, which is the constraint the studio palette is picked against.

Rejected alternatives: keeping img2img with a tuned strength, which the sweep above shows has no setting that both paints a scene and follows the drawing; a full SDXL ControlNet as the default, whose per-step control network misses the realtime bar above two steps for fidelity the sparse-stroke comparison did not justify; ControlNet lineart, which expects dense detector-style contours rather than a canvas that begins with two or three strokes; moving the realtime base to an SD1.5 LCM such as `dreamshaper-lcm`, whose measured four-step latency already misses the bar before any conditioning is added and which would give up the accepted Vega base; a learned preprocessor such as HED or PidiNet, unnecessary because the canvas is already clean line art and a deterministic invert and threshold suffices; reusing `strength` as the conditioning scale, which would give one parameter two unrelated meanings in a manifest that still advertises `image_to_image`; and region-based or vector transport as the fix for frame-to-frame instability, which addresses bandwidth rather than the cause.

## Realtime picker: sdxl-turbo returns, for the realtime capability only

Supersedes "Stability Community License models in the product" for the `realtime` capability of `sdxl-turbo`, and only that. The earlier entry held both turbo manifests at `benchmark_only` because prompt-only quality at the shipped 512 resolution was not good enough for the studio picker, and named a measured higher default resolution as the reopening condition. That condition is not what changed. The conditioned realtime path asks a different question of the base: the drawing supplies composition and the prompt supplies subject, so what is left to the base is rendering the scene it is handed. sdxl-turbo does that well at 512 where its unconditioned text-to-image at the same resolution did not convince, which is why the earlier rejection does not transfer to this path.

Measured on the reference card with both models resident, one drawing and one prompt at a fixed seed, thirty consecutive frames each, timed at the browser end of the relay: `vega-rt` 388 ms p95 at its default four steps and 280 ms at two; `sdxl-turbo` 305 ms at its default one step and 415 ms at two. Peak memory is 3.77 GiB against 7.66 GiB, from isolated loads. So sdxl-turbo is the faster of the two at its default, and a step costs it about 111 ms, which puts three near 526 ms and four near 637 ms. Its schema still permits four, because a parameter schema in this project does not encode the realtime bar and `vega-rt` already permits eight for the same reason: the bar is enforced by slot calibration, which measures the model at its defaults and advertises the capacity that measurement supports. Capping the schema instead would have broken the four-step cells of the shipped benchmark matrices, which is the anchor role this model keeps. These numbers replace an earlier pass that reported 459 ms and 671 ms for sdxl-turbo. That pass shared the card with other work, the failure mode already recorded for the four-step measurement above; these were taken on an otherwise idle card and the samples sit inside a 9 ms band. Over a loopback API the relay costs nothing measurable: the same thirty frames timed at the browser and observed at the worker came to 379.0 ms and 379 ms. So the picker's number and the number `slots_from_frame_ms` divides are the same quantity, and what separates a calibration figure from a session's is the conditioning input rather than the transport, since calibration renders a synthetic sparse sketch map where a session renders a drawing.

The picker shows each model's realtime p95 as the connected worker measured it at calibration, not the queued text-to-image estimate, because the two paths differ by both the adapter and the step count. Only one model is calibrated at warmup, so a second realtime model shows no number until something measures it; carrying a per-model frame p95 on the heartbeat is the missing piece [metrics.md](metrics.md) already promises.

sdxl-turbo is the realtime default, decided by the operator after seeing both. On the conditioned path it is both quicker per frame and the only one of the two that reads a drawn shape as a thing rather than as an outline: given a ridge, a horizon and an ellipse, it renders a lake with water and reflections, while `vega-rt` renders a flat white disc lying on a hillside. That is the base model and not the conditioning scale, established by rendering both models at 0.7 and at 1.0 from the same seed: the disc appears at both scales on `vega-rt` and at neither on sdxl-turbo. It is also the mechanism behind a complaint that a drawn circle did not become a lake, which was read at the time as a limit of edge conditioning in general.

Defaulting to it accepts what `vega-rt` was chosen for. The Stability AI Community License carries a $1M annual revenue cap and an attribution obligation, so a self-hoster now meets both without asking for them, and `min_vram_gb` of 10 against `vega-rt`'s 8 means a card that holds `vega-rt` may not hold the default. Neither is silent: the picker renders the attribution whenever the selected model demands it, `available()` withholds `realtime` from a model that cannot be full-resident, so a card too small never sees this one offered, and `vega-rt` remains one selection away. The alternative, a default that differs per deployment according to what the worker can hold and what the operator has agreed to, was rejected for now as a default nobody can predict from the repository.

Which model is the default is a declared choice rather than an ordering accident. The realtime picker preselected `realtimeModels[0]`, so the default was whichever id sorted first from `/api/v1/models` and would have moved silently when a model with an earlier id shipped. It now honours the manifest's `default` flag through the same `fallbackModelId` every other picker uses, so swapping the default is that one field, and the flag is safe to set on a narrowed model because the helper only ever runs against an already capability-filtered list.

Visibility is per capability, not per model. A manifest may narrow what the studio offers with `studio_capabilities`, which `registry.public()` intersects into the advertised `capabilities`, so `sdxl-turbo` reaches the realtime picker while staying out of the queued generate picker whose quality case remains unmeasured. `registry.available()` is untouched, so the benchmark page keeps it as a speed anchor exactly as the earlier entry intended. Selecting it obliges the "Powered by Stability AI" attribution its manifest already carries, so the realtime panel renders that string whenever the chosen model declares one. The $1M annual revenue cap and the registration requirement are unchanged.

Narrowing is a product boundary, so the API enforces it rather than only the pickers. A prompt-only `POST /api/v1/generations` was accepted for any model whatever its capabilities, and `create_generation` now requires `text_to_image` for a request that carries no source asset. The refusal says the model is not offered for that path rather than that it does not support it, because a narrowed model does support it and an operator sent looking for a model limitation would find none. One consequence is deliberate: `scripts/generate.py` against a narrowed model returns 422, and `BENCHMARK_API=1` is the path that still reaches it, since `for_jobs()` returns `available()` in that mode. The same POST also used to persist the narrowed copy it had just been handed, overwriting the capability list `hello` wrote into the models table, which is the row usage events and job history classify from; it persists the unnarrowed manifest now.

Rejected alternatives: clearing `benchmark_only` on its own, the one field that makes a model public today, which would have reopened the rejected prompt-only-at-512 case in the queued picker with no measurement behind it; deleting `text_to_image` and `image_to_image` from its capabilities to reach the same narrowing, which would strip the benchmark anchor the earlier entry deliberately kept; leaving it benchmark-only and pointing the realtime picker at `available()`, which would expose every benchmark manifest including `sd-turbo`, not SDXL-class and unable to take this adapter; keeping `vega-rt` as the realtime default on its license and its 8 GiB floor, which is what this entry argued before the operator chose otherwise, and which loses the better result to a caution the picker and the memory ladder already handle; and capping its steps schema at two so the studio could never offer an over-bar setting, which reads as prudence and is not: it contradicts `vega-rt` permitting eight, it puts the bar in the wrong layer, and it silently broke the four-step cells of three shipped benchmark matrices, whose 422 nobody would have seen until a benchmark run.

## Realtime concurrency comes from one GPU serving several sessions, by decode first and batching second

> Shipped status (2026-08-22): **shipped.** The worker collects pending frames for 40 ms and denoises one compatibility class (model, steps, resolution) per GPU cycle, with round-robin across classes (issue #294). Admission uses a measured per-class batch curve when present; counts above the curve are refused. The picker still shows the single-frame p95 from issue #288. On the reference card, the fourth turbo session is outside the bar once the window and WebP are included.

Supersedes only the start trigger in "GPU session density: calibrated slots now, worker-internal batching later" and in "GPU session density: capacity-critical at 1000 active sessions". Their method, their slot abstraction and their adoption bar all stand: the scheduler keeps consuming calibrated slots, batching stays internal to the worker, and a technique is adopted only when end-to-end p95 stays inside the realtime bar with quality accepted.

What changes is when the work starts. Both entries gate it on fleet spend, at the 500 to 1000 GPU-process scale where density is the cheapest capacity. The requirement is now a product one at a single GPU: two people drawing at once on a self-hosted box, and more than two per GPU in the cloud. Serialising on the frame the realtime path shipped with cannot deliver that, and the reason turned out to be the decoder rather than the denoiser. Sessions share one GPU lock, so two sessions each see twice a single frame: on the reference card `sdxl-turbo` measures 278 ms for one frame, and two serialised sessions are 556 ms per cycle each, which misses the 500 ms bar and the 2 fps floor it encodes. The recorded formula agrees, admitting a second slot only at 250 ms or below, which neither shipped realtime model reaches.

So capacity above one session per GPU is a decode problem before it is a batching problem, which is the opposite of where this entry started. Measured at 512 px with fused attention, over 120 timed frames per point, `vega-rt` at its two-step floor costs 264 ms of complete frame for one session and 465 ms for two batched, against 528 ms for the same two serialised. Batching therefore does buy the second session, by 11.9 percent of what the two would cost serialised, and it leaves 35 ms of margin against the bar.

That margin is not enough to promise on, because the collection window a batching scheduler needs is 30 to 50 ms by the design above and is not in the 465 ms. Nor are the input decode and the real output encode, which the measurement substitutes with synthetic drawing and a faster encoder setting. So batching alone puts two sessions at the edge of the bar rather than inside it.

The VAE is where the room is. Issue #214 measures `vega-rt`'s full VAE decode at 122.2 ms of a 265.8 ms frame and TAESDXL at 8.3 ms, and decode is per image, so a batch of two pays it twice. Substituting the tiny decoder for the live preview is therefore worth more than batching is, and the table below is what both together measure. Two people drawing at once needs the tiny decoder and does not need batching at all; batching is what buys the sessions after that, and its saving is worth having only once the decode is no longer half the frame.

Measured together, both changes give these curves at 512 px with the tiny decoder, over 40 timed frames per point, reporting the complete frame every session in the batch waits for rather than a per-image cost. Each model is at the step count its manifest ships, because that is what a user gets, and `vega-rt` is also shown at its two-step floor to price what turning steps down buys:

| Sessions batched | sdxl-turbo, 1 step | vega-rt, 4 steps | vega-rt, 2 steps |
|---|---|---|---|
| 1 | 167.5 ms | 232.6 ms | 156.5 ms |
| 2 | 260.5 ms | 356.8 ms | 223.9 ms |
| 3 | 367.4 ms | 507.9 ms | 310.1 ms |
| 4 | 473.5 ms | 645.2 ms | 396.2 ms |
| 5 | 603.3 ms | 824.1 ms | 494.3 ms |
| 6 | 707.3 ms | | 576.1 ms |

So the number is not a property of the card. The studio's default model holds four sessions inside the bar and three with real margin; `vega-rt` at the four steps its manifest asks for holds two, and the same model at two steps holds five. That is the compatibility class from further down this entry arriving as a measurement rather than an argument: step count is part of the class, so capacity belongs to the class and a single per-worker number cannot describe it. Three sessions is what this card supports on the shipped default with margin to spare, and any promise has to name the class it was measured for.

Reserved VRAM grows about 0.1 GiB per extra session, reaching 7.56 GiB for `sdxl-turbo` at four sessions and 4.03 GiB for `vega-rt` at five, so on this 15.98 GiB card the GPU cycle binds and memory does not. An earlier revision of this table reported only `vega-rt` at two steps and read five sessions off it as the ceiling, which overstated what anyone running defaults would see.

A third saving is larger than batching and is the only one that changes nothing about the image. A realtime session re-encodes the same prompt on every frame, though the prompt changes only when the user types while the canvas changes constantly. Encoding it once per session and reusing the tensors removes 26 to 33 ms of every frame, 15.7 percent of `sdxl-turbo` at one step and 12.3 percent of `vega-rt` at four, and the output is bit identical: the maximum difference between a cached-embedding latent and a re-encoded one at the same seed is exactly zero, because they are the same tensors. It is the cheapest capacity in this entry and it was found only by measuring what a frame is made of.

With the tiny decoder and cached embeddings, which are free in different senses: the cache is bit identical, so nothing about the image changes, while the decoder is a preview that was measured and accepted rather than one that costs nothing:

| Sessions batched | sdxl-turbo, 1 step | vega-rt, 4 steps |
|---|---|---|
| 1 | 143.9 ms | 216.9 ms |
| 2 | 241.3 ms | 338.8 ms |
| 3 | 360.7 ms | 490.3 ms |
| 4 | 446.7 ms | 644.0 ms |
| 5 | 565.3 ms | 793.0 ms |

The order of work follows from this rather than from the ceiling, and one scope note has to come first: every `sdxl-turbo` figure above was measured with a distilled decoder. The studio default now names `preview_decoder` `madebyollin/taesdxl` (issue #323). Measured through the path without that field, each case in its own process, the default rendered at 274.3 ms p50 and 276.4 p95 and advertised one slot; adding the decoder measured 157.0 and 160.2, which is three. Calibration on the card is what advertises the live count.

Read with that scope, four sessions of the studio default fit with 53 ms of margin once it has a decoder, and `vega-rt` at the steps it ships gains a third session it did not have before, at 490.3 ms, which is inside the bar and too close to promise. Serialised, the same two savings give 288 ms for two users of the default and 432 ms for three, both inside the bar, so the product requirement is met with no scheduler at all. Batching is still faster at every count, saving about sixteen percent of what the same sessions cost serialised, 241.3 ms against 287.8 for two and 360.7 against 431.7 for three, and it is no longer what makes concurrency possible. Batching then buys the fourth session and cheaper cloud GPUs; it is not a prerequisite for concurrency, which is what this entry assumed before any of this was measured. Both the preview decoder and the session-owned `PromptCache` have shipped; on the reference RX 7600 XT the shipped `vega-rt` path measures 219.8 ms p95, which matches the 216.9 ms single-session point in the table above. The studio default now names the same `preview_decoder`; live advertisement is calibration on the card. What remains before anyone promises two concurrent users is the end-to-end confirmation that has never been run, because until the decoder landed a worker advertised exactly one slot and a second session could not be opened. Batching comes after that.

Peak allocated VRAM was 4.15 GiB for `vega-rt` at batch two and 7.51 GiB for `sdxl-turbo`, with reserved at 4.71 GiB, so allocated memory did not bind anywhere in the measured matrix. That is a narrower claim than memory not constraining anything: other resident models, compiled graphs, desktop use and larger cloud batches were outside it.

Two earlier revisions of this entry were wrong in ways worth recording, because both were published. The first said a batch of N costs far less than N single frames, which overstated a saving that is 11.9 percent for two sessions on the full decode, and about sixteen percent once the decode and the embeddings stop dominating. The second concluded that concurrency is a resolution trade, one session at 512 px and two at 448, from a sweep that ran without `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`: RDNA 3 gates its fused attention kernels behind that variable, `DiffusersEngine` sets it for `DEVICE=rocm`, a script building a pipeline directly must set it too, and [gpu-performance.md](gpu-performance.md) already said so. With it set, two sessions fit at full resolution and the trade was imaginary. A third correction is smaller and in the same direction: the first sweeps took twelve samples and reported the second highest as a p95, which is nearer the 85th percentile; at 120 samples the distribution turns out to be tight enough that the numbers held, which was luck rather than method.

Two sessions may share a batch only when their frames need the same graph. Model, step count and resolution must match; prompts, seeds and Structure may differ freely. Structure looked like a fourth constraint because a Python list of scales raises, but the pipeline multiplies its batched adapter state by that value, so a `[B,1,1,1]` device tensor scales each sample: measured, each batch member matches a solo render at its own scale to under one level of mean absolute difference while the two members differ from each other by about 48. Guidance is not a constraint either, because the realtime path hardcodes it to zero and the studio never sends it; prompts and seeds may differ freely, because those are batched tensors rather than graph shape. That defines a compatibility class, and capacity is a property of a class rather than of a worker: `slots` becomes the largest N whose measured batch time for that class stays inside the bar. This is also what makes admission honest, which four attempts at a single per-worker number failed to do (issue #285): a number derived from one measurement cannot bound a session that chooses its own parameters, but a number derived per class can, because the class fixes the parameters that matter.

Capacity is therefore measured as a curve rather than a point, and `scripts/prototype-batch-sweep.py` is what measures it: it reads a manifest, batches real sketch maps through the shipped adapter, and reports the pipeline call and the whole frame separately, so any card can be characterised rather than only this one. Its `--tiny-vae` flag is the decode path the table above was measured with, so the curve can be reproduced rather than only cited. A deployment then picks the resolution its card serves for the concurrency it wants, and the cloud reads the same curve on its own hardware.

Rejected alternatives: a second worker process on the same GPU, which is the obvious way to get two sessions and does not work here twice over, because two processes each holding `sdxl-turbo` need 15.32 GiB of weights on a 15.98 GiB card that already carries about 1 GiB of desktop, and because both processes would still serialise on the one GPU, so the cost is doubled weights for no throughput; multiple CUDA streams inside one process, which does not parallelise a single UNet meaningfully and contends in the allocator; keeping serialised slots and lowering the promise to 1.8 fps for two users, which is honest and gives up the requirement; and admitting two sessions on the current serialised path without batching, which would advertise a bar the second session breaks, the defect issue #285 records.

Self-hosted and cloud share the mechanism and differ only in how many workers exist. A self-hosted box gets whatever the batch curve supports on its card, one session until the sweep says otherwise, and the cloud multiplies that by workers rather than by processes per GPU.

Every number here stops at the worker, and the bar is end to end, so none of them is a capacity promise yet. The curve was measured with perfectly aligned inputs, no collection window, synthetic strokes and a fast encoder setting, which is the friendliest case a scheduler will ever see; real users draw at different moments, so realised batches will be smaller and less full than the sweep's. What has to be measured before admitting a second user is the whole path with stage timings, a sustained multi-user run rather than a burst, and a slow browser next to healthy ones, which has since been measured: a session stalled for 25 seconds does not slow its neighbours, but it does resume to a 25-second-stale backlog. Issue #294 carries that program. The quality acceptance the arithmetic depends on has been done: decoding one denoised latent both ways, so nothing but the decoder differs, gives a mean absolute difference of 10 of 255 across four subjects at 22 to 28 dB PSNR, and the tiny decoder retains 108 percent of the full decode's local gradient, so it is not the softening that was expected of a distilled decoder. Side by side the two are hard to tell apart. That clears it for a live preview, which is what it is for; the final image a user keeps is a queued job through the full VAE, so this decision never trades the output away.

## Realtime admission is per-model cost, not one worker slot count

Closes the honesty gap in issues #285 and #234. A worker still serializes
realtime sessions on one GPU lock. Independent per-model slot counters would
admit three sdxl-turbo sessions and two vega-rt sessions at once and miss the
500 ms bar. Capacity is therefore a budget of serialized frame cost, not a
pair of counters.

Each realtime model has a measured p95 at its declared defaults. A session's
cost is that p95. An unmeasured realtime model cannot be admitted. Admit a
new session when the sum of live session costs on that worker plus the new
cost is at most 500 ms. slots_from_frame_ms remains the human-readable form
of the same arithmetic: floor(500 / p95) for a homogeneous batch of that
model.

Wire: optional top-level `realtime_p95_ms` on hello, a map of model id to p95.
No protocol bump was spent on that map. Protocol 4 is `control_generation`
and `session_refused` (the session-lifecycle entry below).
Keep scalar realtime_slots on hello. A current worker sets it to the minimum
of floor(500 / p95) across measured realtime models, so an older API that
only reads the scalar stays pessimistic and honest. Heartbeat already carries
frame_p95_ms; the API stores the map and admits from it when present. An N-1
worker that omits the map keeps today's shared integer pool.

Heartbeat may raise a model's p95 (decoder fallback, slower observations),
which lowers new admissions. It must not lower a model's p95 in a way that
raises capacity on that live connection.

Warmup calibrates every non-benchmark_only realtime model that still advertises
realtime after measured_manifests, not only the default. Boot pays one extra
cold load.

Calibration times one _frame under the GPU lock. A compatible adapter
batch times concurrent frame() calls for n=2..cap and records the complete
cycle p95. Admission uses the measured batch curve when present and keeps
the serialized p95 as fallback. The picker still reads the single-frame p95
(issue #288), while admission reads the curve. A heartbeat raise drops that
model's curve. After twenty observations, a higher
p95 may lower admission. A lower p95 must not raise it on that
connection.

Ending a live session that no longer fits ships in protocol 4: after a
heartbeat raises a model's admission p95, if live cost on that worker
exceeds 500 ms the API closes the newest protocol 4 sessions until the
sum fits and reassigns them (issue #270 / remainder of #317).

Rejected alternatives: independent per-model counters, which over-admit mixed
load; calibrating only the default and applying that count to every model,
which is the defect this entry closes; admitting only the calibrated model,
which is honest and wastes the second realtime model; bumping the protocol
for an optional map, which spends protocol 4 on a field an older API can
ignore.

## The realtime session has states, a fencing generation, and one durable accounting owner

> Shipped status (2026-08-19): **partially implemented.** Protocol 4, named states (`assigning` / `live` / `ending` / `ended`; `queued` and `idle` remain in the enum), `control_generation` fencing, and `session_refused` as an attempt failure all ship. Checkpoints, durable outbox, unique settlement keys, and per-session mailboxes do not. Accounting is still `session_closed` after browser teardown via `closing_sessions`.

A realtime session is currently a dataclass with a worker, an event and a membership test, and its transitions are decided by whichever coroutine notices first. Four can: the browser's handler, the fleet handler, `reassign`, and the worker. Three attempts to add one feature on that footing each produced a defect, all found by review rather than by tests, so the design comes before the feature this time. The feature is ending a live session whose model stops being fully resident, which today renders nothing until the browser leaves (issue #270).

What each attempt broke is the specification. A worker-ended session stranded its runner, so later frames reached a finished task and the accounting never arrived against an API that did not know the new message. Arming that accounting in a second place made it worse: `session_closed` is accepted only once `closing_sessions` holds an entry, so two arming sites produced two usage events for one ordering and none for another, where fleet cleanup deleted the armed entry before the browser teardown could re-arm it. Serialising assignment with a per-session lock let a queued `reassign` resurrect a session whose open had already failed, sending `interrupted` and `resumed` where no `ready` was ever sent, because readiness carries no identity and any waiter accepts any answer. And retrying other candidates gave a stale attempt somewhere to go, so two workers held one session and one slot was never freed.

Four things are therefore named, and nothing new is built on the old footing.

**A session is in exactly one state, and one place moves it.** Queued, assigning, live, idle, ending, ended, with `ended` absorbing. Queued and idle are not new inventions here: an admission queue for a request that finds no free slot, and the release of a slot after about 60 seconds without input, are already designed above and simply have not shipped. A state set that omitted them would have to be widened by whoever implements them, which is how a design becomes a thing implementers work around. Every writer calls one place, and that place compares the expected state and transitions atomically, because routing four writers through one function does not by itself stop two of them from both running the side effects.

Assignment failure is scoped to the attempt, not to the session. A worker that evicts a model or runs out of memory while another worker could serve the request has failed an attempt, so `assigning` goes back to `assigning` under a new generation, or to `queued` when no candidate is free, and a live session whose worker is lost returns to either. Only the browser leaving, losing authorization, asking for something no worker can ever serve, or cancelling ends the session. This is the correction the first version of this entry needed most: it made every refusal and timeout terminal while the prose promised another candidate would be tried.

**Assignment carries a fencing generation, not an opaque identity.** Every session has a monotonically increasing `control_generation`, which the Redis layout in the blueprint already names, and it travels on `open_session`, `update_session`, `close_session`, `session_ready`, `session_refused`, `session_checkpoint` and `session_closed`, all of which means protocol 4: a protocol 3 worker carries no generation on any of them, keeps the unfenced `update_session` that ships, and is confined to a session's first attempt so that it never has two to tell apart. Not on frames: the 17-byte binary header has no field for it and issue #19 owns adding one, so instead a fenced open cancels the runner it replaces. That stops a superseded attempt starting new work rather than stopping it delivering, because a runner cancelled in its send may already have the bytes on the transport, so the honest bound is one stale frame in front of the user and never a second. A preview can afford that; the exact form waits for #19. The worker accepts an open only when its generation exceeds the highest it has seen for that session, treats an equal one as idempotent and a lower one as stale, requires equality with the active runner for updates and closes, and keeps a highest-generation tombstone after close so a delayed open cannot resurrect a finished session. Lifecycle authority belongs only to the current generation on the current worker incarnation, so nothing else can move a session, start one or end one. Accounting is deliberately wider: a report from a retired generation still records that attempt's segment, because a session reassigned twice ran on three workers and discarding two of those segments would undercount what it used.

A counter rather than a UUID, because the ordering is the point. An opaque identity can tell two attempts apart but cannot say which is newer, and the failure that needs ordering is a delayed message from an earlier attempt to the same still-connected worker: `close_session` carries only a session id, so attempt one's late close pops the runner attempt two just installed, and an `open_session` for a session that already has a runner overwrites it without cancelling the old one, which can then emit a frame the API accepts. Worker object identity cannot separate those, because both attempts hold the same object. The queued-job path already learned exactly this and already carries a per-dispatch token for it, with the reasoning written at the check; the realtime path was about to repeat the mistake with a weaker mechanism.

**Accounting has one owner, and the owner is durable.** One place deciding is necessary and is not sufficient: it settles competing writers on the normal path and does nothing about crashes. Today the arming map is process-local, persistence is a fire-and-forget task whose database failure is logged and dropped, and `usage_events` has no unique key, so a crash between decision and commit loses the event and adding a retry would duplicate it. So the API is the durable terminal owner: terminal state and an outbox record commit in one transaction, every settlement carries a stable unique key which is the source key the ledger already deduplicates on, so a redelivery after an unrecorded acknowledgement costs nothing, the session settles only once every attempt it created is reported or declared lost rather than at the first terminal transition, with a late arrival correcting the total through a supplementary event of its own, keyed by that settlement key and the generation it belongs to so it cannot collide with the aggregate it corrects, instead of being dropped, sessions left `ending` are reconciled at restart, and per-attempt segments aggregate into one event for the session. The rollup table already has the unique index this pattern needs and the quota contract already specifies the outbox, so this is applying a pattern the repository has rather than inventing one. One transaction means a PostgreSQL one: the Redis session hash carries the live scheduling state because the hot path cannot afford a database round trip per frame, and it is a cache that a restart may lose, while the session row and its per-attempt segments are the authority that settlement reads. Losing the cache costs a reassignment; losing the row would cost a charge, which is why they are not the same store. A worker that dies abruptly settles against its last fenced checkpoint the API acknowledged, because work that died with the process is not observable and pretending otherwise is how an estimate becomes a charge.

**A slow browser is shown the newest frame, not every frame it missed.** The shared fleet reader awaits delivery to a browser inline and there are no per-session queues, so reading the code says one browser that stops reading should hold up every other session on that worker. Measured, it does not: `scripts/prototype-slow-consumer.py` stalls one of three sessions for 25 seconds with its receive window closed, and the other two render every frame with their p50 within 0.1 ms of before. That is worth knowing before building the mailboxes to fix a stall that is not there.

What the same measurement does find is that nothing drops the stalled session's frames. It resumes to its whole backlog in order, the oldest 25 seconds old, so a browser that pauses shows a quarter minute of stale canvas before catching up to what the user is drawing now. So the mailboxes are per-session, bounded, and keep only the latest frame, with lifecycle controls and heartbeats ahead of frames and queue ages observable; the reason is freshness rather than isolation, and dedicated writers are worth having anyway because the inline await is one GPU speedup away from mattering.

What already works is kept and must survive the change: slot compensation is ownership-checked, `release` is idempotent, the seed lives on the API session so a reassignment does not re-roll the image, and a malformed worker message never closes the fleet socket.

Rejected alternatives: adding the live-session refusal on the current footing, which is what three reviewed attempts did and where each new defect came from; a per-session assignment lock as the ordering mechanism, which the review showed resurrects terminal sessions and blocks failover when held across an unbounded send; inferring state from the `sessions` dict and the `ready` event as today, which is what makes four writers possible; an opaque attempt identity without ordering, which was this entry's own first answer and cannot reject a delayed open; a single `ending` state per outcome, rendered against never-rendered, where frames and settlement status are orthogonal data on one state; and moving worker-side session state onto the engine, which is where a cached rung and a calibrated slot count already outlive what they describe.

Batch membership is deliberately not a session state. A batch is work with its own short life, collected then executed then retired, and it must never transition its members as a unit: a session that closes mid-batch ends on its own while its mates finish, its output is discarded, and its slot is not advertised free while the GPU cycle it is part of is still running. The earlier claim here that batching multiplies the things that can end a session had it backwards, and issue #294 depends on this entry only for that separation.

## A dispatch is a capability, and its output is written once

A dispatch used to be identified by the job id and the worker object holding it, which cannot separate two attempts of one job, and three things followed from that. The upload key is derived from the user id, the job id and the attempt, so any worker that ever held the job could compute the key of the attempt that replaced it, and the local upload route authorised a PUT for any key that looked in flight. A stall requeue can hand a job back to the same worker object, so a late `job_done` from attempt one passed the identity check and spoke for attempt two. And an object stayed writable after the API had inspected it: the local PUT truncated whatever was there, and a presigned S3 PUT is replayable for its whole hour.

Each dispatch now mints a token. It rides in `dispatch_job`, the worker echoes it on `job_progress`, `job_done` and `job_failed`, and a message carrying the wrong one is ignored. On the local backend the same token authorises the upload, riding in the headers the worker already echoes, so the worker needed no change for that half. Outputs are written once. Locally the body goes to a temporary file in the destination directory, authority is checked again with the bytes in hand, and `os.link` publishes it, which refuses an existing destination and answers 409; that makes `STORAGE_LOCAL_PATH` require a filesystem with hard links. On S3 the upload target signs `If-None-Match`, so the bucket answers 412. A retry writes to a different key, so nothing legitimate needs a second write.

The protocol moved to 3 rather than accepting a missing token from everyone. Without the bump, "accepted when omitted" applied to every worker, so the binding was opt-out: a current worker could leave the field out and get the ambiguity back. `MIN_SUPPORTED_VERSION` stays one behind, so a protocol 2 worker is still admitted and still believed without the field, which is the N-1 exception and ends when the floor moves. A `Worker` built without a registration was lenient in the same way, which registration never produces and issue #282 closes by defaulting the field to the current protocol.

Thumbnails are verified like masters. `has_thumbnail: true` used to create an asset row without reading the object, which let a worker have the studio serve arbitrary bytes as an image. Storage gained a WebP reader in the shape of the PNG one: a RIFF and chunk walk that never decodes, requiring an image chunk with a valid frame header, refusing animations and second bitstreams, and holding a `VP8X` canvas to the frame it wraps. Content type comes from the bytes on both backends rather than from what the uploader declared. A rejected thumbnail costs the row and the object, never the job.

Two limits are deliberate. Proving an image decodes means decoding it, and both readers stay parse-only because an earlier PNG version that decompressed was twice a denial of service; a header with no bitstream therefore still passes, which is issue #281. And a presigned S3 PUT outlives the object it was minted for, so a key deleted by cleanup can be recreated by a replay within the hour: `If-None-Match` only refuses a write when a current object exists. Issue #278 addresses that on the cloud profile by uploading under `dispatch/{user_id}/` and promoting the winning attempt into the durable `{user_id}/` library prefix on commit, with an S3 lifecycle rule expiring `dispatch/` after 24 hours as a backstop. Because `potocolom-images` is versioned, that rule has to expire current objects, expire noncurrent versions, and drop expired delete markers, or the replay remains as a noncurrent version.

> Shipped status (2026-08-20): **implemented** for the dispatch prefix split and library promote on commit (issue #278). The `dispatch/` lifecycle rule is documented in `docs/cloud-infrastructure.md` and `docs/aws-setup.md`, including noncurrent-version expiry on the versioned images bucket; Terraform or console application of that rule is operator-side.

Rejected alternatives: publishing the local upload straight into its key with `O_EXCL`, which was written first and replaced, because the key exists and is readable while the body is still landing, so a `job_done` racing its own PUT could have a truncated prefix inspected and approved; signing the upload URL with the attempt so the key carries its own authority, which puts a secret in a path that is logged by every proxy and cannot be revoked when an attempt is superseded; making the worker send a nonce it chooses, which authenticates nothing the API can check; keeping the key-only authorisation and relying on per-attempt keys alone, which is what shipped and is exactly what a previously dispatched worker can derive; refusing a message with no token from every worker, which is correct at the next floor move and breaks every N-1 worker today; and verifying uploads by decoding them, which is the denial of service the PNG reader already learned to avoid.


## Work stays tracked until the state that ends it is durable

Every terminal path used to remove the job's in-memory entry, its watchdog stamp and the worker's slot, and then write the terminal state. A lock timeout or a commit failure after that point left the row `running` with nothing tracking it: the disconnect handler cannot requeue an entry that is gone and the stall sweeper only walks the in-flight map, so the job was stuck until someone read the database by hand. It needs a database failure to reach, which is why it survived normal testing, and a lock timeout under load is exactly when it matters.

The entry now lives until the commit. Inside the transaction the row is checked for still belonging to this attempt, because a requeue can have replaced it while the verdict was in flight, and a verdict that lost that race is refused rather than allowed to overwrite the winner. After a successful commit the state is cleared only if the entry is still the same one. A transaction that raises leaves everything in place, and the existing recovery paths take the job.

The rule generalises past that one function, and the same defect turned up three more times while this was being written: a recovery queue drained its entry before the recovery ran, a presigned URL was generated outside a guard so a signing failure swallowed a terminal event, and a local upload published into its destination key while the body was still arriving. The check that finds all four is the same: for anything that pops, removes, de-tracks or publishes, ask what re-finds the work if the very next await fails. If the answer is nothing, the removal belongs after the durable step.

The slot is released after the commit, which means a process that dies between them leaks one slot on that worker until it reconnects and rebuilds its state. That is accepted: a reconnect is the only thing that can rebuild the count anyway, and the alternative is releasing a slot for work that may still be running.

Rejected alternatives: clearing the state first and reconciling later from the database, which is a second scanner to maintain and would have to distinguish a job that failed to commit from one still running; holding the entry until a background sweeper notices the terminal row, which delays every ordinary completion to fix a rare one; and wrapping the whole verdict in one transaction including the storage inspection, which holds a row lock across an object read that can take as long as the storage takes to answer.

## Failed cleanup deletes retry forever rather than giving up

Issue #254 asked for a bounded number of retries and one error log, so a permanently undeletable key would not retry forever. It ships without the give-up, and this entry records why the requirement was reversed rather than leaving a future reader to re-file it.

A delete that fails on the terminal path is recorded in `pending_deletes` and retried by a five-minute sweep, backing off by doubling minutes to an hour. Giving up was tried twice. Dropping the row destroys the only record that the object exists: the log line has rotated by the time anyone looks, and nothing else names the key, since the asset row only ever names the winning attempt. Keeping the row but unscheduling it is worse in a different way, because nothing re-arms one: the backoff spans under three hours, and a bucket policy broken at nine and fixed at two leaves every key recorded in between permanently unreachable, recoverable only by hand-written SQL nobody knows to run.

Retrying forever costs one delete call an hour per stuck key, and the row is the record an operator needs. The alert at eight attempts stays, once, because that is the signal; the retries after it are cheap and are what makes the fix arrive on its own when the permission is repaired. The table is bounded by the number of distinct undeletable keys rather than by time.

Rejected alternatives: dropping the row at a bounded attempt count, which is what the issue asked for and which loses the object silently; unscheduling the row and re-arming it at startup, which makes recovery depend on a restart nobody will perform for a cleanup failure; an operator endpoint to re-arm, which is a surface with one caller for a case that a retry already handles; and an S3 lifecycle rule instead of any of this, which is the better answer for the cloud profile and covers only it, so it belongs with issue #278 rather than replacing the retry a self-hosted install needs.

## Secrets at rest: one versioned root key ring, purpose keys derived per use

Account secrets are encrypted under keys derived from a versioned root key ring, never under a root key directly. `ROOT_KEYS` carries comma separated `version:base64key` entries with the newest first: the first entry is the active write key and every entry stays readable. A purpose key comes from HKDF-SHA256 over the root key with the purpose in the info string, so one subsystem's key is useless against another's data. Values are sealed with AES-GCM and carry their key version, and the purpose and any row binding go into the associated data, so a blob cannot be replayed under a different purpose or a different row. Rotation is therefore active write, multi read, re-encrypt, then remove, with no downtime and no flag day. A key that leaves the ring before its rows are re-encrypted makes those rows unreadable, and every refusal raises rather than returning a value: losing a secret is the correct outcome, and silently reading one under a fallback key is not. The ring is separate key material from `FLEET_TOKEN_KEY`, which authenticates workers.

Rejected alternatives: one static key from the environment (no rotation story at all, and every subsystem shares one blast radius); encrypting with the root key directly (rotation then rewrites every row for every purpose at once, and one leaked key exposes everything); a key management service (a hard dependency the self-hosted profile cannot take, for a threat the self-hoster's own disk encryption already bounds); storing the key version only inside the blob (finding the rows still on an old key then means scanning and parsing the whole table instead of reading an index).

## Password and email uniqueness: a functional index, not a second column

Accounts are unique on `lower(btrim(email))` through a unique functional index, and invitations use the same expression in a partial index that admits one open invitation per address. There is no stored normalized column to keep in step.

Rejected alternative: a normalized column filled on write. It duplicates a value the database can compute, adds a backfill to the migration, and goes stale the moment one write path forgets it, which is exactly the write path that also decides who owns an account.

## Credential changes: recent authentication, plus the credential itself

Changing a password requires the current one, not only a session that authenticated recently. The two answer different questions: recent authentication says this browser belonged to somebody a moment ago, and the current password says it still belongs to them at this keyboard. An unlocked laptop satisfies the first and not the second. An account with no password is adding one rather than changing it, and has none to give, so that case asks only for recent authentication.

Every credential change ends the account's other sessions and spares the one making the change. The usual reason to change a credential is that somebody else holds the old one, so leaving their session alive defeats the change; signing the person out of the browser they are working in defeats nothing and is merely irritating.

Rejected alternatives: recent authentication alone (an unlocked laptop is then enough to take an account permanently); requiring the current password for the add-password case too (there is nothing to require, so it would only block accounts that signed in with a provider); revoking every session including the current one (the person changing their password is immediately signed out, which reads as a failure and invites them to try again).

## Changing the primary address resets assurance

`mail_verified` becomes false whenever the primary address changes, and the password identity's subject moves with it. A provider verified the old address; that is not evidence about the new one. The identity has to move because login matches on it, so leaving it behind would let somebody sign in under an address they no longer hold.

Rejected alternative: keeping assurance and re-verifying in the background. It leaves a window in which an unproved address is marked proved, and assurance is exactly what promotion to administrator reads.

## Sharing: a token in the fragment, resolved by POST

A share link is `/shared#<token>`. The fragment is the one part of a URL a browser never sends to a server, so the page reads it and posts it to `POST /api/v1/shared`. A token in a path or a query would be written to access logs, proxy traces, and the `Referer` header of every request the shared page makes, which turns a link somebody forwarded once into a credential sitting in half a dozen systems. Only the SHA-256 of the token is stored, and the answer carries the picture, its prompt and its model plus an address for the original that is signed rather than stored and lasts 60 seconds.

Shares live in `asset_shares`, one active row per asset held by a partial unique index. Sharing an asset that is already shared revokes the old row and inserts the new one in the same transaction, so revoking the link somebody can see can never leave an older one alive behind it. The token stays good until it is revoked or its 1, 7 or 30 days run out.

Rejected alternatives: a public key-addressed GET on the storage key (the key is derivable, so the link is not the capability, and R1 retired those routes); `assets.share_token`, the retired column, which gives two answers to the question of whether an asset is shared and cannot express revocation or expiry at all; a one-use token (a share is meant to be forwarded and reopened, so a link that dies on the first view is not a share); a cached CDN behavior on `/shared/{token}` (the edge TTL then decides how long a revoked link keeps working).

## Account states carry the capability, and cancellation is cooperative

An account's state is the single place that decides whether it may sign in, change anything, hold a GPU slot, or keep a share link resolving. Leaving `active` does all of the revocation in one transaction and then reaches the things a transaction cannot reach: the sockets that bound their principal at the handshake, and the workers holding its jobs.

Suspension pauses shares rather than revoking them. A revoked share cannot be un-revoked without minting a new link and asking the owner to send it again, so revoking on suspension would make a reversible administrative act permanent for everybody who held the link.

Job cancellation writes `cancelled` to PostgreSQL and commits, and only then tells the worker, best effort and bounded. The row is the authority: a worker that never hears the cancellation finishes its image, uploads it, and has it discarded by the terminal-state check that already guards every late verdict. Its measured GPU time is recorded and charged anyway, and the attempt is let go on every path that ends it, so a cancelled job never keeps a dispatch slot on a worker that is still connected.

Rejected alternatives: cancelling the worker's task and treating its silence as the record (a worker that dies mid-cancel then leaves a job running forever in the row); waiting for the worker to confirm before marking the row (the caller waits on a machine that may be wedged, and the answer is already decided); discarding the GPU time a cancelled job spent (the hardware ran, the fleet paid for it, and a free cancel button is a way to run a GPU for nothing); a `cancelled` account state (a person is not a unit of work, and the states that stop an account already exist).

## Deletion waits, and a restore is one level deep

Deleting an account stops it immediately and destroys nothing for thirty days. Stopping is what the person asked for; destroying is what they cannot undo, and the two do not have to happen at the same moment. The account records the state it held when it asked, and a restore puts it back there rather than to `active`: an account that was suspended when it asked to be deleted comes back suspended, because a restore undoes the deletion and not everything before it.

The purge orders itself by the foreign keys: the objects first, since the asset row is the only thing that names them, then the assets, then the jobs, then the user row. One account per transaction, so a sweep that dies halfway loses nothing and the next pass finds the rest exactly where it was. The sweep reads the ids it is going to purge in one transaction and destroys each account in another, so it claims the row before it deletes anything: a restore that lands in between is somebody saving an account, and the claim is what stops the pass from destroying it anyway.

The last administrator may delete their own account. An install with no administrator can be recovered offline, and an administrator who cannot leave their own install is a worse outcome than one that needs a console.

Rejected alternatives: deleting on the spot (a person who deletes an account in anger, or by mistake, has no way back, and no export they forgot to take); a full state history to restore from (nobody has asked what an account was two states ago, and keeping it means keeping a record of every suspension forever); deleting the user row and letting cascades take the rest (the objects in storage are named only by the rows the cascade would remove, so they would leak silently); refusing the last administrator's own deletion (an install can be recovered offline, a person cannot be un-trapped).

## Administration reads one account at a time, and says so

There is no cross-user gallery and no cross-user search. An administrator answering a complaint names the account, and that read writes an audit row carrying the target: the role check sees the caller and the route, never which account the answer came from, so the route records what only it knows.

Reading unusually many accounts raises a flag and refuses nothing. An administrator working a queue of reports and a stolen administrator session produce the same pattern, and a limit would stop the first while barely inconveniencing the second, who only has to go slower.

Rejected alternatives: a global gallery for moderation (one compromised administrator session then reads everybody's work in one request, and the feature exists for cases that name an account anyway); auditing reads in the role check (it cannot know the target, so every row would say only that somebody looked at something); refusing past the anomaly threshold (it turns a support queue into an outage and teaches the attacker to pace themselves).

## Every way back into a locked install is a command at the machine

Recovering an administrator, reclaiming an install, collapsing accounts and rotating the root key are commands run at the machine, and none of them is reachable over HTTP. A route that can mint an administrator, or turn authentication off, is a route worth stealing a session for, and no amount of guarding it changes what it is worth.

Collapsing destroys the accounts and keeps the work. The images belong to the installation and end up on the implicit local user, because ending the accounts is not a reason to burn what people made. The confirmation is a phrase typed out rather than a flag: a flag is too easy to pass by accident and too easy to copy out of a forum post.

Key rotation is two steps with a check between them, and refuses to rewrite a blob it cannot read. `ROOT_KEYS` keeps every version so a running install can still open what an older key sealed; the rotation moves the ciphertext, the check says whether anything is left behind, and only then is the old key safe to delete. An install that rewrote what it could not read would have destroyed a second factor rather than moved it.

Rejected alternatives: an administrator route for any of this (the value of the route is exactly what makes it a target); an automatic rotation on startup (an operator who has not yet added the new key would then have their install rewrite itself into a state it cannot read); collapsing by deleting every row the accounts touched (people lose work they made, for an operational decision that was never about their images).


## Retiring something means removing it, and a dropped column does not come back

A retired thing is deleted rather than left in place and ignored. `assets.share_token` is dropped from the schema, and the retired `local` and `oauth` mode names are gone from the documents as well as from the code. A column nobody writes still answers when somebody selects it, and a mode name that appears only in a diagram is indistinguishable, to a reader, from one that still works.

The drop is not undone by its downgrade. The downgrade recreates a nullable, unfilled column so a previous release starts against the schema, and the values are gone. Nothing has written that column since `asset_shares` landed, so on any install that reached that release there is nothing to lose, and saying so in the migration is what stops somebody reading `downgrade` as a way back.

This is the contract half of expand-contract, and it lands in the same release as the expand half, because the mapping is what put the column in every asset query. Release N-1 therefore cannot read `assets` against schema 0022: measured, it raises `column assets.share_token does not exist`. Self-hosted there is no window, because one process migrates and then serves from the same image. A live cloud runs the migration as a gated task before the roll, so it would need the mapping removed in one release and the column dropped in the next; there is none, and splitting the two would leave the column in the schema for another round with nothing reading it and nothing mapping it.

One retired route stays declared. `GET /api/v1/files/{key}` answers `404` because R1 removed key-addressed asset reads, and the stub is what makes that a `404`: the `PUT` on the same path still matches, so deleting the stub answers `405` on a bare API and `404` only where the SPA mount happens to swallow it. A `405` says the route is there after all, and an answer that depends on whether the API also serves the frontend is not a contract. It is kept out of the OpenAPI schema, because a retired route a generated client still offers is a route that still exists, whatever it answers.

Rejected alternatives: leaving the column in place unused (it survived one design change already by being ignored, which is how a second answer about a revoked link stays available to be believed); copying the retired values somewhere before dropping them (nothing consults them, so the copy is a second retired thing to explain); deleting the retired file route with the column (the absence stops being a `404`); keeping the retired mode names in the documents as history (the documents are the contract and git holds the history, so a name kept there costs a reader a wrong belief and saves nobody a lookup).


## The second factor is optional for every role, and no sign-in is exempt from it

TOTP is optional everywhere. Nothing requires it, no role is held to a different rule, and an installation where nobody enrols one signs in exactly as it did before the factor shipped. It is asked for at one moment: after a primary login has already succeeded, on an account that enrolled and confirmed a factor. It gates nothing else, not setup, not invitation acceptance, not promotion to administrator, not a password reset, and it changes neither what an account may do nor how long its session lasts. A second factor that also decided authority or lifetime would be two mechanisms sharing one name, and the second one is the one an operator turns off.

A provider sign-in passes the same gate. `app/oauth.py` runs the callback through the same enrolled-factor check as the password login, and an account with a factor gets a challenge back rather than a session. A provider proves which account is signing in; it does not answer for the factor the account holder enrolled here. This installation cannot see whether the provider asked for a second factor, whether it still asks, or whether the account over there is still held by the same person, so accepting a linked identity as a factor means trusting a policy it cannot read. Exempting one would also make linking the way around the factor: enrol here, link there, sign in past it. The only difference from the password route is presentation, because the browser arrives by navigation and not by fetch, so the challenge redirects to a page that can ask for the code instead of answering JSON that nobody can type into.

The secret lives in `auth_factors`, encrypted with AES-GCM under a purpose key derived from the root key ring and bound to the account it belongs to, so a blob lifted out of one row does not open in another. Its key version is stored beside it, which makes rotation an indexed sweep rather than a scan that parses every blob to find the stale ones. Nothing is written until a code proves the authenticator really holds the secret: a pending enrolment is sealed under the same key ring and handed to the browser setting it up, so an abandoned enrolment leaves nothing behind, and an account replacing its authenticator keeps the working factor until the new one answers. Starting an enrolment needs recent authentication.

Recovery codes are one use, kept only as hashes, and they rotate with the factor. Confirming an enrolment deletes the previous factor and every code that belonged to it and writes the new set in the same transaction, because a code minted against a secret nobody holds any more is a way in nobody expects. A TOTP code is one use as well: the drift window accepts one step either side, which would otherwise leave a code that has already been used live for another ninety seconds.

What a gated login hands back is a challenge, and a challenge is not a session. It carries a ten minute life, lives in its own cookie, and authorizes nothing: the right code exchanges it for a session, and there is nothing else it can do. Ten wrong answers end it, and the budget belongs to the account rather than to the challenge, because starting another challenge costs nothing to anyone who already has the password. Beginning one spends the account's outstanding challenges and carries their used attempts forward, under a per-account lock so that logins which overlap carry the count the same way logins taken one at a time do, and a session is minted only by the request that actually spends the token. What that bounds exactly is ten guesses per account per challenge lifetime, not ten ever: a challenge that runs out its ten minutes is not carried, because a budget that never reset would make ten wrong guesses a permanent lockout that anybody who knows an address could trigger. Bounding how often a challenge can be started in the first place is the login rate limit, which the entry below records (#423).

Rejected alternatives: exempting a provider sign-in, as the launch-era plan assumed (the install cannot read the provider's policy, and linking would become the cheapest way past a factor the account holder enrolled here); requiring TOTP of administrators (the recovery codes are the only way through the gate once the authenticator is gone, so a mandatory factor turns a lost phone into an install with no administrator); issuing a reduced session before the code and upgrading it after (every route then has to remember which kind it holds, and the first one that forgets is the way in); writing the factor when enrolment starts rather than when a code confirms it (one request from a stolen session, no code and no notice to anybody, would be the cheapest way to disarm the second factor); letting a password reset clear the factor (the link proves a capability and not a person, so control of an inbox would be enough to remove it); a per-challenge attempt budget (a fresh challenge is free to whoever has the password, so the guesses would never run out).

## Provider feedback is believed by its topic, not by its signature

`POST /api/v1/mail/feedback` takes SES bounce and complaint notifications from SNS and retires the addresses they name. It presents no account credential and needs none, but the signature alone is not what makes it trustworthy: SNS signs every topic in a region with the same key, so a valid signature says an AWS customer sent this message and not that our provider said this. `SES_FEEDBACK_TOPIC_ARN` is the second half, and unset means refuse rather than accept anything, because a wildcard here lets whoever can create a topic retire any address on the install.

Suppression is a denial of mail to a real person, so the verdict has to be permanent. A transient bounce is a full mailbox or a server having a bad afternoon, and retiring an address for one locks somebody out of the reset link they are waiting for. Only a permanent bounce and a complaint count.

The certificate URL in an SNS message points somewhere, and this API is the thing that would go there, from inside the private subnet, at a sender's choosing, so it is checked against an SNS host and the one published certificate path before the fetch rather than after. The subscribe URL is not fetched at all: confirming through the SNS API with this task's own credentials keeps the one-use token in that URL out of the request log, and makes the confirmation authenticated rather than a GET anyone could have aimed. Only `SignatureVersion` 2 is accepted: version 1 signs with SHA-1, a topic can be told to use 2, and taking both would keep the weaker one alive for the sake of one setup step.

Rejected alternatives: trusting the signature alone (every AWS customer can produce one); a shared secret in the subscription URL (SNS puts the whole URL in its own delivery logs and in the subscription record, and rotating it means resubscribing); polling the SES suppression list instead (it answers about the account's list rather than about our sends, it costs a scheduled call forever, and the delay is exactly the window in which the address keeps being invited); accepting transient bounces to be safe (that is safe for the sender and not for the person who cannot receive their link); confirming subscriptions by hand (a resubscription then needs an operator, which is how a feedback path quietly stops working).


## Replacing a second factor costs the factor being replaced

Enrolling a second factor writes nothing until a code confirms it, so an abandoned enrolment leaves a working factor working. That protects a factor from being destroyed and did not protect it from being replaced. The confirming code was checked against the secret the enrolment had just minted, and the account's existing factor was then deleted to make room, so proving the new authenticator was the whole price of removing the old one.

Whoever asked for the new secret holds the new authenticator by definition, which makes that proof worth nothing against the case it needs to stop. A session with recent authentication could enrol a factor it controlled and take the account's own out in two requests, silently; the owner discovered it when their authenticator stopped working. The case is a stolen session, or one opened before the factor was enrolled, rather than a phished password: with a factor in place a password alone answers with a challenge and not a session. Recent authentication is not the barrier either, since it can come from a provider sign-in as readily as from a password.

Replacing therefore costs a code from the factor being retired, or one of its recovery codes. A first enrolment has nothing to replace and is asked for nothing, because requiring proof of a factor that does not exist would mean an account could never enrol at all. Somebody moving to a new phone still has the old one; somebody whose phone is gone has the codes, and using one to enrol a replacement is exactly what they want to do.

The delete is bound to the factor that was proved, and the request continues only if it is the one that removed it. Deleting by account instead lets the last writer replace a factor it never proved: two replacements racing would each remove whatever was there and install their own, so the one that lost the race still ends up owning the account's second factor. A first enrolment deletes nothing at all, because deleting by account there was the same hole in the case where both callers believed there was nothing to replace; `auth_factors` holds one row per account by unique constraint, and letting that constraint refuse the second is the right answer.

Asking costs something, or it is not an ask. Ten tries end it, counted before the code is looked at so every one is paid for whatever happens next, right or wrong, and answering a login challenge with the factor gives the budget back. Without a count the caller answers the new half correctly every time and grinds the old half through six digits at leisure, and without the reset ten tries by anybody would stop the owner ever moving to a new phone. What it bounds is ten attempts between one successful factor challenge and the next, not ten in a lifetime.

Rejected alternatives: asking for the old code when the enrolment starts rather than when it is confirmed (starting one writes nothing, so refusing there only tells a caller to come back by another route, and it costs an honest person a code before they have set anything up); accepting the new code alone and notifying the account by mail (mail is optional on a self-hosted install, and a notice that arrives after the factor is gone is a record rather than a control); requiring recent authentication and nothing more, which is what shipped (it is the property a stolen session already has, and a session is what this needs to stop rather than a password: with a factor enrolled the password answers with a challenge).


## Turning a second factor off costs what turning it on costs

An account may remove its second factor, and doing so needs recent authentication and a code: a current one from the authenticator, or a recovery code. That is the same bar replacing one asks for, and for the same reason. A session by itself must not disarm a factor, or the factor protects an account only until somebody steals a cookie, which is the situation it exists for.

A factor that could not be removed at all was the shipped behaviour, and "optional for every role" is not true of something you can turn on and never turn off. It left an account whose authenticator broke spending recovery codes until they ran out, and then unreachable. For the last administrator it was worse: the offline command prints a password link and the challenge still stood behind it, so an install could reach a state that only `auth-collapse` could recover, and that destroys every account on it.

Guessing at removal spends the budget replacing a factor spends. Two doors onto the same six digits with a counter on only one of them is not a counter.

The counter is on the digits alone. A recovery code is twenty symbols from an alphabet of thirty-two, which no online guessing reaches, so charging the same ten tries for one bought nothing and cost the account its way back: whoever held a stolen session could spend all ten on rubbish and leave the owner holding a working code that no route would look at. A presented value is matched against the account's unspent recovery codes before anything is charged, and only a value that is not one of them costs a try.

Removing a factor ends the account's other sessions, as enrolling one does. The argument is stronger here rather than weaker: the account is less protected afterwards than it was before, so whoever else is holding a session should not be carried across the change.

The account that has lost the authenticator and every recovery code has nothing to present, and no route can safely help it, so `make auth-clear-factor` is a command at the machine like every other way back in. It ends that account's sessions too, because somebody running it has lost control of the second factor and whether anybody else has hold of the account is exactly what nobody knows. It records an actorless high-severity audit row: nobody signed in did this, and a factor disappearing is the kind of thing somebody should be able to find afterwards.

Rejected alternatives: a removal route that takes only a session and recent authentication (it is the disarm-from-a-stolen-session hole that enrolment is written to avoid, and it would be the weakest point in the whole design); letting a password reset clear the factor (the link proves control of a mailbox, and mail is what the second factor exists to survive); an administrator route to clear another account's factor (the same route worth stealing a session for, one privilege level up, and an administrator who needs it can run the command); leaving it unremovable and documenting the lockout (it makes an account's own security setting a trap, and hands the install a state recoverable only by destroying every account).

## The factor routes exclude each other on the account, not on the factor row

Every route that changes a second factor takes a per-account advisory lock as the first statement of its write transaction, and keeps the `SELECT ... FOR UPDATE` on the factor row underneath it. The row lock is what orders `auth_factors` before `recovery_codes`, which is a separate invariant and still required. The account lock is what exists when the row does not.

A first enrolment has no factor row, so there is nothing for a row lock to hold, and two transactions that should have been exclusive were not. The damage is not hypothetical and was reproduced on two connections before it was fixed: `clear-factor` deleting a factor it could not yet see, an enrolment committing between that statement and the next, and `clear-factor` then deleting the recovery codes that enrolment had just written, leaving an account with a factor and no way back past it.

The lock is always acquired before any row lock, by every route that takes it. That ordering is what makes it safe to add to routes already arranged against deadlock: a lock every holder takes first can serialise them, but can never be the second edge of a cycle.

A route refuses with 503 rather than queueing on it, because a waiter holds a connection out of a pool fifteen deep and its transaction has written nothing at that point. The bound is on that wait alone and is lifted once the key is held: a row lock taken afterwards waits as it always did, because leaving the timeout on would turn it into a failure no handler expects. An attempt a previous transaction already counted stays counted, which is what a refused guess costs anyway. The offline command waits instead: it owns its process, has no pool to starve, and an operator at a terminal can afford a short transaction.

Rejected alternatives: widening the row lock to the account's rows, which still locks nothing when the account has none; a unique constraint alone, which does refuse a second factor but says nothing about the recovery codes written beside it; serialisable isolation for these routes, which turns a rare interleave into a retry loop every caller has to implement; and one global lock, which would serialise unrelated accounts on the login path.


## Explicit invalidation closes the sockets it can see, and a sweep closes the rest

Deepens "Realtime authorization: bind once, invalidate explicitly". That entry stands: a realtime connection binds its principal once, and revocation reaches it by an explicit close rather than by re-authorizing every frame, which is still rejected for the same reason it always was.

What it did not say is which process does the closing. The index of live sockets is a dictionary inside the process holding them, so explicit invalidation only reaches sockets the revoking code is standing next to. The offline operator commands are not: `make auth-clear-factor` runs `python -m app.operator` in a process of its own, where that map is empty, so it revoked the session rows, closed nothing, and printed that it had ended every session the account had open. The account kept a live socket and its GPU slot at the exact moment somebody had decided nobody could be trusted with the second factor any more.

So the socket-owning process sweeps as well: on a fixed interval it asks PostgreSQL which of the account sessions behind its live sockets are still live, and closes the ones that are not. PostgreSQL is already the account-session source of truth in a deployment without Redis, so this adds a periodic read rather than a store. It covers every revocation this process did not perform itself, not only the operator commands: direct SQL, a maintenance script, anything a later replica does.

The bound it accepts is stated rather than hidden. A socket revoked from outside the process closes within one sweep interval instead of at once, and a sweep that cannot reach the database closes nothing at all. That second choice is the opposite of the one the handshake makes, on purpose: the handshake judges the single socket it is about to hand a GPU slot to and fails closed, while the sweep judges every socket on the installation at once, so failing closed would sign an entire install out of a live canvas over one blip. Revocation the API performs itself is unaffected and still immediate, because the explicit close runs first and the sweep only finds what it missed.

Rejected alternatives: re-authorizing per frame, which the parent entry rejects and which a thirty second timer does not become; PostgreSQL LISTEN and NOTIFY, which is faster but holds a dedicated connection open for the life of the process, only fires when the revoking code remembered to send it, and would leave direct SQL and every maintenance path exactly as broken as before; Redis pub/sub, which the cloud profile will have and a self-hosted install must not be made to run for this; giving the operator commands a way into the API process, which needs an authenticated control channel into the very thing these commands exist to recover when it cannot be reached.


## The login limit counts in PostgreSQL, and makes the sixth try wait

Signing in is charged against two subjects: the identifier presented and the address it came from. Ten per identifier in ten minutes is a ceiling and answers 429; the thirty per address the specification asks for is read as a pace rather than a ceiling, so that bucket counts and waits and never refuses. Both are free for the first five attempts and then wait half a second that doubles to a cap of eight, which the tenth attempt reaches exactly as the identifier ceiling does. The specification asked for this from the start and the code did not have it (#423), so a challenge budget of ten guesses bounded nothing: starting another challenge was free, and ten guesses every ten minutes for as long as somebody cared to ask is not a bound.

The wait is half the control rather than a softener on the other half. A ceiling on its own answers instantly until it answers 429, which says where the line is and hands anybody who knows an address a way to keep its owner out for the rest of the window. A wait that grows costs an attacker the whole window and costs a person who mistyped their password twice nothing at all. The identifier ceiling still exists because a wait alone is only a tax, and eight seconds is a tax a patient attacker pays; what the two together buy is that the expensive part arrives long before the refusal does.

The count is a row in PostgreSQL. It is correct in both deployment profiles as they stand, it adds no service to a compose file that deliberately has no Redis, and it stays correct when the cloud runs a second replica, which is the exact place a counter in process memory silently admits the allowance twice. The login path already pays for an Argon2id verification, so one upsert beside it is not what makes this route slow. Redis is where this state belongs once the cloud profile has it, and swapping it in is a change behind `charge_login` rather than a change to the login route.

Charged before the password is verified, for three reasons that all point the same way. Argon2id is the expensive half of the route, and a limit that runs after it has already paid the cost it exists to bound. An attempt charged afterwards is free whenever the route leaves early, which is every attempt an attacker cares about. And charging before anything has been looked up is what keeps an address nobody holds indistinguishable from one somebody does, which is what `ABSENT_ACCOUNT_HASH` is in that route to protect: a limit consulted after the account was found would answer differently for an address that exists, and give away by refusal what the constant-time verification refuses to give away by timing.

Every attempt is counted, right or wrong. What this bounds is somebody who already holds the password and is grinding a second factor by starting challenge after challenge, and every one of those attempts succeeds; a counter that charged only failures would count none of them.

Both subjects are stored as digests. The identifier is whatever address the caller typed, so the plain column would be a list of addresses anybody can write to, including people who hold no account here, and the caller's address is raw IP, which this specification keeps to expiring keys only. A digest counts identically and reads back as nothing. Rows carry the end of their window, are reset rather than resumed by the first attempt after it passes, and are dropped by the maintenance loop that already prunes audit and OAuth rows. That end is set by the attempt which opened the window and is never pushed forward by the ones after it, so ten guesses by a stranger cost the owner ten minutes and not a minute more; a window that slid with every attempt would let somebody knocking once a minute hold an account shut indefinitely.

One statement does the counting, an upsert that returns the new total. A read followed by a write loses increments here the same way it lost them in the challenge budget before #421: two attempts that overlap both see the same total and both store one more than it, so a flood is charged for a fraction of what it spends. The wait is served after the session is closed, because the pool is fifteen deep and a delay reaching eight seconds with a connection in hand would spend the pool on callers doing nothing with it.

What this does not do is tell a fronted deployment which caller it is talking to. The address is the socket peer, which is what uvicorn rewrites from `X-Forwarded-For` for peers `FORWARDED_ALLOW_IPS` trusts, so a correctly configured proxy is already handled and a wildcard there lets a caller choose its own limit bucket. That is the same configuration hole `app/main.py` already warns about for the fleet socket, and not one this entry closes separately.

The address bucket carries no ceiling, and the shared peer above is the reason. The specification names thirty per IP and does not say whether that number is a ceiling or a pace, and read as a ceiling it turns a shared peer into an outage: everybody behind one NAT, one proxy that has not been named in `FORWARDED_ALLOW_IPS`, or the loopback publish the shipped compose file uses arrives here as a single address, so thirty attempts between all of them would sign the whole installation out of `/api/v1/auth/login` ten minutes at a time. That is a denial of service the limit creates rather than one it prevents. Read as a pace it costs a bystander nothing: the address count feeds the wait instead of a refusal. The wait is only a pace if the turns are taken one at a time, and that had to be built rather than assumed. A sleep is latency for one request and not a rate, so twenty attempts from one peer that each wait the eight second cap at the same moment are all answered inside one eight seconds: measured, twenty finished in 8.206s, which made the first version of this cheaper to spray against than the ceiling it replaced. The turn is therefore stored on the address row, which the counter has already locked, so overlapping attempts read different turns and serve them in order. Once the cap arrives that peer gets one attempt every eight seconds, a few hundred an hour, which is the bound the number was asking for. A turn more than thirty seconds out is answered 503 rather than held, because a connection kept for as long as a flood cares to lengthen the queue is an amplification rather than a limit, and a 503 says the route is busy where a 429 on a sign-in reads as the account being shut. So the bucket does refuse, and what it costs a bystander should be stated rather than glossed: behind a shared peer they wait up to the thirty second cap when they are admitted, and while an attacker keeps taking slots they are answered 503 for as long as that lasts, plus one cap after it stops. That is a busy signal bounded by the cap and recoverable the moment the flood ends, which is what separates it from the ceiling this rejected: a ceiling refuses for the rest of the ten minute window and nothing the bystander or the attacker does shortens it. A refused attempt takes no turn, and neither does one the identifier ceiling has already refused, or a single spent identifier could hold every slot on its peer without ever waiting in the queue it was filling. The account keeps a ceiling either way, because the identifier bucket is scoped to the one account it protects and a refusal there reaches nobody else.

Rejected alternatives: Redis now (the self-hosted profile has none, and adding a service to the compose file so that logins can be counted is the trade this project has refused everywhere else); a counter in process memory (free and correct exactly until a second replica exists, at which point the limit quietly becomes twice what it says, and it is the kind of wrong that shows up in production and never in a test); a hard refusal with no delay (it makes the limit a lockout anybody can trigger against an address they merely know, and gives an attacker a clean signal for where to stop); a 429 on the address bucket as well, which is the other reading of thirty per IP (it costs an installation behind one NAT, one untrusted proxy or the shipped loopback publish its entire login route for ten minutes at a time, and a shared peer reaches thirty on ordinary traffic, so the limit would go off far more often against users than against an attacker the wait already holds to a few hundred attempts an hour); counting only failures (the attempts worth bounding are the successful ones, because the password is already known by the time this matters); and a keyed digest over the identifier (better against a leaked table, but it puts the key ring in the login path to protect rows that live ten minutes and hold no secret).



## A credential change rotates the token of the session making it

Changing a password, an address, a linked identity or a second factor ends the account's other sessions. Until #436 it left the session making the change exactly as it was, so a copy of that browser's cookie went on working through every one of them. A stolen session is a copy of a token, not a separate sign-in, which makes the one session these changes never touched the one they most needed to end. Revoking was implemented everywhere and rotating nowhere.

So the token is replaced under the person making the change. They stay signed in, the response carries a new session cookie and a new CSRF cookie beside it, and every copy of what they presented is dead. All six call sites get it from the one function they already shared, which now returns what it issued rather than nothing; the routes set the cookie because only they hold a response.

The token is replaced on the session row rather than by revoking the row and inserting another. The session keeps its id, its clocks and its recent authentication, so nothing downstream has to learn that a rotation happened. Three things depend on that. The realtime socket bound this session id at its handshake, and the sweep that closes sockets whose session died asks PostgreSQL by id: a new row means the caller's own canvas closes within thirty seconds of their own password change, for no reason its owner could see, which the explicit close sparing the old id hides rather than fixes. The recent-authentication window is what the next credential change is gated on, so a fresh row either drops it, telling somebody who proved themselves a moment ago to prove themselves again, or re-opens it, which lets a stolen session hold that window open indefinitely by changing something cheap. And the absolute expiry keeps running, so a change cannot be used to extend a session's life.

It happens inside the caller's transaction, for the reason every durability bug in this repository has had in common: the token is handed to a browser, and a change that rolls back must not leave that browser holding the only working credential on the account. The existing `rotate` commits in a transaction of its own and could not be used here for that reason, quite apart from what a second connection writing rows the caller's transaction has already locked would do: block on a transaction that is itself blocked waiting for it, which is one process on both ends and so not a cycle PostgreSQL can see or break.

The account's session rows are locked in id order first. Without that, two changes racing on the same account from two different sessions deadlock: each revocation holds the row the other one is about to rotate. The routes serialise on the account earlier in their own transactions, but not on anything they share, so a change of address and an unlink reach these statements together, and PostgreSQL breaks the cycle by killing one of them, which reaches somebody as a 500 on a password change.

The swap itself is a compare-and-swap: the row is matched on the token the caller presented as well as on its id. Locking the rows in order keeps two changes from deadlocking and does nothing about two requests carrying the same token, because the principal was resolved in a transaction that has already ended by the time either of them writes. Both used to be answered, and that is what makes losing this race expensive now. The owner's password change committed and set a cookie; a stolen copy of that same cookie changed the address a moment later and set a cookie of its own; the owner was signed out holding a dead token while whoever held the copy kept the session and landed their change as well. Without the rotation both cookies simply went on working, so this is a cost the rotation introduces rather than one it inherits. The request whose token is no longer the stored one is refused and its transaction rolls back, so it is handed no cookie and its change does not stand.

409 rather than 401 for that refusal. The answer is that the request collided with a concurrent change, not that the caller has no session, and 409 is what four other lost races on these same routes already answer with. A 401 would also read as a sign-out to a browser that double-submitted, whose session is in fact alive under the token the winning request handed back.

`operator.collapse` deletes sessions after the second factor rather than before it, because of the same rotation. The command is offline, but nothing stops the API serving while somebody runs it, and confirming or removing a factor holds `auth_factors` and `recovery_codes` while it rotates the session making the change. With sessions deleted first that pair is a cycle, measured on PostgreSQL as a deadlock that kills one side: a 500 on the route, or a collapse the operator has to run again. `auth_tokens` stays ahead of `auth_factors` because answering a challenge claims the challenge before it locks the factor, so the order is `auth_tokens`, `auth_factors`, `recovery_codes`, `sessions`, `auth_identities`. No foreign key ties those five together, so the order is free to choose.

Putting `auth_tokens` ahead of `sessions` there made a cycle out of every transaction that took the two the other way round, and four did: the rotation itself when it spends the mailed capabilities, and the role, state and deletion changes that write the same two statements for themselves. Measured on PostgreSQL, each of the six routes over those four, run against a collapse with a live reset link present, was a DeadlockDetected that killed the collapse. So the direction is settled everywhere rather than only in the command: `auth_tokens` before `sessions`, in the routes as well as in the deletes. The rotation spends the links before it locks the session rows, and the three modules write their two statements in that order; `recovery.complete` already read that way and needed no change. The factor routes are outside this: they rotate with `spend_capabilities` off and never touch `auth_tokens`.

Two bounds are left, and no reordering of the deletes reaches either. The credential routes write the identity before they call the rotation, so they take `auth_identities` before `auth_tokens` where the collapse deletes it last: that knot is issue #444, and a password change, an address change or an unlink can still deadlock with a collapse there. And a collapse has to delete `users` last because the foreign keys point at it, while every route that changes an account locks that row first, so a role, state or deletion change can still deadlock with a collapse on the account row. This entry claims one cycle closed, not all of them.

The cost is accepted rather than solved. A response lost in flight leaves the client holding a revoked token and signed out, where today it leaves them signed in. That is a real regression in a rare case, traded against closing a copied-token hole in a common one, and the recovery is a sign-in the person is already in front of.

One thing the rotation does not reach is a realtime socket the intruder already had open. That socket bound the session id at its handshake, the id is still live because the rotation kept it, and nothing tells it apart from the owner's socket, which is the same session. Closing every socket on the account instead was rejected for now: it takes the owner's own canvas down in the middle of securing their account, and nothing in the browser reconnects a socket closed as unauthorized. Explicit invalidation spared the current session's socket before this change too, so the rotation neither improves that nor worsens it, and every request the intruder makes outside that socket is refused from the moment the change commits.

Rejected alternatives: revoking the session outright and making the person sign in again, which is honest and which nobody reads as anything but a failed change, so they try it a second time and the account ends up with two of whatever the change was; leaving it as it is and documenting the gap, which the specification already refuses by saying these events revoke or rotate as appropriate, and which leaves the eviction working against every attacker except the one who copied a cookie; rotating for password changes only, on the grounds that a password is the strongest case, which leaves an address change and a factor enrolment evicting everybody but the intruder and makes the rule impossible to state; rotating outside the transaction, which is simpler to write and hands out a live token for a change that did not happen; and moving the rotation to the top of the factor transactions so it takes the session rows before `auth_factors` and stops cycling with a collapse, which puts the swap back before the change it belongs to and reopens exactly the rollback hole doing it in place closes.

## A sign-in decides whether it may mint in the transaction that mints

A password login read the account's factors, found none, and minted in a transaction of its own. An enrolment committing between those two revokes every session that exists at that moment, and the one the login is about to insert is not among them. What is left is a live session inside an account whose owner has just turned a second factor on, past a gate that did not exist when the login started, and the owner believes they have ended it. A challenge that finishes its proof reaches the same place from the other side: a replacement or a removal commits, and the session minted afterwards belongs to a factor the account no longer has. The provider callback is a fourth way in, written the same way and wrong for the same reason.

One helper covers every one of them, at the mint site rather than at each reader. It takes the per-account advisory key the factor routes already take, reads the confirmed factor and inserts the session, in one transaction. It is given the factor the caller proved, or nothing for a password login, which passed the gate by there being nothing to prove. When what it finds is not that, it mints nothing and says so: the login answers with the challenge it would have handed back had it read the factor a moment later, and the challenge route refuses, because the code it accepted was good for a factor that is gone.

Re-reading the factor after the insert, inside the minting transaction, was measured before it was rejected, and it does not close this. On two connections the minting transaction inserted its session and re-read, seeing no factor; the enrolment then inserted its factor and ran its revocation, which cannot see a row the other transaction has not committed and so reached nothing; both committed, and the account was left holding one confirmed factor and one live session. Two transactions that never touch a common row do not serialise, so the re-read narrows the window rather than closing it. The key closes it because both sides take it, which makes whichever commits first the one the other reads.

The key is released with that transaction, before the login decides what to do about a gate it found closed. `begin_challenge` takes the same key in a transaction of its own, so calling it while the gate transaction is still open is a wait on a key the same request is holding: measured at three seconds, the whole of the `lock_timeout` `_hold_the_account` sets, and then a 503 in answer to a correct password. Not a deadlock PostgreSQL can see or break, because the two waits sit on different connections. Just a bounded wait that is always lost.

Taken first, before any row lock, the way every other holder takes it, so it can serialise but can never be the second edge of a cycle. `answer_challenge` still does not take it inside its write transaction, which claims `auth_tokens` before it locks `auth_factors` because `operator.collapse` deletes in that order; the gate runs after that transaction has committed, in one of its own that locks no rows at all. The login pays no round trip for any of this: reading the factor and minting were two transactions, and are now one. What it does pay is what every factor route already pays for the key: a 503 telling it to come back, if three seconds of contention on that one account go by without it. Nothing has been written when that happens, and the key is per account, so a flood aimed at one account cannot reach the sign-ins of any other.

The proof a refused challenge spent stays spent, and costs nothing. Both routes that close this gate take the account's recovery codes with them, and a step is matched against the factor being replaced or removed, so the credential the loser gave up belongs to something that no longer exists. That is not the case #424 and #430 are about, where a code was consumed for a change that never happened and the account kept the factor it was presented to.

Rejected alternatives: re-reading the factor after the insert, which the issue preferred and which the measurement above leaves a live session behind; minting and then revoking, which hands a browser the cookie for a session it then kills, so a sign-in that failed has to be told apart from one that succeeded by something other than the cookie it set; serialisable isolation for these transactions, which does close it and turns a rare interleave into a serialisation failure every sign-in has to retry; and a guard at each reader, which was four places to keep in step by the time this was written and leaves whichever path is added next unguarded.


## Linking a provider is a credential change

The entry above says that a password, an address, a linked identity or a second factor ends the account's other sessions. That was true of an unlink and not of a link, which is the half of "a linked identity" that adds one. A link is what somebody does when they are securing an account they believe is at risk, and it is also what an intruder holding a copy of the browser's cookie does to keep a way in after the owner changes the password. It now runs the same rotation the other five routes run, as the last statement of the transaction that writes the identity, so a lost compare-and-swap takes the link out with it: a link that stands while its rotation is refused leaves a new way into the account and every old session alive, which is worse than either outcome on its own.

It spends the account's outstanding reset and recovery links, which the factor routes deliberately do not. A second factor stands in front of the password a reset link sets, so a link left alive there costs nothing and keeps a way back in for somebody who has just secured their account. A linked provider gates nothing: whoever holds the mailbox still spends the link, sets a password and signs in, past a provider that was never asked. Keeping it would leave open the one door the person linking meant to close, which puts linking with the credential routes rather than with the factor routes.

The rotation needs the session the flow started from, so the callback resolves the browser's session cookie and refuses with the same 403 as everything else this route will not do, when that session is gone or belongs to another account. A flow lasts ten minutes; a session that ended inside them was signed out or revoked, and completing the link then would add a way into the account and end nothing, which is the reverse of what revoking it meant.

Rejected alternatives: leaving the links alone as a factor change does, which reads as the same argument and is not, because what keeps a reset link harmless there is the gate the factor puts in front of it and a provider has no such gate; linking from a browser whose session has ended and revoking every session instead, which holds no token to rotate and hands a credential change to a browser that can no longer prove anything; and rotating after the link commits, in a transaction of its own, which leaves the identity standing and nothing rotated on exactly the 409 the credential routes already answer.


Chosen as conventional defaults rather than debated decisions:

- PostgreSQL with SQLAlchemy and Alembic migrations. One database engine in every mode; docker compose makes it trivial for self-hosters.
- Redis only in the cloud profile (queue, session scheduling, rate limiting). Self-hosted installs do not need it.
- Object storage behind an adapter: local filesystem by default, S3 compatible in the cloud.
- Docker Compose as the self-hosted distribution format.
- VRAM requirements are per model metadata (min_vram_gb in the manifest), applying across GPU vendors.
- Monorepo: frontend, backend, worker, deploy and docs live in this repository.
- Documentation diagrams are written in Mermaid, which GitHub renders as drawn diagrams; UI wireframes stay ASCII because they sketch screen layouts.
