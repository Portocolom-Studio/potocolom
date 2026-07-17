# Development entry points. `make verify` runs exactly what CI runs.
# The local stack is three processes in three terminals, in this order:
#   make deps && make api      # terminal 1: PostgreSQL etc., then the API
#   make worker-rocm           # terminal 2 (or worker-sim without a GPU)
#   make web                   # terminal 3: the studio on :5173
# Or: make dev-start           # API + frontend + worker in the background (logs under data/dev/)
#     WORKER=sim|off to use the simulated worker or skip it
# Self-hosted GitHub Actions runner (when hosted minutes are exhausted):
#   make ci-runner-install && make ci-runner-service-install && make ci-runner-start
# See docs/self-hosted-runner.md

.PHONY: setup setup-rocm deps deps-down lint test build verify simulate \
	api worker-rocm worker-sim web web-landing dev-start dev-stop dev-restart \
	stack-up stack-down stack-restart cleanup-failed generate \
	benchmark benchmark-publish \
	ci-runner-install ci-runner-service-install ci-runner-start ci-runner-stop \
	ci-runner-restart ci-runner-status \
	site-build site-preview site-deploy worker-deploy

setup: ## create virtualenvs and install all dependencies
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd worker && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

setup-rocm: ## worker inference deps for AMD: ROCm torch wheels, then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.3
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
# Or use dev-start / dev-stop / dev-restart for API + frontend + worker in the background.
PROMPT ?= a castle on a hill at sunset, oil painting
DEV_DIR := $(CURDIR)/data/dev
API_PORT ?= 8000
WEB_PORT ?= 5173
WORKER ?= rocm

api: ## API server on :8000; assets under ./data (make deps first)
	cd backend && STORAGE_LOCAL_PATH=$(CURDIR)/data \
		BENCHMARK_API=1 .venv/bin/uvicorn app.main:app --port 8000

worker-rocm: ## inference worker on the AMD GPU (make setup-rocm once)
	cd worker && MODELS_DIR=models DEVICE=rocm \
		API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		.venv/bin/python -m worker

worker-sim: ## simulated worker: no GPU, echo frames, flat images
	cd worker && API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet .venv/bin/python -m worker

web: ## studio dev server; proxies /api/v1 to localhost:8000
	cd frontend && npm run dev

web-landing: ## dev server in landing mode: /app shows the Cloudflare variant
	cd frontend && PUBLIC_SITE_MODE=landing npm run dev

site-preview: site-build ## serve the exact marketing-site artifact locally
	cd frontend && npm run preview

dev-stop: ## stop background API (:8000), frontend (:5173), and worker
	@if [ -f "$(DEV_DIR)/api.pid" ]; then \
		kill $$(cat "$(DEV_DIR)/api.pid") 2>/dev/null || true; \
	fi
	@if [ -f "$(DEV_DIR)/web.pid" ]; then \
		kill $$(cat "$(DEV_DIR)/web.pid") 2>/dev/null || true; \
	fi
	@if [ -f "$(DEV_DIR)/worker.pid" ]; then \
		kill $$(cat "$(DEV_DIR)/worker.pid") 2>/dev/null || true; \
	fi
	@fuser -k $(API_PORT)/tcp 2>/dev/null || true
	@fuser -k $(WEB_PORT)/tcp 2>/dev/null || true
	@rm -f "$(DEV_DIR)/api.pid" "$(DEV_DIR)/web.pid" "$(DEV_DIR)/worker.pid"

dev-start: ## start API, frontend, and worker in the background (make deps first)
	@if [ "$(WORKER)" != "rocm" ] && [ "$(WORKER)" != "sim" ] && [ "$(WORKER)" != "off" ]; then \
		echo "Unknown WORKER=$(WORKER); use rocm, sim, or off" >&2; exit 1; \
	fi
	@mkdir -p "$(DEV_DIR)"
	@$(MAKE) dev-stop
	@echo "Starting API on :$(API_PORT)..."
	@bash -c 'cd "$(CURDIR)/backend" && STORAGE_LOCAL_PATH="$(CURDIR)/data" \
		nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT) \
		> "$(DEV_DIR)/api.log" 2>&1 & echo $$! > "$(DEV_DIR)/api.pid"'
	@echo "Starting frontend on :$(WEB_PORT)..."
	@bash -c 'cd "$(CURDIR)/frontend" && \
		nohup npm run dev -- --host 127.0.0.1 --port $(WEB_PORT) \
		> "$(DEV_DIR)/web.log" 2>&1 & echo $$! > "$(DEV_DIR)/web.pid"'
	@if [ "$(WORKER)" = "rocm" ]; then \
		echo "Starting worker (rocm, MODELS_DIR=models)..."; \
		bash -c 'cd "$(CURDIR)/worker" && MODELS_DIR=models DEVICE=rocm \
			API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
			nohup .venv/bin/python -m worker \
			> "$(DEV_DIR)/worker.log" 2>&1 & echo $$! > "$(DEV_DIR)/worker.pid"'; \
	elif [ "$(WORKER)" = "sim" ]; then \
		echo "Starting worker (simulated engine)..."; \
		bash -c 'cd "$(CURDIR)/worker" && API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
			nohup .venv/bin/python -m worker \
			> "$(DEV_DIR)/worker.log" 2>&1 & echo $$! > "$(DEV_DIR)/worker.pid"'; \
	fi
	@echo "API log:    $(DEV_DIR)/api.log"
	@echo "Web log:    $(DEV_DIR)/web.log"
	@if [ "$(WORKER)" != "off" ]; then echo "Worker log: $(DEV_DIR)/worker.log"; fi
	@echo "API:        http://localhost:$(API_PORT)"
	@echo "Studio:     http://localhost:$(WEB_PORT)"

dev-restart: dev-stop dev-start ## restart background API, frontend, and worker

stack-up: dev-start ## alias for dev-start
stack-down: dev-stop ## alias for dev-stop
stack-restart: dev-restart ## alias for dev-restart

# GitHub Actions self-hosted runner (docs/self-hosted-runner.md). Requires Docker
# for the backend workflow postgres service. Uses gh to fetch registration tokens.
CI_RUNNER_DIR ?= $(HOME)/.local/share/potocolom-actions-runner

ci-runner-install: ## register the self-hosted Actions runner (once)
	@RUNNER_INSTALL_DIR="$(CI_RUNNER_DIR)" bash "$(CURDIR)/scripts/install-actions-runner.sh"
	@echo "Next: make ci-runner-service-install && make ci-runner-start"

ci-runner-service-install: ## install runner as a systemd service (sudo, once)
	@test -f "$(CI_RUNNER_DIR)/svc.sh" || { echo "run make ci-runner-install first" >&2; exit 1; }
	@cd "$(CI_RUNNER_DIR)" && sudo ./svc.sh install

ci-runner-start: ## start the self-hosted CI runner (systemd)
	@test -f "$(CI_RUNNER_DIR)/svc.sh" || { echo "run make ci-runner-install first" >&2; exit 1; }
	@cd "$(CI_RUNNER_DIR)" && sudo ./svc.sh start
	@cd "$(CI_RUNNER_DIR)" && sudo ./svc.sh status

ci-runner-stop: ## stop the self-hosted CI runner
	@test -f "$(CI_RUNNER_DIR)/svc.sh" || exit 0
	@cd "$(CI_RUNNER_DIR)" && sudo ./svc.sh stop

ci-runner-restart: ci-runner-stop ci-runner-start ## restart the self-hosted CI runner

ci-runner-status: ## show self-hosted runner service status
	@if [ -f "$(CI_RUNNER_DIR)/svc.sh" ]; then \
		cd "$(CI_RUNNER_DIR)" && sudo ./svc.sh status; \
	else \
		echo "runner not installed ($(CI_RUNNER_DIR))"; \
	fi

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

site-build: ## build the frontend in landing mode with the waitlist endpoint baked in
	cd frontend && PUBLIC_WAITLIST_URL=$(WAITLIST_URL) PUBLIC_SITE_MODE=landing npm run build

site-deploy: site-build ## build and deploy the site to Cloudflare Pages
	cd frontend && npx wrangler pages deploy build --project-name $(PAGES_PROJECT)

worker-deploy: ## deploy the waitlist worker (operator only, config in .local)
	@test -d .local/waitlist-worker || { echo "no .local/waitlist-worker on this machine"; exit 1; }
	cd .local/waitlist-worker && npx wrangler deploy
