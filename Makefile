# Development entry points. `make verify` runs exactly what CI runs.

.PHONY: setup deps deps-down lint test build verify simulate

setup: ## create virtualenvs and install all dependencies
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd worker && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

deps: ## start development dependencies (PostgreSQL, Redis, MinIO, Mailpit)
	docker compose -f deploy/compose/dev.yml up -d

deps-down:
	docker compose -f deploy/compose/dev.yml down

lint:
	cd backend && .venv/bin/ruff check . ../scripts
	cd worker && .venv/bin/ruff check .
	cd frontend && npm run lint

test:
	cd backend && .venv/bin/pytest
	cd worker && .venv/bin/pytest
	cd frontend && npm run check

build:
	cd frontend && npm run build

verify: lint test build ## everything CI runs, locally

simulate: ## live connection-handling demo (docs/connection-handling.md)
	backend/.venv/bin/python scripts/simulate.py
