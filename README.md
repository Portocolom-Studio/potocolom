# potocolom

Draw it. Watch it render.

potocolom is an open source, real time generative image platform: you sketch on a canvas and a diffusion model paints over your strokes live, at 2 to 4 frames per second, then one click hands the result to a heavier model for the final image. One AGPL codebase runs it both ways - self-hosted on your own GPU for free, or as a managed cloud with subscriptions.

![Draw it. Watch it render.](frontend/static/og.png)

## Status

Pre-alpha, under active development in the open. The architecture, protocols and economics are fully documented below; the walking skeleton (a real generation, end to end, on a self-hosted install) is the current milestone, and v0.1 tags when it passes. The cloud service opens later as an invite-only beta - the waitlist lives at [potocolom.leonfuller.com](https://potocolom.leonfuller.com).

## What makes it different

- A live loop, not a prompt queue: canvas frames stream to a GPU worker over one WebSocket and generated frames stream back while you draw.
- One codebase, two modes: the self-hosted install and the paid cloud run the same three container images; every difference is configuration behind documented seams.
- Self-hosting is a first-class citizen: docker compose, one machine, NVIDIA (CUDA) or AMD (ROCm), no account, no telemetry you cannot see and switch off.
- Models without releases: drop a model manifest and the interface adapts to its parameters.
- Private by default: no public gallery, signed URLs, self-serve GDPR export and deletion.

## Self-hosting

```bash
cp deploy/compose/.env.example deploy/compose/.env
# edit POSTGRES_PASSWORD
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

Open http://localhost:8080. Hardware requirements, NVIDIA and AMD GPU passthrough, first-run notes and what persists in which volume are covered in [docs/self-hosting.md](docs/self-hosting.md). The fleet WebSocket (`/api/v1/fleet`) is unauthenticated in this profile - treat the host as a trusted LAN until fleet auth is implemented. Validate the stack without a GPU: `scripts/compose-smoke.sh` (uses port 18080 by default; override with `COMPOSE_SMOKE_PORT`).

## Documentation

The design is documentation-first: every load-bearing decision is recorded with its rejected alternatives before the code lands.

- [Architecture](docs/architecture.md)
- [Deployment profiles and migration](docs/deployment-profiles.md)
- [Implementation blueprint](docs/blueprint.md)
- [API reference and user journeys](docs/api.md)
- [Connection handling](docs/connection-handling.md)
- [Local development and testing](docs/local-development.md)
- [Cloud infrastructure](docs/cloud-infrastructure.md)
- [AWS setup guide](docs/aws-setup.md)
- [Cloud delivery and access model](docs/cloud-delivery.md)
- [Repository boundary, licensing and delivery pipeline](docs/repository-boundary.md)
- [Usage metrics and telemetry](docs/metrics.md)
- [Design decisions](docs/decisions.md)

Editable diagram sources (draw.io, with AWS service icons) live in [docs/diagrams/](docs/diagrams/).

## Development

The repository is a monorepo: `frontend/` (SvelteKit SPA), `backend/` (FastAPI API server), `worker/` (Python inference worker), `deploy/` (compose files and deployment configuration) and `docs/`.

### Prerequisites

- Docker with Compose v2, for the development dependencies.
- Python 3.11 or newer, for the backend and the worker.
- Node.js 24 or newer, for the frontend.
- A GPU is optional until inference lands (issue #15). Both NVIDIA (CUDA) and AMD Radeon (ROCm) are supported worker targets; machines without a supported GPU run the worker on CPU. Machine specific setup, including AMD desktops, is documented in [Local development and testing](docs/local-development.md).

### Common tasks

```
make setup      # create virtualenvs, install all dependencies
make deps       # start PostgreSQL, Redis, MinIO and Mailpit
make verify     # lint, test and build all components: exactly what CI runs
make simulate   # live connection handling demo (API + workers + simulated browser)
```

See [Local development and testing](docs/local-development.md) for running each component individually.

## License

AGPL-3.0. The full product in this repository is self-hostable forever: self-hosting, private use, internal use and contribution are all permitted at no cost under the license's ordinary terms. Anyone who modifies the platform and distributes it, or operates it as a network service, must make their modified source available under the AGPL - or obtain a commercial license, see [COMMERCIAL.md](COMMERCIAL.md). The commercial cloud's billing and fleet orchestration live in a separate private repository behind documented HTTP contracts, and the cloud runs the same unmodified images published here. The reasoning is in [the repository boundary document](docs/repository-boundary.md).

Contributions require a `Signed-off-by` line ([DCO](https://developercertificate.org/)) - see [CONTRIBUTING.md](CONTRIBUTING.md).
