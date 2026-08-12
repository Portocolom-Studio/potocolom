# Development entry points. `make verify` runs exactly what CI runs.
# The local stack is three processes in three terminals, in this order:
#   make deps && make api      # terminal 1: PostgreSQL etc., then the API
#   make worker-rocm           # terminal 2 (worker-cuda on NVIDIA, worker-sim without a GPU)
#   make web                   # terminal 3: the studio on the configured port
# Or: make dev-start           # API + frontend + worker in the background (logs under data/dev/)
#     make dev-status          # pid files, ports, workers, model list
#     WORKER=rocm|cuda|sim|off (default rocm; cuda on NVIDIA, sim without a GPU)
# Self-hosted GitHub Actions runner (when hosted minutes are exhausted):
#   make ci-runner-install && make ci-runner-service-install && make ci-runner-start
# See docs/self-hosted-runner.md

.PHONY: setup setup-rocm setup-cuda check-python check-worker-venv \
	deps deps-all deps-down dco-hook verify verify-backend verify-worker \
	verify-frontend verify-compose verify-guards verify-mermaid simulate test-db-clean dev-db \
	api worker-rocm worker-cuda worker-sim web web-landing \
	dev-start dev-stop dev-restart dev-status \
	stack-up stack-down stack-restart cleanup-failed generate \
	benchmark benchmark-publish \
	ci-runner-install ci-runner-service-install ci-runner-start ci-runner-stop \
	ci-runner-restart ci-runner-status \
	site-build site-preview site-deploy worker-deploy

# Interpreter used only to create backend/.venv and worker/.venv. A system
# python3 of 3.10 creates a venv that sends pip backtracking against
# requires-python >=3.11, so take the first candidate that is new enough.
# Project packages install into the venvs only, never system site-packages.
# Override: make setup PYTHON=/path/to/python3.11
VENV_OK = -c 'import sys; sys.exit(sys.version_info < (3, 11))'
PYTHON ?= $(shell for c in python3 python3.13 python3.12 python3.11; do \
	$$c $(VENV_OK) 2>/dev/null && { echo $$c; break; }; done)

check-python: ## fail fast unless a Python 3.11+ interpreter is on PATH
	@test -n "$(PYTHON)" || { \
		echo 'error: Python 3.11 or newer is required for backend/ and worker/.' >&2; \
		echo 'Install it alongside the system python3 if needed (for example' >&2; \
		echo 'apt install python3.11 python3.11-venv), or set PYTHON=/path/to/python3.11.' >&2; \
		exit 1; }
	@$(PYTHON) $(VENV_OK) 2>/dev/null || { \
		echo 'error: $(PYTHON) is missing or older than Python 3.11.' >&2; exit 1; }
	@$(PYTHON) -c 'import sys; print("venvs use %s (%d.%d.%d)" \
		% ((sys.executable,) + sys.version_info[:3]))'

check-worker-venv:
	@worker/.venv/bin/python $(VENV_OK) 2>/dev/null || { \
		echo 'error: worker/.venv is missing or not Python 3.11+; run make setup.' >&2; \
		exit 1; }

setup: check-python ## create virtualenvs and install all dependencies
	@for d in backend worker; do \
		$$d/.venv/bin/python $(VENV_OK) 2>/dev/null \
			|| $(PYTHON) -m venv --clear $$d/.venv; \
	done
	cd backend && .venv/bin/pip install -qU pip && .venv/bin/pip install -e ".[dev]"
	cd worker && .venv/bin/pip install -qU pip && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

setup-rocm: check-worker-venv ## worker inference deps for AMD: ROCm torch wheels, then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.3
	cd worker && .venv/bin/pip install -e ".[inference]"

setup-cuda: check-worker-venv ## worker inference deps for NVIDIA: CUDA torch wheels (PyPI default), then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch torchvision
	cd worker && .venv/bin/pip install -e ".[inference]"

deps: ## start development dependencies (PostgreSQL: all the native dev loop uses)
	docker compose -f deploy/compose/dev.yml up -d

deps-all: ## also start Redis, MinIO and Mailpit (cloud-sim profile; idle in local dev)
	docker compose -f deploy/compose/dev.yml --profile cloud-sim up -d

deps-down:
	docker compose -f deploy/compose/dev.yml down

# One target per component, and the per-component CI workflows run these exact
# targets, so local verify and CI cannot drift. Installing dependencies is the
# caller's job: make setup locally, a fresh venv and npm ci in CI.
verify-backend:
	cd backend && .venv/bin/ruff check . ../scripts && .venv/bin/mypy && .venv/bin/pytest

verify-worker:
	cd worker && .venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest

verify-frontend:
	cd frontend && npm run lint && npm run check && npm test && npm run build

verify: verify-backend verify-worker verify-frontend ## everything CI runs, locally

test-db-clean: ## drop per-checkout databases (test and worktree dev), keep the shared dev one
	@docker exec compose-postgres-1 psql -U potocolom -d postgres -tAc \
		"SELECT datname FROM pg_database WHERE datname ~ '^potocolom_test_[0-9a-f]{8}$$' OR datname ~ '^potocolom_[0-9a-f]{8}$$'" \
		| xargs -r -I{} docker exec compose-postgres-1 psql -U potocolom -d postgres \
			-c 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'

dev-db: ## create this checkout's dev database; no-op on the main worktree (make deps first)
	@if [ -z "$(DB_SUFFIX)" ]; then \
		: ; \
	else \
		i=0; \
		while [ $$i -lt 10 ]; do \
			if docker exec compose-postgres-1 psql -U potocolom -d postgres -tAc \
				"SELECT 1 FROM pg_database WHERE datname = 'potocolom$(DB_SUFFIX)'" | grep -q 1; then \
				echo "dev database potocolom$(DB_SUFFIX) already exists"; \
				exit 0; \
			fi; \
			if docker exec compose-postgres-1 psql -U potocolom -d postgres \
				-c "CREATE DATABASE \"potocolom$(DB_SUFFIX)\""; then \
				echo "created dev database potocolom$(DB_SUFFIX)"; \
				exit 0; \
			elif docker exec compose-postgres-1 psql -U potocolom -d postgres -tAc \
				"SELECT 1 FROM pg_database WHERE datname = 'potocolom$(DB_SUFFIX)'" | grep -q 1; then \
				echo "dev database potocolom$(DB_SUFFIX) already exists"; \
				exit 0; \
			fi; \
			sleep 1; \
			i=$$((i+1)); \
		done; \
		echo 'dev database not created; is compose-postgres-1 running? (make deps)'; \
		exit 1; \
	fi

dco-hook: ## sign off every commit in this clone automatically (CONTRIBUTING.md)
	git config core.hooksPath .githooks
	@echo 'hooks now run from .githooks; git commit adds Signed-off-by for you.'
	@echo 'Undo with: git config --unset core.hooksPath'

verify-guards: ## prove make setup refuses a toolchain without Python 3.11+
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	for c in python3 python3.11 python3.12 python3.13; do \
		printf '#!/bin/sh\nexit 1\n' > "$$tmp/$$c"; chmod +x "$$tmp/$$c"; done; \
	if PATH="$$tmp:$$PATH" $(MAKE) --no-print-directory check-python >/dev/null 2>&1; then \
		echo 'error: check-python accepted a PATH with no Python 3.11+ on it.' >&2; \
		exit 1; \
	fi; \
	echo 'setup guards ok: no 3.11+ interpreter is refused, not silently used'

verify-compose: ## validate every compose file and profile (no containers started)
	cd deploy/compose && test -f .env || cp .env.example .env
	cd deploy/compose && for p in gpu rocm smoke; do \
		docker compose -f compose.yml --profile $$p config -q || exit 1; done
	cd deploy/compose && docker compose -f dev.yml config -q \
		&& docker compose -f dev.yml --profile cloud-sim config -q \
		&& docker compose -f compose.smoke.yml config -q

verify-mermaid: ## render every Mermaid diagram under docs/ (requires mmdc and Chrome)
	python3 scripts/verify-mermaid.py

simulate: ## live connection-handling demo (docs/connection-handling.md)
	backend/.venv/bin/python scripts/simulate.py

# The local M2 stack. Each target runs in the foreground in its own terminal.
# Or use dev-start / dev-stop / dev-restart for API + frontend + worker in the background.
PROMPT ?= a castle on a hill at sunset, oil painting
DEV_DIR := $(CURDIR)/data/dev
# One dev loop per checkout: the main worktree keeps the documented canonical
# ports and database (docs/local-development.md), a linked worktree derives an
# offset from its path. An explicit API_PORT=8000 make api still wins. Fail if
# the derivation printed nothing: empty ports would silently rebuild commands
# on the canonical ones, which is the collision this exists to prevent.
CHECKOUT_PORTS := $(shell scripts/checkout-ports.sh)
ifeq (,$(and $(filter API_PORT=%,$(CHECKOUT_PORTS)),$(filter WEB_PORT=%,$(CHECKOUT_PORTS))))
$(error scripts/checkout-ports.sh printed no API_PORT= or WEB_PORT= line)
endif
# override + $(or ...) lets a non-empty API_PORT or WEB_PORT from the command
# line or environment win while an empty one falls back to the derived value.
# DB_SUFFIX is derived only: an override would break worktree isolation.
override API_PORT := $(or $(strip $(API_PORT)),$(patsubst API_PORT=%,%,$(filter API_PORT=%,$(CHECKOUT_PORTS))))
override WEB_PORT := $(or $(strip $(WEB_PORT)),$(patsubst WEB_PORT=%,%,$(filter WEB_PORT=%,$(CHECKOUT_PORTS))))
override DB_SUFFIX := $(patsubst DB_SUFFIX=%,%,$(filter DB_SUFFIX=%,$(CHECKOUT_PORTS)))
# Resolved once so api, dev-start, dev-restart and cleanup-failed agree, and
# so an exported or command-line DATABASE_URL survives (the alternate-port
# workflow in docs/local-development.md).
DEV_DATABASE_URL := $(or $(strip $(DATABASE_URL)),postgresql://potocolom:potocolom@localhost:5432/potocolom$(DB_SUFFIX))
WORKER ?= rocm

api: dev-db ## API server on the configured port; assets under ./data (make deps first)
	cd backend && STORAGE_LOCAL_PATH=$(CURDIR)/data \
		ALLOWED_ORIGINS=http://localhost:$(WEB_PORT) \
		PUBLIC_URL=http://localhost:$(API_PORT) \
		DATABASE_URL="$(DEV_DATABASE_URL)" \
		BENCHMARK_API=1 TELEMETRY=false .venv/bin/uvicorn app.main:app --port $(API_PORT)

worker-rocm: ## inference worker on the AMD GPU (make setup-rocm once)
	cd worker && MODELS_DIR=models DEVICE=rocm \
		API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		.venv/bin/python -m worker

worker-cuda: ## inference worker on an NVIDIA GPU (make setup-cuda once)
	cd worker && MODELS_DIR=models DEVICE=cuda \
		API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		.venv/bin/python -m worker

worker-sim: ## simulated worker: no GPU, echo frames, flat images
	cd worker && API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		.venv/bin/python -m worker

web: ## studio dev server; proxies /api/v1 to the configured API port
	cd frontend && API_PORT=$(API_PORT) WEB_PORT=$(WEB_PORT) npm run dev

web-landing: ## dev server in landing mode: /app shows the Cloudflare variant
	cd frontend && API_PORT=$(API_PORT) WEB_PORT=$(WEB_PORT) \
		PUBLIC_WAITLIST_URL=$(WAITLIST_URL) PUBLIC_SITE_MODE=landing npm run dev

site-preview: site-build ## serve the exact marketing-site artifact locally
	cd frontend && npm run preview

# Background stack: scripts/dev-stack.sh reaps workers by cwd (they have no
# listen port, so fuser alone cannot stop them) and records real PIDs via exec.
dev-stop: ## stop background API, frontend, and worker
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		bash "$(CURDIR)/scripts/dev-stack.sh" stop

dev-start: dev-db ## start API, frontend, and worker in the background (make deps first)
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		DATABASE_URL="$(DEV_DATABASE_URL)" \
		WORKER="$(WORKER)" bash "$(CURDIR)/scripts/dev-stack.sh" start

dev-restart: dev-db ## restart background API, frontend, and worker
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		DATABASE_URL="$(DEV_DATABASE_URL)" \
		WORKER="$(WORKER)" bash "$(CURDIR)/scripts/dev-stack.sh" restart

dev-status: ## show pid files, ports, local workers, and /api/v1/models
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		bash "$(CURDIR)/scripts/dev-stack.sh" status

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

cleanup-failed: dev-db ## remove failed generation jobs from the database
	DATABASE_URL="$(DEV_DATABASE_URL)" backend/.venv/bin/python scripts/cleanup-failed-jobs.py

generate: ## one image end to end: make generate PROMPT="..."
	backend/.venv/bin/python scripts/generate.py --api http://localhost:$(API_PORT) "$(PROMPT)"

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
BENCHMARK_PROMPTS ?=

benchmark: ## multi-model suite: run API with BENCHMARK_API=1 first [BENCHMARK_IDS=1-3]
	backend/.venv/bin/python scripts/benchmark.py \
		--api http://localhost:$(API_PORT) \
		--out-dir "$(BENCHMARK_OUT)" \
		$(if $(BENCHMARK_PROMPTS),--prompts $(BENCHMARK_PROMPTS),) \
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
