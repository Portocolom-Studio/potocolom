# Development entry points. `make verify` runs exactly what CI runs.

.PHONY: setup deps deps-down lint test build verify simulate site-build site-deploy worker-deploy

setup: ## create virtualenvs and install all dependencies
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd worker && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

deps: ## start development dependencies (PostgreSQL, Redis, MinIO, Mailpit)
	docker compose -f deploy/compose/dev.yml up -d

deps-down:
	docker compose -f deploy/compose/dev.yml down

lint:
	cd backend && .venv/bin/ruff check . ../scripts && .venv/bin/mypy
	cd worker && .venv/bin/ruff check . && .venv/bin/mypy
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

# Site deployment (Cloudflare Pages). Anyone deploying their own copy
# overrides the variables; the waitlist worker lives outside this repo.
WAITLIST_URL ?= /api/waitlist
PAGES_PROJECT ?= potocolom

site-build: ## build the frontend with the waitlist endpoint baked in
	cd frontend && PUBLIC_WAITLIST_URL=$(WAITLIST_URL) npm run build

site-deploy: site-build ## build and deploy the site to Cloudflare Pages
	cd frontend && npx wrangler pages deploy build --project-name $(PAGES_PROJECT)

worker-deploy: ## deploy the waitlist worker (operator only, config in .local)
	@test -d .local/waitlist-worker || { echo "no .local/waitlist-worker on this machine"; exit 1; }
	cd .local/waitlist-worker && npx wrangler deploy
