---
title: Deployment profiles
subtitle: The self-hosted profile, and what changes when you scale it.
tab: architecture
group: System
order: 4
source: docs/deployment-profiles.md "The profile spectrum", "Version migrations within any profile"; docs/connection-handling.md "Connection establishment"; docs/decisions.md "Database migrations: gated step in the cloud, automatic self-hosted", "SPA/API compatibility: N-1 through expand-contract", "Retiring something means removing it, and a dropped column does not come back"; deploy/compose/compose.yml
---

One codebase builds every deployment. A deployment profile is a set of settings that picks how one install behaves. The self-hosted profile is the one that ships today. Each profile uses the same container images, so a change of profile is a change of settings, not of code.

## The self-hosted profile

A self-hosted install is one Docker Compose stack on one machine, with one GPU. It needs no external services and no Redis. Sign-in is optional.

### Services and volumes

The stack is defined in `deploy/compose/compose.yml`.

- `api`: the API server. It also serves the frontend build.
- `postgres`: PostgreSQL 16.
- `worker`: the NVIDIA worker. It starts with the `gpu` profile.
- `worker-rocm`: the AMD worker. It starts with the `rocm` profile.

The named volumes keep state between restarts:

- `pgdata`: the PostgreSQL data.
- `assets`: the stored images, mounted at `/data/assets` in the API container.
- `models`: the model weights, mounted at `/models` in the worker container.
- `hf-cache`: the Hugging Face download cache.

From a source checkout, start the NVIDIA stack with:

```bash
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

The [self-hosting guide](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/self-hosting.md) covers the install script, the host requirements and the AMD stack.

### Settings for this profile

| Setting           | Self-hosted value                                                                                                                                               |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AUTH_MODE`       | `none` (default), which signs in one local administrator automatically. Or `accounts`, which uses passwords, with Google and GitHub as options.                 |
| `STORAGE_BACKEND` | `local`, which stores images in the `assets` volume                                                                                                             |
| `EMAIL_BACKEND`   | `none`                                                                                                                                                          |
| `TELEMETRY`       | `true` by default. Set it to `false` to switch it off.                                                                                                          |
| `LOG_FORMAT`      | `plain`                                                                                                                                                         |
| `FLEET_TOKEN_KEY` | Required. Compose passes it from `FLEET_SECRET`, and `scripts/preflight.sh` writes that secret into `deploy/compose/.env`. The API refuses to start without it. |
| Workers           | One worker service, `worker` or `worker-rocm`, from the same image version as the API                                                                           |

The backend reads no Redis setting and no quota service setting today. The API process keeps its queues and realtime sessions in memory, so run one API process.

## Upgrades and version rules

### One version for the whole install

A release tag produces one set of images. The compose file pulls the API and the worker at the same `POTOCOLOM_VERSION`, so both move together when you change that value. To upgrade an install, rerun the install script of the newer release, as the [README](https://github.com/Portocolom-Studio/potocolom/blob/main/README.md) describes.

### Database migrations run on API startup

The API applies its Alembic migrations when it starts. In a self-hosted install, one process runs the migration and then serves from the same image, so there is no window between the migration and the serving process.

The rule for migrations is that they keep the previous release's code working. Read the rule in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md) under "Database migrations: gated step in the cloud, automatic self-hosted".

> [!WARNING]
> Rolling back the image does not roll back the database. A release that drops a column leaves the older image unable to read that table, and the downgrade step recreates the column empty. The dropped data comes back only from a backup taken before the upgrade. The rule and its case are in "Retiring something means removing it, and a dropped column does not come back" in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md).

### The worker protocol allows one version of difference

Workers talk to the API over a versioned protocol. The API accepts workers on the current protocol version and on the version before it. The current version is protocol 5. The oldest version the API still accepts is protocol 4. A worker older than that is refused. The API sends back `rejected` with `min_supported_version` and closes the socket with code 4002.

Because of this, an API and a worker one release apart keep working during an upgrade. The order still matters. Upgrade the API first, then the worker. Compose takes both from the same image, so an install that follows compose does not hit the gap.

### The frontend can be one release behind

Each API release tolerates the previous release's frontend build. A browser tab that was open during an upgrade keeps working, and the page can offer a reload.

## Scaling a self-hosted install

The shipped compose file runs one API process and one worker service. Adding API processes does not share queues or realtime sessions, because both live in memory in one process. Adding a Redis service does not change this either, because the backend does not read a Redis setting today.

Scaling out is planned, and its design is in [docs/decisions.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md) under "Redis topology: one instance, split-ready namespaces". The scheduling and relay parts are on the [GPU scheduling](/docs/architecture/gpu-scheduling) and [realtime loop](/docs/architecture/realtime-loop) pages.

> [!PLANNED]
> The managed cloud profile is planned. It is not available, and no step on this page applies to it.
