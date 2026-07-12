# Implementation blueprint

This document sits between [architecture.md](architecture.md) and real code. It gives each load-bearing mechanism a concrete shape: configuration keys, Redis layout, load balancer definition, authentication flows, the scheduler loop, the worker protocol and the seam interfaces. The pseudocode is normative for structure and naming, not for syntax: implementation refines the low level details but should not change the shape without updating this document and [decisions.md](decisions.md).

## Configuration and profiles

All mode differences are environment variables read once at startup into a settings object. Nothing branches on "self-hosted or cloud"; code branches on the specific setting.

```
AUTH_MODE            = none | local | oauth      # default none
OAUTH_PROVIDERS      = google,github             # only read when AUTH_MODE=oauth
BILLING_ENABLED      = false | true              # default false
SAFETY_CHECKS        = false | true              # prompt screen + output checker, default false
TELEMETRY            = true | false              # self-hosted daily aggregate report, see metrics.md
DATABASE_URL         = postgresql://...
REDIS_URL            = ""                        # empty: in-process fallbacks (self-host)
REDIS_URL_CACHE      = $REDIS_URL                # per-concern endpoints; set individually
REDIS_URL_QUEUE      = $REDIS_URL                # to split Redis later without code changes
REDIS_URL_PUBSUB     = $REDIS_URL
REDIS_URL_RATE       = $REDIS_URL
STORAGE_BACKEND      = local | s3
STORAGE_LOCAL_PATH   = /data/assets              # local backend
STORAGE_S3_BUCKET    = ...                       # s3 backend, plus region and credentials
QUOTA_SERVICE_URL    = ""                        # empty: unlimited default implementation
FLEET_TOKEN_KEY      = ...                       # verifies worker tokens; self-host uses a static shared secret
EMAIL_BACKEND        = none | smtp | ses
PUBLIC_URL           = https://app.potocolom.com
```

The frontend learns everything it needs at runtime:

```python
@app.get("/api/v1/config")
async def config():
    return {
        "auth_methods": auth_provider.methods(),   # e.g. [] | ["local"] | ["local","google","github"]
        "billing_enabled": settings.billing_enabled,
        "languages": ["en", "es"],
    }
```

## Redis layout

One instance at launch. Every key belongs to exactly one concern, every concern gets its client from its own `REDIS_URL_*` setting, so splitting into multiple servers later is a configuration change. PostgreSQL is always the source of truth; every structure below can be rebuilt from it.

| Key or channel | Type | TTL | Purpose |
|---|---|---|---|
| `sessions:{token_hash}` | hash | 300 s | cache of the session row; miss falls back to PostgreSQL |
| `rate:{bucket}:{subject}` | counter | window | rate limit windows per user id or IP |
| `queue:jobs` | sorted set | none | job ids scored by tier priority then enqueue time |
| `queue:admission` | sorted set | none | waiting realtime session requests, same scoring |
| `rt:{session_id}:to_worker` | pub/sub | n/a | browser to worker leg of the frame relay |
| `rt:{session_id}:to_browser` | pub/sub | n/a | worker to browser leg |
| `worker:{worker_id}` | hash | 90 s | connected worker: models, slots, slots_in_use, protocol_version, api_replica |
| `session:{session_id}` | hash | none | realtime session state: user, worker, state, last_input_ms |
| `sched:leader` | string | 10 s | scheduler leader lease, value is the replica id |
| `invalidate:sessions` | pub/sub | n/a | replicas drop cached sessions on logout or revocation |

Scoring for both queues, lower pops first:

```python
def score(tier: int, now_ms: int) -> float:
    # tier: 0 = resuming idle session, 1 = paid, 2 = trial
    return tier * 1e13 + now_ms
```

Atomic pop, so two schedulers (during a leader handover) can never dispatch the same entry:

```lua
-- pop_best.lua  KEYS[1] = queue key
local e = redis.call('ZPOPMIN', KEYS[1], 1)
if #e == 0 then return nil end
return e[1]
```

Fixed window rate limit, one round trip:

```lua
-- rate_check.lua  KEYS[1] = rate key, ARGV[1] = limit, ARGV[2] = window seconds
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return n <= tonumber(ARGV[1]) and 1 or 0
```

Queue position for the waiting room UI is one call: `ZRANK queue:admission {request_id}`.

Self-hosted installs set no `REDIS_URL` and get in-process implementations of the same interfaces, which is the whole trick that keeps Redis out of the compose file:

```python
class Queues(Protocol):
    async def push(self, queue: str, id: str, tier: int) -> None
    async def pop(self, queue: str) -> str | None
    async def position(self, queue: str, id: str) -> int | None

class RedisQueues(Queues): ...      # sorted sets + pop_best.lua
class InProcessQueues(Queues): ...  # a heap in the single API process

class FrameBus(Protocol):
    async def publish(self, channel: str, payload: bytes) -> None
    def subscribe(self, channel: str) -> AsyncIterator[bytes]

class RedisFrameBus(FrameBus): ...      # pub/sub between replicas
class InProcessFrameBus(FrameBus): ...  # asyncio queues in one process
```

When load justifies it, the split path is: move `REDIS_URL_PUBSUB` first (frame relay is the latency and throughput hot spot), then `REDIS_URL_QUEUE`; the cache and rate keys can share an instance indefinitely.

## Load balancer

The ALB definition, as Terraform-shaped pseudocode. The commentary in [cloud-infrastructure.md](cloud-infrastructure.md) explains each number.

```hcl
resource "aws_lb" "main" {
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
  idle_timeout       = 120            # heartbeats every 30 s keep sockets alive at 4x margin
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = aws_acm_certificate.api.arn
  default_action    { forward { target_group = aws_lb_target_group.api } }
}

resource "aws_lb_listener" "http_redirect" {
  port     = 80
  protocol = "HTTP"
  default_action { redirect { port = 443, status_code = "HTTP_301" } }
}

resource "aws_lb_listener_rule" "stripe_webhooks" {
  listener_arn = aws_lb_listener.https.arn
  condition    { path_pattern { values = ["/webhooks/stripe*"] } }
  action       { forward { target_group = aws_lb_target_group.billing } }
}

resource "aws_lb_target_group" "api" {
  target_type          = "ip"        # required for Fargate
  port                 = 8080
  protocol             = "HTTP"
  deregistration_delay = 120         # lets the app close WebSockets cleanly on deploy
  health_check {
    path                = "/api/v1/health"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  # no stickiness block, deliberately: frames cross replicas via Redis pub/sub
}
```

`GET /api/v1/health` answers from process state only (no database round trip), so a database incident does not convince the ALB to kill healthy tasks and make everything worse.

## Authentication

### Session middleware

Every request passes one middleware. Opaque token in an httpOnly cookie, hash stored server side, cache in front of PostgreSQL.

```python
async def session_middleware(request, call_next):
    request.user = None
    token = request.cookies.get("session")
    if token:
        h = sha256(token)
        s = await cache.get(f"sessions:{h}")
        if s is None:
            s = await db.sessions.active(token_hash=h)     # source of truth
            if s:
                await cache.set(f"sessions:{h}", s, ttl=300)
        if s and not s.user.deleted_at:
            request.user = s.user
    return await call_next(request)
```

Logout and revocation delete the row, delete the cache key and publish on `invalidate:sessions` so every replica drops its copy; the five minute cache TTL is only the backstop.

### Local registration and login

```python
@app.post("/api/v1/auth/register")            # AUTH_MODE=local or oauth
async def register(email, password, attest_18):
    await rate.check(f"rate:signup:{client_ip}", limit=5, window=3600)
    require(attest_18)                        # cloud terms
    require(not disposable_domain(email))     # cloud only
    user = await db.users.insert(email=email, role="user")
    await db.auth_identities.insert(user, provider="local",
                                    password_hash=argon2id.hash(password))  # OWASP parameters
    await email_backend.send_verification(user)   # EMAIL_BACKEND=none skips, marks verified

@app.post("/api/v1/auth/login")
async def login(email, password, persistent: bool):
    await rate.check(f"rate:login:{client_ip}", limit=10, window=300)
    await rate.check(f"rate:login:{email}",     limit=10, window=300)
    ident = await db.auth_identities.get(provider="local", subject=email)
    if not ident or not argon2id.verify(ident.password_hash, password):
        raise Unauthorized                    # same response either way
    token = secrets.token_urlsafe(32)
    await db.sessions.insert(user=ident.user, token_hash=sha256(token),
                             persistent=persistent,
                             expires_at=now() + (days(30) if persistent else hours(12)))
    await email_backend.notify_new_signin(ident.user)
    # secure only when served over HTTPS: browsers drop Secure cookies on plain
    # http://localhost, which local development and LAN self-hosting both use
    set_cookie("session", token, httponly=True,
               secure=settings.public_url.startswith("https"), samesite="lax")
```

### OAuth callback

```python
@app.get("/api/v1/auth/callback/{provider}")   # google, github
async def oauth_callback(provider, code, state):
    verify_state(state)                        # CSRF: state minted at redirect time
    claims = await providers[provider].exchange(code)   # id token or userinfo
    ident = await db.auth_identities.get(provider=provider, subject=claims.sub)
    if not ident:
        user = await db.users.find_or_create(email=claims.email)
        ident = await db.auth_identities.insert(user, provider, subject=claims.sub)
    # from here identical to local login: mint token, insert session, set cookie
```

### The mode seam

```python
class AuthProvider(Protocol):
    def methods(self) -> list[str]
    def mounts(self) -> list[Route]           # which endpoints exist in this mode

NoneAuth   # methods() = [];        middleware maps every request to the single local user
LocalAuth  # methods() = ["local"]; register, login, verify, reset
OAuthAuth  # methods() = ["local", *providers]; LocalAuth plus redirect and callback routes
```

## Scheduler

One logical scheduler, leader elected among API replicas with a Redis lease. Self-hosted there is one process, so it is simply always the leader.

```python
async def scheduler_task():                    # runs in every replica
    while True:
        if await redis.set("sched:leader", replica_id, nx=True, ex=10):
            await lead()                       # returns if the lease is ever lost
        await sleep(2)

async def lead():
    while await renew_lease():                 # SET XX with value check, every 3 s
        await step()
        await sleep(0.1)

async def step():
    # 1. Reconcile: forget workers whose heartbeat hash expired; their jobs
    #    requeue (attempt 2) or fail; their sessions enter Reassigning.
    await reconcile_workers()

    # 2. Idle release: sessions with last_input_ms older than 60 s
    #    release their slot, stop metering, state = Idle.
    await release_idle_sessions()

    # 3. Admission: fill free realtime slots from queue:admission.
    while (slot := free_realtime_slot()) is not None:
        req = await queues.pop("queue:admission")
        if req is None: break
        await preempt_job_if_running(slot)     # pause between denoising steps, requeue
        await open_session(req, slot)          # via the worker's WebSocket

    # 4. Jobs: hand queue:jobs entries to workers with idle capacity,
    #    respecting the hot set (prefer a worker with the model loaded;
    #    otherwise trigger an on-demand load and report it on the job).
    while (w := worker_with_capacity()) is not None:
        job = await queues.pop("queue:jobs")
        if job is None: break
        await dispatch(job, w)
```

Everything the loop mutates lives in Redis hashes (`worker:*`, `session:*`), so a leader handover picks up mid-flight state; anything ambiguous after a crash is rebuilt from PostgreSQL rows. Resuming idle sessions re-enter `queue:admission` with tier 0, ahead of new sessions of any plan.

## Worker protocol

One WebSocket from worker to `wss://api.../api/v1/fleet`, authenticated by a short lived fleet token (cloud) or the shared secret (self-host). Text messages are JSON control; binary messages are image payloads.

| Direction | Message | Payload |
|---|---|---|
| worker to api | `hello` | protocol_version, models with capabilities as measured (the memory ladder may drop `realtime`, see [architecture.md](architecture.md)), realtime_slots, gpu info |
| api to worker | `registered` or `rejected` | rejected carries min_supported_version |
| worker to api | `heartbeat` | every 30 s: slots_in_use, vram_free, loaded_models, gpu (util, vram_used, vram_total, temperature, power - NVML or amd-smi, see [metrics.md](metrics.md)) |
| api to worker | `dispatch_job` | job id, model, params; `load_model` first if not loaded |
| worker to api | `job_progress`, `job_done`, `job_failed` | done carries gpu_ms and the output `category` ([metrics.md](metrics.md)); failed carries reason |
| api to worker | `open_session`, `close_session`, `pause_job`, `drain` | drain: finish current work, stop accepting |
| worker to api | `session_ready`, `session_closed` | closed carries gpu_ms, frames and the final frame's `category` |
| both | binary frame | 1 byte type, 16 byte session uuid, then WebP payload |
| api to browser | `credits_tick` | on the browser socket: live drain display while Active |

Version gate at registration, implementing the N-1 promise:

```python
if hello.protocol_version < CURRENT_PROTOCOL - 1:
    send(rejected(min_supported_version=CURRENT_PROTOCOL - 1)); close()
```

The browser's realtime socket speaks the same shape: JSON control (`open`, `ready`, `queued` with position, `prompt_update`, `idle`, `resuming`, `credits_tick`), binary canvas frames up, binary generated frames down.

## The generation request path

Rejects cheapest first; nothing touches a GPU until everything else passed.

```python
@app.post("/api/v1/generations")
async def create_generation(req, user = require_user()):
    await rate.check(f"rate:jobs:{user.id}", limit=..., window=60)
    model = registry.resolve(req.model_id, req.params)   # tier routing when model_id is absent
    if settings.safety_checks:
        await safety.screen_prompt(req.prompt)          # blocklist + classifier, raises
    reservation_id = uuid4()                             # caller-supplied: retries are idempotent
    await quota.reserve(user, reservation_id, estimate_gpu_ms(req),
                        ttl_s=900)                       # raises InsufficientCredits
    job = await db.jobs.insert(user, model.id, req.params, state="queued",
                               reservation=reservation_id, attempt=1)
    await queues.push("queue:jobs", job.id, tier(user))
    return {"job_id": job.id}
```

Worker death mid job: the scheduler's reconcile step requeues `attempt=1` jobs as `attempt=2`; a job failing at `attempt=2` becomes `state=failed`, `quota.refund(reservation_id)`, and the frontend shows the retry button. Completion runs the output safety check on the worker, uploads to a presigned URL, then `quota.commit(reservation_id, actual_gpu_ms, images)`. Actual usage may exceed the estimate; commit charges actuals, and a balance briefly going negative only blocks new reservations until the next grant. Idempotency, expiry and the outage posture of these calls are specified under Quota contract semantics below.

## Prompt screening

Cloud profile only (`SAFETY_CHECKS=true`). Runs in the API before `quota.reserve`, so a refused prompt costs neither GPU time nor credits, and on the realtime socket for `open` and every `prompt_update`, where a rejection refuses the update but keeps the session alive.

```python
def normalize(prompt: str) -> str:
    # NFKC fold, casefold, strip zero-width characters,
    # homoglyph and leetspeak mapping - evasion dies here or nowhere

async def screen_prompt(prompt: str, user) -> None:
    text = normalize(prompt)
    verdict = rules.match(text)                       # curated patterns; combination
                                                      # rules (age x sexual), not words
    if verdict is None:
        verdict = await classifier.classify(text)     # small CPU model, multi-label,
                                                      # milliseconds, in-process
    if verdict.hard:                                  # above all: any sexualization of minors
        await strikes.record(user, verdict.category)  # category + timestamp only,
                                                      # never the prompt text
        raise PromptRejected(message=GENERIC_POLICY_MESSAGE)
    if verdict.soft:                                  # ToS-prohibited, not radioactive
        raise PromptRejected(message=category_message(verdict.category))
```

Hard verdicts answer with one generic message (the screen never teaches which pattern tripped) and count as strikes; crossing the strike threshold sets the same suspended-pending-review flag the payment dispute path uses, and a suspended account cannot reserve. The baseline rule list ships in this repository, so a self-hosted install that enables `SAFETY_CHECKS` gets real protection; the cloud loads a supplementary private list from a configured path. The output-side backstop is the worker's safety checker on generated images (see [decisions.md](decisions.md), "Content safety"), which also covers what no text screen can see: the canvas stream itself.

## Seam interfaces

The four seams from [architecture.md](architecture.md), as they appear in code. Each has exactly two implementations at launch; which one loads is pure configuration.

```python
class QuotaService(Protocol):
    # reservation_id is generated by the caller, so a timed-out call retries with
    # the same id and at most one reservation exists. raises InsufficientCredits.
    async def reserve(self, user, reservation_id, estimated_gpu_ms, ttl_s) -> None
    async def commit(self, reservation_id, gpu_ms, images) -> None
    async def refund(self, reservation_id) -> None

UnlimitedQuota   # default: reserve always succeeds, commit records a metering event, refund no-op
BillingQuota     # HTTP calls to QUOTA_SERVICE_URL (the private billing service)

class Storage(Protocol):
    async def upload_target(self, key) -> UploadTarget   # presigned S3 PUT, or an internal API route
    async def url(self, key, ttl) -> str                 # signed CloudFront URL, or /files/{key}
    async def delete(self, key) -> None

LocalStorage     # files under STORAGE_LOCAL_PATH
S3Storage        # bucket + CloudFront signing

# Queues and FrameBus are specified in the Redis section above.
```

### Quota contract semantics

The wire shape of `BillingQuota`, part of the versioned public contract the fake QuotaService also implements (see [repository-boundary.md](repository-boundary.md)):

```
PUT  /v1/reservations/{id}            {user, estimated_gpu_ms, ttl_s}
                                      201 created | 200 already exists (idempotent retry)
                                      402 insufficient credits | 409 not in reserved state
POST /v1/reservations/{id}/commit     {gpu_ms, images}
POST /v1/reservations/{id}/refund     {}
```

A reservation is a one-way state machine: `reserved -> committed | refunded | expired`. Repeating a transition returns 200 and changes nothing; a conflicting transition returns 409. The billing service expires reservations still `reserved` after `ttl_s` and returns the credits, which is what makes a crash between reserve and enqueue harmless. Rationale in [decisions.md](decisions.md), "Quota contract: caller-supplied reservation ids with expiry".

Outage posture ([decisions.md](decisions.md), "Billing outage posture"): `reserve` fails closed and surfaces the 503 to the user; `commit` and `refund` enqueue in an outbox table in the API's PostgreSQL and a background task retries them until acknowledged. The ledger's unique source keys make redelivery a no-op, so settlement is effectively exactly-once.

Realtime sessions meter through the same contract in chunks:

```python
CHUNK_GPU_MS = 60_000

async def on_admit(session):                       # entering Active
    session.chunk_id = uuid4()
    await quota.reserve(session.user, session.chunk_id, CHUNK_GPU_MS, ttl_s=300)

async def on_chunk_exhausted(session):             # measured gpu_ms reaches the chunk
    await quota.commit(session.chunk_id, gpu_ms=CHUNK_GPU_MS, images=0)
    session.chunk_id = uuid4()
    try:
        await quota.reserve(session.user, session.chunk_id, CHUNK_GPU_MS, ttl_s=300)
    except InsufficientCredits:
        await close_session(session, reason="out_of_credits")   # error message, then close

async def on_release(session):                     # idle release, close, worker loss
    await quota.commit(session.chunk_id, gpu_ms=session.chunk_used_ms, images=0)
```

Overdraft exposure is bounded by one chunk; the `credits_tick` message on the browser socket displays the live drain against the same numbers.

## Self-hosted compose shape

```yaml
services:
  api:
    image: ghcr.io/portocolom-studio/potocolom-api:v0.x
    ports: ["8080:8080"]
    environment:
      AUTH_MODE: none            # or local
      DATABASE_URL: postgresql://potocolom:...@postgres/potocolom
      STORAGE_BACKEND: local
      FLEET_TOKEN_KEY: ${FLEET_SECRET}
    volumes: ["assets:/data/assets"]
    depends_on: [postgres]       # migrates automatically on startup

  postgres:
    image: postgres:16
    volumes: ["pgdata:/var/lib/postgresql/data"]

  worker:
    image: ghcr.io/portocolom-studio/potocolom-worker:v0.x-cuda   # or -rocm, see local-development.md
    environment:
      DEVICE: cuda               # cuda | rocm | cpu
      MEMORY_MODE: auto          # auto | full | model_offload | group_offload, see architecture.md
      API_URL: ws://api:8080/api/v1/fleet
      FLEET_TOKEN: ${FLEET_SECRET}
    volumes: ["models:/models"]  # weights + manifests, pulled from Hugging Face
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]
    # AMD variant instead passes /dev/kfd and /dev/dri and joins the video group
```

No Redis, no billing, no safety services: the in-process implementations and the unlimited quota cover their roles.

## Open questions

None at this level. Every fork that changes the shape above has been decided and recorded in [decisions.md](decisions.md). What remains is code-level convention (exact rate limit numbers, cookie names, retry backoff constants), which belongs in the implementation issues and code review, not here.
