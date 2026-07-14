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

1. In `deploy/compose/compose.yml`, point the worker's `dockerfile` at
   `deploy/docker/Dockerfile.worker-rocm`, set `DEVICE: rocm`, and replace the
   NVIDIA device reservation with:

   ```yaml
   devices:
     - /dev/kfd
     - /dev/dri
   group_add:
     - video
   ```

2. RDNA3 consumer cards (gfx1102 class, RX 7600 XT and similar) are supported
   natively by the torch ROCm 6.3+ wheels the image installs; do not set
   `HSA_OVERRIDE_GFX_VERSION`.

## First run

```bash
cp deploy/compose/.env.example deploy/compose/.env
# edit POSTGRES_PASSWORD
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

- The first generation per model downloads its weights from Hugging Face
  (2-7 GB); watch progress with `docker compose -f deploy/compose/compose.yml logs -f worker`.
  Until the download finishes, jobs on that model sit in the queue.
- Open http://localhost:8080; the studio is served by the API container.
- The fleet WebSocket (`/api/v1/fleet`) is unauthenticated in this profile:
  treat the host as a trusted LAN until fleet authentication lands.
- Models are JSON manifests in the `models` volume, seeded from
  `worker/models/` on first boot. Add or edit manifests in the volume (or
  rebuild the image) and restart the worker; see
  [third-party-models.md](third-party-models.md) for licensing notes.

## What persists where

| Volume | Contents | Losing it means |
|---|---|---|
| `pgdata` | users, jobs (prompts, params, seeds), asset records | history and gallery are gone |
| `assets` | generated images and thumbnails (WebP) | images are gone; rows point at nothing |
| `models` | model manifests (JSON) | re-seeded from the image on next boot |
| `hf-cache` | downloaded model weights | re-downloaded on next use (2-7 GB per model) |

Back up `pgdata` and `assets` together: jobs and asset rows reference files
by storage key, so restoring one without the other leaves dangling
references. `hf-cache` and `models` are reproducible.

## Updating

```bash
git pull
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

Database migrations run automatically at API startup (docs/decisions.md,
migrations on startup). Volumes are untouched by rebuilds.
