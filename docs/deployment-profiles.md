# Deployment profiles and migration

How one codebase is every deployment, and how an installation moves between profiles. This document consolidates the reuse mechanics scattered across [architecture.md](architecture.md), [blueprint.md](blueprint.md) and [decisions.md](decisions.md) into one place, then specifies the migration paths those mechanics make possible.

## The base principle

The self-hosted version is the base and the cloud is a configuration of it, never a fork. Three rules enforce this:

1. One build. A release tag produces one set of container images and one SPA artifact. GHCR serves self-hosters, ECR mirrors the same digests for the cloud. There is no cloud build.
2. No mode branches. Application code never asks "am I self-hosted or cloud"; it reads specific settings (`REDIS_URL`, `STORAGE_BACKEND`, `AUTH_MODE`, `QUOTA_SERVICE_URL`, `BILLING_ENABLED`, `SAFETY_CHECKS`). Every difference between deployments is one of these values.
3. Seams with two implementations. Where behavior must differ, an interface owns the difference: `Queues` and `FrameBus` (in-process or Redis), `Storage` (local filesystem or S3 with signed URLs), `QuotaService` (unlimited or the billing service over HTTP), and the auth mode module (`none`, `accounts`). The interfaces are specified in [blueprint.md](blueprint.md); the wire and API surface above them never change.

The proof mechanism is the cloud-sim compose in [local-development.md](local-development.md): the application demonstrably cannot tell nginx from an ALB or MinIO from S3, because the seams are the only place the difference could show.

> Shipped status (2026-07-30): **partially implemented.** Storage and the generation-job in-process queue have concrete seams. The Redis queue adapter, realtime admission queue, FrameBus implementations, shared invalidation, and multi-owner scheduler do not exist yet. "Realtime and queue Redis seam: optional, behaviorally equivalent" and the issue "Redis-optional Queues and FrameBus contracts" govern those designed profile differences.

## The profile spectrum

Profiles are points on one axis, not different products. Each arrow is configuration, not code.

```mermaid
flowchart LR
    DEV["Local development<br>native apps, deps in containers"]
    SH["Self-hosted<br>compose, one GPU, no Redis"]
    SSH["Scaled self-hosted<br>Redis + several workers"]
    CLOUD["Cloud<br>AWS + rented GPU fleet + billing"]
    SIM["cloud-sim compose<br>validates the seams locally"]
    DEV -->|"same images"| SH
    SH -->|"set REDIS_URL,<br>add worker containers"| SSH
    SSH -->|"S3 storage, accounts,<br>QUOTA_SERVICE_URL, infra"| CLOUD
    SIM -.->|"rehearses"| CLOUD
```

| Setting | Local dev | Self-hosted | Scaled self-hosted | cloud-sim | Cloud |
|---|---|---|---|---|---|
| AUTH_MODE | none | none or accounts | accounts | accounts | accounts |
| OAUTH_PROVIDERS | | | | | google,github |
| BILLING_ENABLED | false | false | false | true (fake) | true |
| SAFETY_CHECKS | false | false | false | false | true |
| TELEMETRY | false | true (default) | true | false | false, not applicable |
| DATABASE_URL | dev compose | compose postgres | compose postgres | compose postgres | RDS |
| REDIS_URL | empty | empty | compose redis | compose redis | ElastiCache |
| STORAGE_BACKEND | local | local | local | s3 (MinIO) | s3 + CloudFront signing |
| QUOTA_SERVICE_URL | empty | empty | empty | fake service | billing service |
| EMAIL_BACKEND | none or Mailpit | none | smtp | Mailpit | SES |
| LOG_FORMAT | plain | plain | plain | plain | json |
| Workers | 1, native | 1, compose | N, compose | 1-2 | rented fleet, autoscaled |

The scaled self-hosted column deserves a note: it is not a separately designed product. Setting `REDIS_URL` switches dispatch and the frame relay to the Redis implementations, and additional worker containers simply dial the same fleet endpoint. A lab or studio with three GPU machines gets multi-worker scheduling with the exact scheduler the cloud runs (issue #20), for the cost of one Redis container.

`AUTH_MODE` is `none` or `accounts`; the retired `local` and `oauth` names are gone, and Google and GitHub are options within `accounts` rather than modes of their own. Both modes are implemented for REST. `accounts` still refuses the realtime socket, which has no principal of its own until issue #19 gives it one, so the studio needs `none` today.

> Shipped status (2026-07-30): **not yet implemented.** Setting `REDIS_URL` currently changes neither job dispatch nor realtime relay, and the backend has no Redis dependency. This remains the migration target under "Redis-optional Queues and FrameBus contracts" and issue #20, "Multi-Worker Scheduling".

## What is shared, layer by layer

| Layer | Shared across every profile | Varies |
|---|---|---|
| REST and WebSocket API | every endpoint, request, response, close code in [api.md](api.md) | nothing |
| Frontend | one static build; behavior driven by `GET /api/v1/config` | the values that endpoint returns |
| Worker | the whole worker, protocol, manifests, `DEVICE=cuda/rocm/cpu`, the low VRAM memory ladder (`MEMORY_MODE`, [architecture.md](architecture.md)) | the hostname it dials, where weights come from (HF or R2); the ladder matters on consumer GPUs, the cloud fleet runs fully resident |
| Database schema | identical, one migration history | instance it runs on |
| Dispatch and relay | the interfaces and the scheduler logic | in-process vs Redis implementation |
| Storage | the interface, storage keys, asset rows | filesystem vs S3; plain paths vs signed URLs |
| Quota | the reserve/commit/refund interface, metering events | unlimited vs the billing service |
| Auth | session mechanics, cookie, revocation | which methods exist |
| Metrics | `usage_events` schema, output categorizer, admin usage view ([metrics.md](metrics.md)) | self-hosted sends daily anonymous telemetry; the analytics warehouse is cloud only, private repo |
| Not shared at all | | AWS infrastructure; the private billing, autoscaler and analytics services |

## Side by side: the same request through both profiles

Both columns run identical container images and expose identical endpoints; every difference below is one environment variable or one seam implementation, never a code branch.

```mermaid
flowchart TB
    subgraph SH["Self-hosted: AGPL, your hardware, free"]
        B1["Browser"] -->|"SPA, REST and WS served by the API itself"| A1["API server, one container"]
        A1 --> AU1["Auth: none (auto login) or accounts (email+password)"]
        A1 --> Q1["Quota: UnlimitedQuota, no payments"]
        A1 -->|"in-process dispatch"| W1["Your GPU worker(s)<br>memory ladder for small VRAM"]
        A1 --> S1["Storage: local disk"]
        A1 --> M1["Metrics: usage_events in own PostgreSQL<br>+ daily anonymous telemetry, TELEMETRY=false to stop"]
    end
    subgraph CL["Cloud: same images, paid subscriptions"]
        B2["Browser"] -->|"SPA via CloudFront, REST and WS via the ALB"| A2["API replicas on ECS"]
        A2 --> AU2["Auth: email+password, Google, GitHub"]
        A2 --> Q2["Quota: billing service over HTTP (private repo)<br>Stripe subscriptions and credits"]
        A2 -->|"Redis queue + scheduler"| W2["Rented GPU fleet, autoscaled (private repo)<br>fully resident models"]
        A2 --> S2["Storage: S3 + signed CloudFront URLs"]
        A2 --> M2["Metrics: usage_events in RDS<br>-> analytics warehouse (private repo)"]
    end
```

The corresponding editable diagram is the Profile comparison page in [diagrams/](diagrams/).

## Cloud-readiness ledger

This table records local choices whose deferred consequences have a concrete
scaling trigger. It is a status ledger, not a roadmap or a new decision record.

| Local choice | Why it is safe today | Trigger that forces the change |
|---|---|---|
| **Database connection pooling.** `backend/app/db.py` uses `NullPool`, so each request and fire-and-forget write opens a connection. Tests currently drive requests and WebSockets on separate event loops, and asyncpg connections are loop-bound. | A single self-hosted API process has modest connection concurrency. | More than one API replica, or use of managed PostgreSQL with a connection ceiling. Move the tests to one event loop, then tune the pool per profile. |
| **In-process queue and relay.** Generation jobs use an in-process heap. Realtime workers and sessions live in process-local dictionaries and frames are forwarded by direct awaited socket sends; there is no FrameBus or realtime admission queue. | Exactly one process owns every worker and browser socket, and a full realtime pool closes with 4003. | Before adding another socket-owning process, implement "Redis-optional Queues and FrameBus contracts" and issue #20, "Multi-Worker Scheduling", then configure the Redis-backed queue and FrameBus. |
| **Container log rotation.** Compose uses Docker's bounded `json-file` driver. | The self-hosted stack runs as Docker containers on one host. | The cloud deployment. ECS must use `awslogs`; the compose logging block does not apply there. |
| **In-memory observed model timings.** Each API process learns per-model GPU speed from recent succeeded jobs, seeded by committed reference-card timings. | The existing maintenance loop refreshes the derived cache every five minutes, and a database failure safely restores the shipped seed. | More than one materially different GPU profile serving the same install. Record worker identity or memory mode on each job, then key observations by that hardware profile. |
| **Usage rollup scale.** Worker identities are pruned after 30 days. Raw `usage_events` are retained for 90 days; older complete UTC days become daily per-user and per-dimension `usage_event_rollups`. | Per-event growth is bounded while daily user presence and additive measures remain available for cohorts and retention. | `usage_event_rollups` period scans slowing at measured fleet volume. Partition by `bucket_date` without changing the rollup contract. |

## Migration paths

Every path below is possible because the schema, storage keys and API are identical everywhere. Paths that need a small tool name the issue that ships it; nothing here requires code the architecture does not already plan.

### Local filesystem to S3 compatible storage

1. Create the bucket (S3, MinIO, R2 - anything S3 compatible).
2. Copy the asset tree: `aws s3 sync /data/assets s3://bucket/` (or `mc mirror`). Storage keys are backend agnostic, so objects land under the same keys the database rows already reference.
3. Set `STORAGE_BACKEND=s3` with the bucket settings and restart the API.
4. Nothing else changes: URLs are minted per request by the adapter, so history and share links keep working. Rollback is the reverse flip (sync back first if writes happened in between).

### Enabling accounts on an install that ran without them

`AUTH_MODE=none` runs everything as one implicit local user who owns every row. `make auth-enable` is the switch: it writes the root key ring, records the change in PostgreSQL, and prints a one-use link valid for an hour. The first claimant adopts that implicit user, keeping its UUID, so the existing library comes with them rather than being copied or stranded.

The switch is one way. An install that has enabled accounts refuses to start in `none` mode again, because falling back would return a multi-user install to answering every request as an administrator. Undoing it needs an offline destructive reset. Google and GitHub are additive within `accounts`: password identities stay valid and providers appear beside them.

### Adding Redis and more workers (self-hosted scale-out)

> Shipped status (2026-07-30): **not yet implemented.** These are target migration steps, not an available switch today. They depend on "Redis-optional Queues and FrameBus contracts" and issue #20, "Multi-Worker Scheduling".

1. Add a Redis container and set `REDIS_URL`.
2. Restart the API: queues rebuild from PostgreSQL job rows (Redis is never the source of truth, so there is nothing to migrate into it), the session cache warms lazily, and the frame relay switches to pub/sub.
3. Start more worker containers pointing at the same `API_URL`. They register, advertise slots, and the scheduler spreads sessions.
4. Reversal: unset `REDIS_URL` and drop to one worker. Nothing is lost.

### Self-hosted into the cloud, and back out

The same schema and storage layout make an install portable in both directions:

- In: `pg_dump` the database, restore into RDS (same migration history, so versions must match or be upgraded first); `aws s3 sync` the assets into the images bucket; users log in again (sessions are deliberately not migrated). Billing state starts fresh because credit ledgers live in the private service and never existed self-hosted.
- Out (the no-lock-in direction, and the point of the license): the same steps in reverse, or per user via the GDPR export (issue #10), which yields their data without operator involvement. A customer can leave the cloud for their own GPU and keep their library.

Cross-install migration is mechanics the architecture guarantees, not a polished launch feature; the first packaged tooling for it can come whenever demand appears, and will be thin because the hard part is already free.

### Version migrations within any profile

Already decided and specified elsewhere, listed here for completeness: one project version per release tag; the worker protocol supports N-1 so fleets and self-hosters upgrade without lockstep ([connection-handling.md](connection-handling.md)); database migrations are expand-contract, applied as a gated task in the cloud and automatically on API startup self-hosted ([decisions.md](decisions.md)).

## What can never migrate

The private repository services (billing, fleet autoscaler) and the AWS infrastructure are the commercial layer; they integrate over HTTP boundaries (`QUOTA_SERVICE_URL`, metering events, worker fleet tokens) and are not part of any self-hosted profile. The open source repository stays complete without them: the default implementations allow everything, which is both the self-hosted behavior and the reason the license boundary holds.
