# Self-hosting

One machine, one compose file: PostgreSQL, the API (with the built studio
embedded), and a GPU worker. This page covers what the [README quick
start](../README.md#self-hosting) assumes: hardware, GPU passthrough, the
first run, and what persists where.

## Hardware requirements

| Component | Minimum | Comfortable |
|---|---|---|
| GPU VRAM | 8 GB (`ssd-1b`, `vega-rt`) | 12-16 GB (SDXL-class at 1024 px, model switching without eviction) |
| Disk | 20 GB free (weights are 2-7 GB per model, plus your images) | 50 GB+ |
| RAM | 8 GB | 16 GB |

Each model manifest declares its floor in `min_vram_gb`. The models a
self-hoster can actually select span 8 GB (`ssd-1b`, `ssd-1b-lightning`,
`vega-rt`) to 14 GB (`sd35-medium`), with the SDXL class at 10 GB and the
upscalers at 1-4 GB.

Manifests marked `benchmark_only` (`dreamshaper-lcm`, `sd-turbo`,
`sdxl-hypersd`) are excluded from `GET /api/v1/models` by
`registry.public()` and never appear in the studio, so their lower floors are
not capacity you can plan around. `dreamshaper-lcm` in particular declares
6 GB but is not selectable.

The floor is not a gate. A card below a model's floor still loads it: the
worker measures free VRAM and steps down a memory ladder (full residency ->
model offload -> group offload, `worker/worker/memory_ladder.py`). What you
lose is the `realtime` capability, which only full residency advertises - so
below the floor you get working stills, but not the live draw-and-render loop
the product is built around. `scripts/preflight.sh` prints which shipped
models clear the bar on your card.

Measured on a 4 GB RTX 3050 Laptop (well under every text-to-image floor, so
every model lands on the bottom rung): `ssd-1b-lightning` at 768 px, 8 steps
takes about 7.7 s of GPU time per still, plus a one-off multi-minute weight
download on the first job for that model. Correct output, nowhere near the
500 ms realtime bar - which is what dropping to group offload means in
practice.

A machine without a supported GPU can still run the full stack against the
simulated worker (flat colored images, real protocol):
`scripts/compose-smoke.sh`.

Every command on this page is `docker compose`, because self-hosting requires
Docker and nothing else. `make compose-up`, `make compose-down` and
`make compose-logs` wrap the same commands and detect the profile, for hosts
that already have `make`; they are a shortcut and never a requirement.

## Checking a machine before you start

`scripts/preflight.sh` (or `make preflight`) checks everything on this page
against the machine you are on, names the compose profile it can run, and
prints the fix for anything missing. It starts no containers and installs
nothing. When `deploy/compose/.env` is missing it writes one from the
example with generated hex secrets. Empty `POSTGRES_PASSWORD` / `FLEET_SECRET`
are filled; a non-empty value is left alone. Run it first; everything below
is what it checks. `make selfhost` is preflight plus compose-up.

## GPU passthrough

Both profiles need your user in the `docker` group
(`sudo usermod -aG docker $USER`, then log out and back in), otherwise every
`docker compose` command fails with `permission denied ... /var/run/docker.sock`.

NVIDIA (default images). The worker image is built on CUDA 12.8, so the host
driver must be 525.60.13 or newer. CUDA minor version compatibility carries the
12.8 runtime on any 12.x driver above that floor, so a 535 driver reporting
"CUDA Version: 12.2" in `nvidia-smi` is fine and does not need upgrading; below
525.60.13 the worker fails at import with a CUDA error.

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   and restart the docker daemon.
2. `docker compose -f deploy/compose/compose.yml --profile gpu up -d --build`
   uses the `deploy.resources.reservations.devices` block already in the
   compose file; no further configuration is needed.
3. Verify: `docker compose -f deploy/compose/compose.yml exec worker python -c "import torch; print(torch.cuda.is_available())"`.

AMD (ROCm):

1. Install the ROCm kernel driver on the host so `/dev/kfd` and `/dev/dri`
   exist, and add your user to the `video` (and on some distributions
   `render`) group.
2. `docker compose -f deploy/compose/compose.yml --profile rocm up -d --build`
   builds `deploy/docker/Dockerfile.worker-rocm` and passes the devices
   through; no editing of the compose file is needed.
3. RDNA3 consumer cards (gfx1102 class, RX 7600 XT and similar) are supported
   natively by the torch ROCm 6.3+ wheels the image installs; do not set
   `HSA_OVERRIDE_GFX_VERSION`.
4. Verify: `docker compose -f deploy/compose/compose.yml --profile rocm exec worker-rocm python -c "import torch; print(torch.cuda.is_available())"`.

The `gpu` profile is the NVIDIA worker and the `rocm` profile is the AMD one;
run one or the other, never both, since they share the model volumes.

Checking whether an NVIDIA driver is installed: use `nvidia-smi`, or the
presence of `/proc/driver/nvidia/version`. Do not use `lsmod | grep nvidia`.
On laptops with switchable graphics the discrete GPU sits in `D3cold` with its
modules unloaded until something touches it, so `lsmod` prints nothing on a
perfectly working install. `nvidia-smi` wakes the device.

The one command that proves the whole chain (driver, toolkit, runtime
registration) works before you build anything:

```bash
docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
```

## First run

```bash
scripts/preflight.sh
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
# With make: make selfhost
```

- Preflight writes `deploy/compose/.env` when that file is missing, with
  `openssl rand -hex 32` for `POSTGRES_PASSWORD` and `FLEET_SECRET`. Empty
  values of those keys are filled; a non-empty value is left alone. It
  prints `FLEET_SECRET` once. Copy that value to a worker on another
  machine. Non-empty keys in an existing `.env` are left alone.
- The first generation per model downloads its weights from Hugging Face
  (2-7 GB for the SD and SDXL class models; `sd35-medium` is much larger, see
  "Gated models" below); watch progress with
  `docker compose -f deploy/compose/compose.yml logs -f worker`.
  Until the download finishes, jobs on that model sit in the queue.
- Open http://localhost:8080; the studio is served by the API container.
- The fleet WebSocket (`/api/v1/fleet`) accepts the shared `FLEET_SECRET` from
  the compose environment. The API receives it as `FLEET_TOKEN_KEY` and the
  worker receives it as `FLEET_TOKEN`; the worker sends it in the handshake
  header. Hex is mandatory: an HTTP header carries ASCII only, and Compose
  expands `$NAME` inside an unquoted `.env` value.
- An unset `FLEET_TOKEN_KEY` refuses fleet handshakes and refuses to start.
  The error names `scripts/preflight.sh`. An existing install that still has
  an empty `FLEET_SECRET` will fail to start until that file is filled.
- Origin check stays. Signed cloud tokens remain issue #225.
- Models are JSON manifests in the `models` volume, seeded from
  `worker/models/` on first boot. Add or edit manifests in the volume (or
  rebuild the image) and restart the worker; see
  [third-party-models.md](third-party-models.md) for licensing notes.

## Upgrading

The worker protocol's N-1 promise is API-at-or-ahead of its workers. Upgrade
the API first, then the worker. Extra hello fields from a worker one version
ahead of its API are dropped, so narrowing fields such as
`studio_capabilities` fail open. From protocol 4, that mismatch also leaves
realtime unready: a protocol 4 worker ignores `open_session` with no
`control_generation`, so the session never becomes ready (close 4003 after
the ready timeout). Jobs still run. Compose brings both from one image, so
operators who follow compose do not hit this.

## Accounts

There are none yet. `AUTH_MODE` defaults to `none`, every request resolves to a single local
user, and that user holds the `admin` role, so anyone who can reach the API can do anything
the install can do. Keep it on a trusted network, and see the fleet secret above for the
worker side of the same question.

`AUTH_MODE=accounts` is the account mode. Turn it on with `make auth-enable`, which
generates the root key ring, asks where browsers reach this install, records the switch
in PostgreSQL, and prints a one-use call that claims the administrator account. The
claimant adopts the implicit local user, so every job and asset made before accounts
existed belongs to them; nothing is stranded and nothing is copied.

Enabling accounts is one way. An install that has enabled them refuses to start in
`none` mode again, because falling back would return a multi-user install to answering
every request as an administrator. Undoing it needs an offline destructive reset.

The link lasts one hour, is good once, and is replaced if you run `make auth-enable`
again. Nothing durable holds it, only its hash, so a lost link means minting another.

Sign-in is here: address and password, an optional second factor, and Google or
GitHub when you configure them. A session is a cookie the browser never reads,
and an administrator invites everybody else.

Once accounts are on, adding people needs no mail service. An administrator
creates an invitation for an address and a role, and the API returns the link
once. Copy it and hand it over however you like. It is bound to that address,
good once, and expires after 72 hours. Asking to see the link again mints a
fresh one and retires the previous, so a link that never reached its recipient
cannot be used by whoever did see it.

Promoting someone to administrator asks for evidence. The caller must have
signed in within the last 30 minutes, and the account being promoted must
either hold a verified address or be promoted under an explicit attestation
that is recorded against it. Without mail there is nothing else that can say
who an address belongs to, so the record names the administrator who vouched.
Nobody can change their own role, and the last administrator cannot be demoted.

Multi-user is the intended shape, not a maybe: an operator holding `admin`, invited people
signing in with an email and password or with Google or GitHub, and a read-only `viewer`
tier for someone who should see the gallery without spending the GPU. That is recorded in
[decisions.md](decisions.md) under "Self-hosted installs are multi-user" and tracked in issues
#5 and #9. Until those land, treat the install as single-operator.

## Gated models

`sd35-medium` (Stable Diffusion 3.5 Medium) is a gated Hugging Face
repository. Without credentials the worker still advertises the model, and
the first job against it fails when the download is refused.

1. Accept the license at
   [huggingface.co/stabilityai/stable-diffusion-3.5-medium](https://huggingface.co/stabilityai/stable-diffusion-3.5-medium)
   on the account that will download the weights.
2. Create a read token and put it in `deploy/compose/.env` as `HF_TOKEN`.
   The compose file passes it to the worker only, and the Hugging Face client
   reads it from the environment; no other configuration is needed.
3. Recreate the worker so it picks the variable up.

Budget for it: roughly 15 GB of weights, well beyond the 2-7 GB the other
models need, and the download is slow on a domestic connection.

The bundled profile runs PostgreSQL 16. PostgreSQL 13 or newer is required if
you point `DATABASE_URL` at an existing server. The API checks the server version
before running migrations and starts in degraded mode with a clear warning when
the server is too old.

## TLS and HSTS

The API emits `Strict-Transport-Security: max-age=31536000` on every HTTP
response (aligned with Cloudflare Pages via `frontend/static/_headers`). HSTS
is honored by browsers **only over HTTPS**; it does not itself provide TLS or
upgrade an initial plain-HTTP connection.

- The shipped localhost/LAN compose profile remains plain HTTP for development
  (`http://localhost:8080`). That is intentional.
- Any public deployment must terminate TLS in front of the API (reverse proxy,
  load balancer, or similar) and redirect HTTP to HTTPS before exposing the
  service. Without that, clients never see a secure context and HSTS has no
  effect.
- HSTS is emitted without `includeSubDomains` or `preload` so self-hosted
  operators can serve unrelated subdomains from the same host without pinning
  them to HTTPS via this header.
- The SPA CSP allows `img-src http:` (and `https:`, kept explicitly for
  readability) so S3-compatible stores such as MinIO
  (`http://localhost:9100` in the cloud-sim profile) can serve presigned
  images cross-origin. CSP permits the source; browser mixed-content
  processing still applies, so secure pages may upgrade or block insecure
  image requests.

## What persists where

| Volume | Contents | Losing it means |
|---|---|---|
| `pgdata` | users, jobs (prompts, params, seeds), asset records | history and gallery are gone |
| `assets` | generated PNG masters and WebP thumbnails | images are gone; rows point at nothing |
| `models` | model manifests (JSON) | re-seeded from the image on next boot |
| `hf-cache` | downloaded model weights | re-downloaded on next use (2-7 GB per model) |

Back up `pgdata` and `assets` together: jobs and asset rows reference files
by storage key, so restoring one without the other leaves dangling
references. `hf-cache` and `models` are reproducible.

The API deletes what a failed or retried job left behind, and a delete it
cannot make is recorded and retried every five minutes rather than logged and
forgotten. If those retries keep failing, the cause is usually the `assets`
volume: a full disk, or permissions the container cannot write through. The log
line to look for names the key and says `still cannot delete`, and the
`pending_deletes` table is the list of what is waiting.

## Logs

Every service in `deploy/compose/compose.yml` uses Docker's `json-file`
logging driver with five 10 MB files per container. The files live in Docker's
data directory on the host, survive container restarts, and are removed when
the container is removed. Read them with:

```bash
docker compose -f deploy/compose/compose.yml logs
docker compose -f deploy/compose/compose.yml logs -f api worker
```

To change the approximately 50 MB per-container limit, edit `max-size` or
`max-file` in the `x-logging` block and recreate the services. Job state,
`jobs.failure_reason`, phase timings, GPU sample history, and usage events are
operational records in PostgreSQL; container logs retain the remaining process
detail such as startup, protocol, driver, and traceback messages.

The API sends the anonymous daily aggregate documented in
[metrics.md](metrics.md) by default. Set `TELEMETRY=false` in
`deploy/compose/.env` and recreate the API service to disable it. The exact
next payload is available from `GET /api/v1/telemetry/preview`.

## Updating

```bash
git pull
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
# AMD: --profile rocm, the same profile you first started with
```

Database migrations run automatically at API startup (docs/decisions.md,
migrations on startup). Volumes are untouched by rebuilds.
