# Usage metrics and telemetry

What the platform measures about its own use, where those measurements live, and what leaves a self-hosted install. The goal is to answer product and investor questions - what are people creating, with which models, how often do they come back - without cookies, third party trackers or any client side beacon. Everything here is server side rows derived from requests the API already handles.

## The questions this answers

- What are users creating: art, photo editing, design assets, characters, NSFW content, split by day and by plan.
- Which models and tiers do they choose, and how does the optional `model_id` routing actually get used.
- How much time do they spend: realtime drawing minutes, queued generations per session, days active per week.
- Retention and cohorts: DAU/WAU, how usage changes after the first week, which categories retain.
- The self-hosted install base: how many installs exist, which versions, which GPUs and memory ladder rungs.

The first four come from usage events; the last one from telemetry. They are separate streams with separate privacy rules.

## The planes at a glance

What flows where, and what never leaves the deployment:

```mermaid
flowchart TB
    W["Worker<br>GPU sample rides every 30 s heartbeat<br>CLIP category attached at job_done"]
    A["API"]
    subgraph FLEET["Fleet plane: per heartbeat, hardware detail"]
        RH[("Redis worker hash<br>live fleet view, autoscaler")]
        CW["CloudWatch fleet aggregates<br>cloud profile only"]
        LOG["One JSON log line<br>Logs Insights history"]
        GS[("PostgreSQL gpu_samples<br>raw 48 h, 5 min rollups 30 d<br>studio usage panel")]
    end
    subgraph USAGE["Product plane: raw events plus cohort rollups"]
        UE[("PostgreSQL usage_events<br>raw detail for 90 days<br>never prompts, images, IPs")]
        UR[("PostgreSQL usage_event_rollups<br>daily per user and dimension tuple<br>long-lived cohort history")]
        ADMIN["Admin usage view"]
        OWN["Studio own-metrics view"]
    end
    subgraph TELE["Telemetry plane: self-hosted installs, daily"]
        AGG["Aggregate-only payload<br>previewable, TELEMETRY=false disables"]
        ING["Private ingest service"]
    end
    W -->|"heartbeat over the one WSS"| A
    W -->|"job_done, session_closed"| A
    A --> RH
    A --> CW
    A --> LOG
    A --> GS
    A --> UE
    UE -->|"roll up complete UTC days,<br>then prune raw rows"| UR
    UE --> ADMIN
    UR --> ADMIN
    UE --> OWN
    UR --> OWN
    UE -->|"counts by action, category, tier<br>joinable to no person"| AGG
    AGG -->|"one POST per day"| ING
```

## Usage events: per-event rows in the deployment's own database

Every completed job and every closed realtime session writes one row to a `usage_events` table in the deployment's own PostgreSQL. The same code runs in both modes: a self-hosted admin gets the same view of their instance that the cloud has of its fleet. Nothing about this stream crosses the network.

| Field | Content |
|---|---|
| user_id | FK to users; deleted with the account purge |
| kind | `job` or `realtime` |
| action | `generate`, `edit`, `enhance` or `draw` |
| model_id, tier | what actually ran, after routing |
| category, category_score | top label from the output categorizer below |
| gpu_ms, duration_ms, frames | cost and effort of the event |
| created_at | timestamp |

Deliberately never stored in this table: prompt text, images, IP addresses, user agents. Time-on-project metrics derive from these server-visible rows (session durations, first to last activity per day), not from any frontend ping; the frontend contains no analytics code at all.

Raw rows are retained from the start of the UTC day 90 days before the current
day. This keeps at least 90 days of event-level detail: far more than the previous
UTC day read by telemetry, and enough for short-horizon session, funnel and
quarter-scale product questions. The fixed window gives both deployment
profiles the same analysis contract; their different event rates change the
bounded raw row count, not the meaning of the data.

Because pruning is aligned to midnight, the raw table contains between 90 and
91 days of arrivals: at an average `R` completed events per day, about 90 x `R`
to 91 x `R` rows.

On the existing five-minute maintenance loop, complete older UTC days are first
written to `usage_event_rollups`, then the corresponding raw rows are deleted in
the same transaction. The rollup grain is one row per user, UTC day, kind,
action, model, tier and category. It stores the event count and sums for category
score (with a separate non-null score count), GPU time, duration and frames.
Daily user presence answers the long-horizon retention question directly: did
this user return on a later day or in a later week or month. It also preserves
DAU/WAU, days active per week and cohorts whose first week begins on the user's
signup date; a weekly or monthly bucket would blur those questions.
Exact event ordering for funnel sequences is available only inside the raw
window; older history keeps the funnel's daily dimensional counts, not a
replayable event stream.

The row consequence is one row per dimension tuple a user uses on each active
day. A user active every day with one tuple produces 365 rollup rows per year;
with `D` distinct tuples every active day, the result is 365 x `D`. This remains
long-lived history, but growth is periodic and dimension-bounded instead of one
row per completed event. At the current product paths, `D` comes from four
kind/action paths, six categories, the registered models and the tier values
actually used.

Both raw and rollup rows remain user-linked because that is what retention,
cohort and funnel analysis need. The obligations apply to both tables: the
foreign keys use `ON DELETE CASCADE`, so both are hard deleted with the account's
30 day purge, and issue #10's GDPR export must include both
([architecture.md](architecture.md), content safety and privacy).

## Content categorization

The worker categorizes each output image with a CLIP zero-shot pass against a fixed label set - `art`, `photo_edit`, `design`, `character`, `nsfw`, `other` - and attaches the top label and score to `job_done` (for queued jobs) and to `session_closed` (for realtime, classifying the final frame). SD-class pipelines already ship a CLIP encoder, so this is one extra embedding comparison at a point where the image is already in memory, in both modes and on every device type.

Categorization is metrics, not moderation: it runs regardless of `SAFETY_CHECKS`, and the diffusers safety checker remains the only enforcement path. A self-hosted install with safety off still labels its own NSFW output correctly in its own statistics.

## Telemetry from self-hosted installs

Self-hosted installs report anonymous daily aggregates to project infrastructure, on by default with a single switch to disable (`TELEMETRY=false`). This supersedes the original zero-phone-home decision ([decisions.md](decisions.md)); the design keeps the properties that make opt-out defensible:

- The payload is aggregates only. No user ids, emails, prompts, images or per-user rows ever leave the install; the report cannot be joined back to a person.
- The full payload is documented here and printable locally (`GET /api/v1/telemetry/preview` shows exactly what would be sent).
- The API logs the destination, the payload summary and the off switch at every startup, so no admin discovers it by reading traffic.

One POST per day to the ingest endpoint (private repo service, versioned like the quota contract):

```json
POST https://telemetry.potocolom.com/v1/report
{
  "install_id": "random uuid, generated at first boot, carries no identity",
  "version": "0.3.1",
  "day": "2026-07-09",
  "active_users": 3,
  "events": {"job": 41, "realtime": 12},
  "by_action": {"generate": 30, "draw": 12, "edit": 8, "enhance": 3},
  "by_category": {"art": 25, "photo_edit": 9, "design": 6, "nsfw": 8, "other": 5},
  "by_tier": {"draft": 35, "standard": 18},
  "realtime_minutes": 74,
  "workers": [{"device": "rocm", "memory_mode": "model_offload"}]
}
```

The `workers` field includes identities whose `last_seen` is at or after the
start of the reported UTC day. A continuously connected worker remains included,
while workers gone before that day drop out. A worker first seen on the following
day can be included in the previous day's report; this small over-inclusion avoids
an unbounded identity history without adding a per-day activity log.

A failed send is dropped, never queued: telemetry must never affect the install that emits it.

## Operational metrics: planes and export paths

Usage events and telemetry are the product plane. Three more planes cover operating the system, each with one export path and one rule about where detail lives:

| Plane | Source | Export path | Where it lands |
|---|---|---|---|
| Fleet and GPU | worker heartbeat samples | the existing WSS connection - workers are never AWS principals | Redis worker hash (live), CloudWatch fleet aggregates, one JSON log line per heartbeat |
| Service | API and private services | `PutMetricData` in the `potocolom` namespace + structured JSON logs | CloudWatch metrics, Logs Insights |
| Money path | API outbox, billing webhooks, autoscaler | same as service plane | CloudWatch metrics and alarms; machine-hour rows in the autoscaler's store |

The cardinality rule that keeps CloudWatch cheap: aggregates become metrics, detail stays in Redis (live) and logs (history). Per-worker CloudWatch dimensions would multiply ephemeral worker ids by metric names at a price per metric; the admin fleet view and Logs Insights already answer per-worker questions for free.

## GPU fleet metrics

The worker samples its card once per heartbeat - GPU utilization, VRAM used and total, temperature, power - with one `nvidia-smi` subprocess on CUDA or one combined `rocm-smi --json` subprocess on ROCm. The blocking hardware query runs through `asyncio.to_thread`, off the async event loop that relays realtime frames and dispatches jobs. The ROCm parser prefers structured output and retains the human-readable regex parsers as defensive fallbacks. The API fans each heartbeat out three ways: the `worker:{id}` Redis hash (the admin fleet view and the autoscaler read this), fleet-level CloudWatch aggregates (workers connected, slots in use and free, average and max GPU utilization, minimum VRAM free), and one JSON log line for history.

In-process NVML and amd-smi bindings remain a possible future optimization with no licensing obstacle. They are not adopted here because each GPU family would add a new vendor dependency, while the single subprocess per heartbeat captures most of the benefit.

A multi-GPU machine runs one worker process per GPU, pinned by device index, so every GPU is one connection, one heartbeat stream and one set of slots - the fleet view lists them all individually with no special casing. The admin area is the live many-GPU console; CloudWatch is for trends and alarms; Logs Insights is for the post-mortem on one specific worker.

The studio's own usage panel has a fourth consumer: each heartbeat's GPU sample is also written to the deployment's PostgreSQL (`gpu_samples`, raw rows kept 48 hours) and rolled into five-minute buckets (`gpu_sample_rollups`, kept 30 days) by a maintenance loop in the API. Static `device` and `memory_mode` facts live once per worker in `workers`, refreshed at registration and heartbeat, instead of being repeated on every sample. `GET /api/v1/metrics/gpu/history` serves raw and rolled-up samples: raw rows for windows up to an hour, rollups beyond. This is per-install history for the user's own hardware; the CloudWatch plane above stays aggregate-only.

## Frame loop metrics

Two recorded decisions depend on observing the realtime loop, so its numbers are first class: per-model p95 frame time, measured at the worker (inference) and at the relay (end to end), and the frame drop rate from latest-input-wins. The worker-side half of the p95 pair ships: the worker keeps a bounded window of observed frame times per model, the heartbeat carries them as `frame_p95_ms`, and the API serves them as `realtime_p95_ms` on the manifest, which is what the studio's model picker labels a model with. That field is a fleet number rather than one worker's: it is the median across the workers that hold the model and have measured it ([decisions.md](decisions.md), "The advertised frame cost is the fleet's median"), so a worker that has not measured cannot mask one that has, and a single stale card cannot set the label. Worker-side p95 feeds the slot calibration benchmark's ongoing sanity check, with the caveat that the two are not yet the same quantity (issue #288): calibration times the whole frame, while an observed timing starts just before the diffusion call, so anything derived from the pair inherits the gap. Relay-side p95 is the explicit trigger for the gateway extraction ([decisions.md](decisions.md), "Realtime relay") and does not exist yet: nothing times frames at the relay today. A rising drop rate says sessions are degrading before users say it, and the worker does count dropped frames per session, but that counter has no wire home yet.

## Unit economics

The number the pricing model stands on is utilization: gpu_ms sold (summed from
non-overlapping raw `usage_events` and older `usage_event_rollups`) divided by
machine-hours bought (the autoscaler's per-machine-hour accounting rows). The
warehouse computes it by joining the two; it is reviewed weekly against the
pricing assumptions, and a sustained fall below the assumed floor is a money
alarm, not a curiosity.

## Dashboards and the boundary

- Both modes: the admin area (issue #28) gains a usage view over the instance's own `usage_events` - top models, categories, active users, GPU time.
- Cloud only, private repo: the analytics warehouse. It reads the cloud deployment's `usage_events` and the telemetry ingest database, and produces the investor-facing numbers (growth, retention, category mix, install base). Per the boundary rules in [repository-boundary.md](repository-boundary.md) it never imports public code; the telemetry payload above is a public, versioned contract exactly like the quota interface.

## Commitments, in one place

No cookies beyond the session cookie. No third party analytics or trackers,
self-hosted or cloud. No prompt or image content in any metrics store. Raw usage
events and their user-linked rollups die with the account. The telemetry payload
is public, aggregate-only, previewable and one variable away from off.
