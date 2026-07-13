# Development entry points. `make verify` runs exactly what CI runs.
# The local stack is three processes in three terminals, in this order:
#   make deps && make api      # terminal 1: PostgreSQL etc., then the API
#   make worker-rocm           # terminal 2 (or worker-sim without a GPU)
#   make web                   # terminal 3: the studio on :5173
# Or: make dev-start           # API + frontend in the background (logs under data/dev/)
# Self-hosted packaging (one compose file for everything) is issue #18.

.PHONY: setup setup-rocm deps deps-down lint test build verify simulate \
	api worker-rocm worker-sim web dev-start dev-stop dev-restart cleanup-failed generate \
	benchmark benchmark-publish \
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
# Or use dev-start / dev-stop / dev-restart for API + frontend in the background.
PROMPT ?= a castle on a hill at sunset, oil painting
DEV_DIR := $(CURDIR)/data/dev
API_PORT ?= 8000
WEB_PORT ?= 5173

api: ## API server on :8000; assets under ./data (make deps first)
	cd backend && STORAGE_LOCAL_PATH=$(CURDIR)/data \
		BENCHMARK_API=1 .venv/bin/uvicorn app.main:app --port 8000

worker-rocm: ## inference worker on the AMD GPU (make setup-rocm once)
	cd worker && MODELS_DIR=models DEVICE=rocm .venv/bin/python -m worker

worker-sim: ## simulated worker: no GPU, echo frames, flat images
	cd worker && .venv/bin/python -m worker

web: ## studio dev server; proxies /api/v1 to localhost:8000
	cd frontend && npm run dev

dev-stop: ## stop background API (:8000) and frontend (:5173)
	@if [ -f "$(DEV_DIR)/api.pid" ]; then \
		kill $$(cat "$(DEV_DIR)/api.pid") 2>/dev/null || true; \
	fi
	@if [ -f "$(DEV_DIR)/web.pid" ]; then \
		kill $$(cat "$(DEV_DIR)/web.pid") 2>/dev/null || true; \
	fi
	@fuser -k $(API_PORT)/tcp 2>/dev/null || true
	@fuser -k $(WEB_PORT)/tcp 2>/dev/null || true
	@rm -f "$(DEV_DIR)/api.pid" "$(DEV_DIR)/web.pid"

dev-start: ## start API and frontend in the background (make deps first)
	@mkdir -p "$(DEV_DIR)"
	@$(MAKE) dev-stop
	@echo "Starting API on :$(API_PORT)..."
	@bash -c 'cd "$(CURDIR)/backend" && STORAGE_LOCAL_PATH="$(CURDIR)/data" \
		nohup .venv/bin/uvicorn app.main:app --port $(API_PORT) \
		> "$(DEV_DIR)/api.log" 2>&1 & echo $$! > "$(DEV_DIR)/api.pid"'
	@echo "Starting frontend on :$(WEB_PORT)..."
	@bash -c 'cd "$(CURDIR)/frontend" && \
		nohup npm run dev -- --host 127.0.0.1 --port $(WEB_PORT) \
		> "$(DEV_DIR)/web.log" 2>&1 & echo $$! > "$(DEV_DIR)/web.pid"'
	@echo "API log:  $(DEV_DIR)/api.log"
	@echo "Web log:  $(DEV_DIR)/web.log"
	@echo "API:      http://localhost:$(API_PORT)"
	@echo "Studio:   http://localhost:$(WEB_PORT)"

dev-restart: dev-stop dev-start ## restart background API and frontend

cleanup-failed: ## remove failed generation jobs from the database
	backend/.venv/bin/python scripts/cleanup-failed-jobs.py

generate: ## one image end to end: make generate PROMPT="..."
	backend/.venv/bin/python scripts/generate.py "$(PROMPT)"

# Full run: 24 prompts x 4 models x 5 variants = 480 images (~hours on GPU).
BENCHMARK_DIR ?= $(CURDIR)/data/benchmark
BENCHMARK_STAMP := $(shell date -u +%Y%m%d-%H%M%S)
BENCHMARK_OUT ?= $(BENCHMARK_DIR)/$(BENCHMARK_STAMP)
BENCHMARK_IDS ?=
BENCHMARK_MODELS ?=
BENCHMARK_QUICK ?=
BENCHMARK_CONTINUE ?=
BENCHMARK_FORCE ?=
BENCHMARK_INCLUDE_CAPPED ?=

benchmark: ## multi-model suite: run API with BENCHMARK_API=1 first [BENCHMARK_IDS=1-3]
	backend/.venv/bin/python scripts/benchmark.py \
		--out-dir "$(BENCHMARK_OUT)" \
		$(if $(BENCHMARK_IDS),--ids $(BENCHMARK_IDS),) \
		$(if $(BENCHMARK_MODELS),--models $(BENCHMARK_MODELS),) \
		$(if $(BENCHMARK_QUICK),--quick,) \
		$(if $(BENCHMARK_CONTINUE),--continue-on-error,) \
		$(if $(BENCHMARK_FORCE),--force,) \
		$(if $(BENCHMARK_INCLUDE_CAPPED),--include-capped,)

BENCHMARK_PUBLISH ?= $(BENCHMARK_DIR)/full-run

benchmark-publish: ## minify results.json into frontend static assets
	test -f "$(BENCHMARK_PUBLISH)/results.json"
	python3 -c 'import json, pathlib; src=pathlib.Path("$(BENCHMARK_PUBLISH)/results.json"); dst=pathlib.Path("frontend/static/benchmark/results.json"); dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(json.dumps(json.loads(src.read_text()), separators=(",", ":")))'

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
