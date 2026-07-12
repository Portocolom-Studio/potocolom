# Development entry points. `make verify` runs exactly what CI runs.
# The local stack is three processes in three terminals, in this order:
#   make deps && make api      # terminal 1: PostgreSQL etc., then the API
#   make worker-rocm           # terminal 2 (or worker-sim without a GPU)
#   make web                   # terminal 3: the studio on :5173
# Self-hosted packaging (one compose file for everything) is issue #18.

.PHONY: setup setup-rocm deps deps-down lint test build verify simulate \
	api worker-rocm worker-sim web generate \
	site-build site-deploy worker-deploy

setup: ## create virtualenvs and install all dependencies
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd worker && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

setup-rocm: ## worker inference deps for AMD: ROCm torch wheel, then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch --index-url https://download.pytorch.org/whl/rocm6.3
	cd worker && .venv/bin/pip install -e ".[inference]"

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

# The local M2 stack. Each target runs in the foreground in its own terminal.
PROMPT ?= a castle on a hill at sunset, oil painting

api: ## API server on :8000; assets under ./data (make deps first)
	cd backend && STORAGE_LOCAL_PATH=$(CURDIR)/data \
		.venv/bin/uvicorn app.main:app --port 8000

worker-rocm: ## inference worker on the AMD GPU (make setup-rocm once)
	cd worker && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker

worker-sim: ## simulated worker: no GPU, echo frames, flat images
	cd worker && .venv/bin/python -m worker

web: ## studio dev server; proxies /api/v1 to localhost:8000
	cd frontend && npm run dev

generate: ## one image end to end: make generate PROMPT="..."
	backend/.venv/bin/python scripts/generate.py "$(PROMPT)"

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
