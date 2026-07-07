# potocolom
Open-source real-time generative AI platform that enables users to generate, edit, and enhance images through an intuitive interface

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment profiles and migration](docs/deployment-profiles.md)
- [Implementation blueprint](docs/blueprint.md)
- [API reference and user journeys](docs/api.md)
- [Connection handling](docs/connection-handling.md)
- [Local development and testing](docs/local-development.md)
- [Cloud infrastructure](docs/cloud-infrastructure.md)
- [AWS setup guide](docs/aws-setup.md)
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
