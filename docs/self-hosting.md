# Self-hosting

One machine, one compose file: PostgreSQL, the API (with the built studio
embedded), and a GPU worker. This page covers what the [README quick
start](../README.md#self-hosting) assumes: hardware, GPU passthrough, the
first run, and what persists where.

## Hardware requirements

| Component | Minimum | Comfortable |
|---|---|---|
| GPU VRAM | 6 GB (SD-class models at 512 px) | 12-16 GB (SDXL-class at 1024 px, model switching without eviction) |
| Disk | 20 GB free (weights are 2-7 GB per model, plus your images) | 50 GB+ |
| RAM | 8 GB | 16 GB |

Each model manifest declares its floor in `min_vram_gb`; the shipped set
spans 6 GB (`dreamshaper-lcm`) to 10 GB (`sdxl-base`, `sdxl-fast`). A machine
without a supported GPU can still run the full stack against the simulated
worker (flat colored images, real protocol): `scripts/compose-smoke.sh`.

## GPU passthrough

NVIDIA (default images):

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

## First run

```bash
cp deploy/compose/.env.example deploy/compose/.env
# edit POSTGRES_PASSWORD and FLEET_SECRET
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

- The first generation per model downloads its weights from Hugging Face
  (2-7 GB for the SD and SDXL class models; `sd35-medium` is much larger, see
  "Gated models" below); watch progress with
  `docker compose -f deploy/compose/compose.yml logs -f worker`.
  Until the download finishes, jobs on that model sit in the queue.
- Open http://localhost:8080; the studio is served by the API container.
- The fleet WebSocket (`/api/v1/fleet`) accepts the shared `FLEET_SECRET` from
  the compose environment. The API receives it as `FLEET_TOKEN_KEY` and the
  worker receives it as `FLEET_TOKEN`; the worker sends it in the handshake
  header. Generate it with `openssl rand -hex 32` and keep it private. Hex is
  not arbitrary advice: an HTTP header carries ASCII only, and Compose expands
  `$NAME` inside an unquoted `.env` value, so a secret containing a dollar sign
  can be altered or emptied, and an emptied one puts the API back in permissive
  mode. Compose and the API both warn, but the run continues. Single-quote any
  value containing a dollar sign. The API warns at startup when the secret is
  unset or not ASCII.
- If `FLEET_SECRET` is empty, the API logs a warning and keeps the fleet socket
  permissive for compatibility with existing installs, but only for workers
  whose address cannot route from the internet: loopback, private and
  link-local ranges, which covers the compose network and the rest of your LAN.
  A worker connecting from a public address is refused with the secret unset,
  because `docker compose up` publishes port 8080 on all interfaces and an
  admitted worker is dispatched real prompts and canvas frames. Set
  `FLEET_SECRET` to run a worker from anywhere else.
- That check is a safety net, not a boundary, and it does not cover every path.
  Published IPv4 ports are forwarded by iptables and keep the client's address,
  so a direct IPv4 connection from the internet is refused. A connection
  arriving over IPv6 reaches the IPv4-only container through Docker's userland
  proxy, which opens a fresh connection from the bridge gateway, so the client
  becomes indistinguishable from a worker on the compose network and is
  admitted. Measured on Docker 29.6.1. A reverse proxy has the same effect for
  both families. If the host has a public address of either family, set
  `FLEET_SECRET`; nothing else closes the IPv6 path.
- One combination is unsafe and easy to reach by accident: `FLEET_SECRET` empty
  together with `FORWARDED_ALLOW_IPS=*`. The second tells uvicorn to believe the
  `X-Forwarded-For` header from any client, and the address in that header is
  what the permissive check then sees, so a client can claim to be on your
  network and register as a worker from anywhere. Setting the wildcard is
  ordinary advice when running behind a proxy, so the API warns about the pair
  at startup. Set `FLEET_SECRET` in that setup.
- Models are JSON manifests in the `models` volume, seeded from
  `worker/models/` on first boot. Add or edit manifests in the volume (or
  rebuild the image) and restart the worker; see
  [third-party-models.md](third-party-models.md) for licensing notes.

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
