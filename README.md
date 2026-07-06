# potocolom
Open-source real-time generative AI platform that enables users to generate, edit, and enhance images through an intuitive interface

## Documentation

- [Architecture](docs/architecture.md)
- [Implementation blueprint](docs/blueprint.md)
- [Local development and testing](docs/local-development.md)
- [Cloud infrastructure](docs/cloud-infrastructure.md)
- [Design decisions](docs/decisions.md)

## Development

The repository is a monorepo: `frontend/` (SvelteKit SPA), `backend/` (FastAPI API server), `worker/` (Python inference worker), `deploy/` (compose files and deployment configuration) and `docs/`. See [Local development and testing](docs/local-development.md) for how to run each component.
