---
title: Architecture overview
subtitle: The services, the database and the storage, and how they talk.
tab: architecture
group: System
order: 1
source: docs/architecture.md "Goals", "Components", "Deployment profiles", "Data model"; docs/connection-handling.md "Model store"; deploy/compose/compose.yml
---

potocolom has three deployable parts: a web frontend, an API server and an inference worker. Two more parts hold data: PostgreSQL for records, and a storage adapter for image files. The same three parts run in every deployment. A self-hosted install differs from other deployments only in its settings, not in its code.

## The parts

- **Frontend:** a single page app built with SvelteKit and the static adapter. It runs in the browser. On start it reads `GET /api/v1/config` to learn which sign-in methods exist and which features are on. In a self-hosted install, the API server serves the built files.
- **API server:** a FastAPI app. It serves REST endpoints for sign-in, the model registry, generation jobs, generation history and saved images. It also accepts the realtime drawing socket from the browser. Admin endpoints sit behind an admin role flag.
- **Worker:** a Python process that runs the image models with Hugging Face diffusers and PyTorch. It accepts no inbound connections. It dials out to the API at `/api/v1/fleet` and keeps one connection open for registration, job dispatch, frames and heartbeats. Its `DEVICE` setting selects `cuda` (NVIDIA), `rocm` (AMD) or `cpu`. The default is `cpu`. It downloads and removes model files when the API asks, and it lists only the models it has installed.
- **Storage:** holds the image bytes. A self-hosted install stores them on local disk by default. The storage adapter can also use an S3 compatible service. The account export links to each image and does not copy the bytes.
- **Database:** PostgreSQL holds users, sessions, the model registry, jobs and generation history. It also records the install's sign-in choice, which is `unclaimed` until the setup link is spent, then `none` or `accounts`. Image bytes do not go into the database.

> [!NOTE]
> The backend does not use Redis today. Generation queues and realtime session ownership live in memory inside the API process. Run one API process. A second API process would not see the first one's queues or drawing sessions.

## How a request moves

### Generating an image

1. The browser sends `POST /api/v1/generations`.
2. The API checks the request and writes a job row to PostgreSQL.
3. The API places the job in its in-process queue.
4. The API hands the job to a worker over that worker's fleet connection.
5. The worker reports progress and then sends `job_done`. The browser follows the job's state and progress on `GET /api/v1/generations/{job_id}/events`, a server-sent event stream.
6. The worker uploads the image to the storage target named in the dispatch message. The job then completes in PostgreSQL.

### Browsing saved work

1. The browser calls `GET /api/v1/generations` for its history.
2. The API reads the rows the user owns from PostgreSQL.
3. The browser loads each image from `GET /api/v1/assets/{id}`. The API checks ownership before it returns the bytes.

### Drawing in realtime

A drawing session uses its own socket and does not go through the job queue. The frames travel browser, API, worker and back. See the [realtime loop](/docs/architecture/realtime-loop) page.

## The self-hosted shape

A self-hosted install is one Docker Compose stack on one machine. The file is `deploy/compose/compose.yml`.

```text
Browser  --HTTP and WebSocket-->  api (FastAPI, also serves the frontend)
api      --SQL-->                 postgres
api      --files-->               assets volume (local disk)
worker   --dials out, one connection-->  api at /api/v1/fleet
```

The compose file defines these services:

- `api`: the API server. It also serves the frontend.
- `postgres`: PostgreSQL 16.
- `worker`: the NVIDIA worker. It starts with the `gpu` profile.
- `worker-rocm`: the AMD worker. It starts with the `rocm` profile.

From a source checkout, start the NVIDIA stack with the command from the README:

```bash
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

For install steps, read the [self-hosting guide](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/self-hosting.md) in the repository.

## Where to read next

- [Realtime loop](/docs/architecture/realtime-loop): how a stroke becomes a frame, and how the frame comes back.
- [GPU scheduling](/docs/architecture/gpu-scheduling): how jobs and live sessions share the workers today.
- [Deployment profiles](/docs/architecture/deployment-profiles): the self-hosted profile and version upgrades.

The full design record is in [docs/architecture.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/architecture.md).

> [!PLANNED]
> The managed cloud service is planned. It would run the same three parts with several API replicas, a shared frame bus and a rented GPU worker pool. None of that is available today.
