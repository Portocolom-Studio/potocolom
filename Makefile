# Development entry points. `make verify` runs exactly what CI runs.
# First time on a machine:
#   make init                  # venvs, GPU extras, secrets, postgres
#   make dev                   # native studio on :5173 (or make selfhost for :8080)
# The local stack is three processes in three terminals, in this order:
#   make deps && make api      # terminal 1: PostgreSQL etc., then the API
#   make worker-cuda           # terminal 2 (worker-rocm on AMD, worker-sim without a GPU)
#   make web                   # terminal 3: the studio on the configured port
# Or: make dev                 # API + frontend + worker in the background (logs under data/dev/)
#     make dev-status          # pid files, ports, workers, model list; use this if the studio is blank
#     WORKER=rocm|cuda|sim|off (detected from the GPU present; set to override)
# Self-hosted GitHub Actions runner (when hosted minutes are exhausted):
#   make ci-runner-install && make ci-runner-service-install && make ci-runner-start
# See docs/self-hosted-runner.md

.PHONY: preflight compose-up compose-down compose-logs \
	setup setup-rocm setup-cuda setup-inference check-python check-node check-worker-venv \
	ensure-venvs ensure-env init dev selfhost \
	deps deps-all deps-down dco-hook verify verify-backend verify-worker \
	verify-frontend verify-compose verify-guards verify-mermaid simulate test-db-clean dev-db \
	api worker-rocm worker-cuda worker-sim web web-landing \
	dev-start dev-stop dev-restart dev-status \
	stack-up stack-down stack-restart cleanup-failed generate \
	benchmark benchmark-publish \
	auth-enable auth-recover auth-reclaim auth-rotate-keys auth-configure auth-collapse \
	auth-clear-factor \
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

preflight: ## check this machine; write deploy/compose/.env when missing
	@bash "$(CURDIR)/scripts/preflight.sh"

auth-recover: ## print a one-use 10-minute recovery link for an administrator (EMAIL=...)
	@test -n "$(EMAIL)" || { echo 'usage: make auth-recover EMAIL=admin@example.com' >&2; exit 1; }
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.recovery "$(EMAIL)"

auth-enable: ## turn on accounts and print the one-use first-admin setup link
	@bash "$(CURDIR)/scripts/auth-enable.sh"

auth-reclaim: ## get back into a locked-out install (CLAIM=1, or EMAIL=someone@example.com)
	@test -n "$(CLAIM)$(EMAIL)" || { echo 'usage: make auth-reclaim CLAIM=1' >&2; \
		echo '   or: make auth-reclaim EMAIL=someone@example.com' >&2; exit 1; }
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.operator reclaim \
		$(if $(CLAIM),--claim,) $(if $(EMAIL),--restore "$(EMAIL)",)

auth-rotate-keys: ## re-encrypt every stored secret under the newest ROOT_KEYS entry (CHECK=1 to report only)
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.operator rotate-keys $(if $(CHECK),--check,)

auth-clear-factor: ## remove one account's second factor when the authenticator and codes are both gone (EMAIL=...)
	@test -n "$(EMAIL)" || { echo 'usage: make auth-clear-factor EMAIL=someone@example.com' >&2; exit 1; }
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.operator clear-factor "$(EMAIL)"

auth-configure: ## what mail and OAuth would do if the API started right now
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.operator configure

auth-collapse: ## turn accounts OFF, destroying every account (CONFIRM="...")
	@cd "$(CURDIR)/backend" && .venv/bin/python -m app.operator collapse --confirm "$(CONFIRM)"

ensure-env: ## write deploy/compose/.env; fill empty FLEET_SECRET / POSTGRES_PASSWORD
	@bash "$(CURDIR)/scripts/ensure-env.sh"

# Self-hosting convenience wrappers. The docker compose commands in the README
# stay canonical: self-hosting requires Docker and nothing else, and make is
# not on every container host. These are for people who already have it, and
# must not become the documented path.
COMPOSE_FILE := $(CURDIR)/deploy/compose/compose.yml
# gpu on NVIDIA, rocm on AMD, smoke (simulated worker) without either. Detected
# the same way scripts/preflight.sh reports it; override with PROFILE=.
# nvidia-smi must actually enumerate a GPU: driver files alone do not prove
# CUDA can run, so the query is the gate, not /proc/driver/nvidia/version.
PROFILE ?= $(shell if [ -e /dev/kfd ]; then echo rocm; \
	elif nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; \
	then echo gpu; else echo smoke; fi)

compose-up: ensure-env ## self-hosted stack up (PROFILE=gpu|rocm|smoke, detected by default)
	docker compose -f "$(COMPOSE_FILE)" --profile "$(PROFILE)" up -d --build

compose-down: ## stop the self-hosted stack; named volumes are left intact
	docker compose -f "$(COMPOSE_FILE)" --profile "$(PROFILE)" down

compose-logs: ## follow the self-hosted stack's logs
	docker compose -f "$(COMPOSE_FILE)" --profile "$(PROFILE)" logs -f

check-python: ## fail fast unless a Python 3.11+ interpreter is on PATH
	@test -n "$(PYTHON)" || { \
		echo 'error: Python 3.11 or newer is required for backend/ and worker/.' >&2; \
		echo 'Install it alongside the system python3 if needed (for example' >&2; \
		echo 'apt install python3.11 python3.11-venv), or set PYTHON=/path/to/python3.11.' >&2; \
		exit 1; }
	@$(PYTHON) $(VENV_OK) 2>/dev/null || { \
		echo 'error: $(PYTHON) is missing or older than Python 3.11.' >&2; exit 1; }
	@# Debian and Ubuntu ship venv separately, so a new enough interpreter can
	@# still fail to create one. Without this the failure arrives as a raw
	@# ensurepip traceback from inside python -m venv, several lines from the
	@# fix, rather than as the package to install.
	@$(PYTHON) -c 'import ensurepip' 2>/dev/null || { \
		echo 'error: $(PYTHON) cannot create virtualenvs: ensurepip is missing.' >&2; \
		echo 'Debian/Ubuntu ship it separately, for example' >&2; \
		echo '  sudo apt install $(shell $(PYTHON) -c "import sys; print(\"python%d.%d-venv\" % sys.version_info[:2])" 2>/dev/null || echo python3-venv)' >&2; \
		exit 1; }
	@$(PYTHON) -c 'import sys; print("venvs use %s (%d.%d.%d)" \
		% ((sys.executable,) + sys.version_info[:3]))'

# Node 24 is frontend/package.json engines plus frontend/.npmrc engine-strict.
check-node: ## fail fast unless Node.js 24+ is on PATH
	@command -v node >/dev/null 2>&1 || { \
		echo 'error: Node.js 24 or newer is required for frontend/.' >&2; \
		echo 'Install it from https://nodejs.org/ or your package manager.' >&2; \
		exit 1; }
	@node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 24 ? 1 : 0)' || { \
		echo 'error: Node.js 24 or newer is required (found '"$$(node --version)"').' >&2; \
		exit 1; }

check-worker-venv:
	@worker/.venv/bin/python $(VENV_OK) 2>/dev/null && test -x worker/.venv/bin/pip || { \
		echo 'error: worker/.venv is missing, not Python 3.11+, or has no pip; run make setup.' >&2; \
		exit 1; }

# A leftover .venv can have a python symlink and still lack pip (Debian venv
# without python3.x-venv, or an interrupted first run). python -m pip is not
# the check: that can hit the system pip through the symlink and skip recreate.
# VENV_ROOT is for verify-guards; make setup leaves it at this checkout.
VENV_ROOT ?= $(CURDIR)
ensure-venvs: check-python ## create backend/ and worker/ venvs, recreating pip-less ones
	@set -e; \
	venv_pkg="$$($(PYTHON) -c 'import sys; print("python%d.%d-venv" % sys.version_info[:2])')"; \
	for d in backend worker; do \
		venv="$(VENV_ROOT)/$$d/.venv"; \
		if "$$venv/bin/python" $(VENV_OK) 2>/dev/null && test -x "$$venv/bin/pip"; then \
			continue; \
		fi; \
		$(PYTHON) -m venv --clear "$$venv"; \
		if ! test -x "$$venv/bin/pip"; then \
			echo "error: $$venv was created without pip." >&2; \
			echo "Debian/Ubuntu: sudo apt install $$venv_pkg" >&2; \
			exit 1; \
		fi; \
	done

setup: check-node ensure-venvs ## create virtualenvs and install all dependencies
	cd backend && .venv/bin/pip install -qU pip && .venv/bin/pip install -e ".[dev]"
	cd worker && .venv/bin/pip install -qU pip && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install
	@if [ "$(PROFILE)" = gpu ]; then \
		echo 'Next: make setup-cuda  # NVIDIA inference wheels; skip if you only need make verify'; \
	elif [ "$(PROFILE)" = rocm ]; then \
		echo 'Next: make setup-rocm  # AMD inference wheels; skip if you only need make verify'; \
	else \
		echo 'No GPU detected: make dev-start will use the simulated worker'; \
	fi

setup-rocm: check-worker-venv ## worker inference deps for AMD: ROCm torch wheels, then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.3
	cd worker && .venv/bin/pip install -e ".[inference]"

setup-cuda: check-worker-venv ## worker inference deps for NVIDIA: CUDA torch wheels (PyPI default), then the extra
	cd worker && .venv/bin/pip install --upgrade pip
	cd worker && .venv/bin/pip install torch torchvision
	cd worker && .venv/bin/pip install -e ".[inference]"

# Same GPU detection as PROFILE / scripts/dev-stack.sh. Does not install torch
# when there is no GPU: make verify does not need it.
setup-inference: check-worker-venv
	@if [ "$(PROFILE)" = gpu ]; then $(MAKE) setup-cuda; \
	elif [ "$(PROFILE)" = rocm ]; then $(MAKE) setup-rocm; \
	else echo 'No GPU: skipping inference extras (make dev uses the simulated worker)'; fi

# Sequential: setup-inference needs the venvs from setup; deps needs Docker.
# Not a dependency list, so `make -j init` cannot race those.
init: ## first-time contributor: venvs, GPU extras, secrets, postgres
	@$(MAKE) ensure-env
	@$(MAKE) setup
	@$(MAKE) setup-inference
	@$(MAKE) deps
	@echo
	@echo 'Initialized. Native studio:  make dev'
	@echo '            Docker product:  make selfhost'
	@echo '            CI checks:       make verify'

selfhost: ## Docker product on :8080 (preflight + compose-up)
	@$(MAKE) preflight
	@$(MAKE) compose-up
	@echo 'Studio: http://localhost:8080'

# Isolated from deploy/compose/compose.yml (same directory would otherwise
# share project "compose" and a postgres with a different password).
DEV_POSTGRES ?= potocolom-dev-postgres-1

deps: ## start development dependencies (PostgreSQL: all the native dev loop uses)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx compose-postgres-1; then \
		echo 'stopping leftover self-hosted postgres on :5432'; \
		docker compose -f "$(COMPOSE_FILE)" down; \
	fi
	@if ! docker compose -f deploy/compose/dev.yml up -d; then \
		echo 'error: could not start potocolom-dev postgres.' >&2; \
		echo '  port 5432 in use? stop whatever holds it, or:' >&2; \
		echo '  DEV_POSTGRES_PORT=5433 DATABASE_URL=postgresql://potocolom:potocolom@localhost:5433/potocolom make deps' >&2; \
		exit 1; \
	fi
	@i=0; \
	while [ $$i -lt 20 ]; do \
		if docker exec $(DEV_POSTGRES) pg_isready -U potocolom >/dev/null 2>&1; then \
			exit 0; \
		fi; \
		sleep 0.5; \
		i=$$((i+1)); \
	done; \
	echo 'error: $(DEV_POSTGRES) started but is not ready' >&2; \
	exit 1

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
	@docker exec $(DEV_POSTGRES) psql -U potocolom -d postgres -tAc \
		"SELECT datname FROM pg_database WHERE datname ~ '^potocolom_test_[0-9a-f]{8}(_[0-9]+_[0-9a-f]+)?$$' OR datname ~ '^potocolom_[0-9a-f]{8}$$'" \
		| xargs -I{} docker exec $(DEV_POSTGRES) psql -U potocolom -d postgres \
			-c 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'

dev-db: ## create this checkout's dev database; no-op on the main worktree (make deps first)
	@if [ -n "$(DEV_DB_SUPPLIED)" ]; then \
		echo 'DATABASE_URL is set; this worktree uses that database, not its own'; \
	elif [ -z "$(DB_SUFFIX)" ]; then \
		: ; \
	else \
		i=0; \
		while [ $$i -lt 10 ]; do \
			if docker exec $(DEV_POSTGRES) psql -U potocolom -d postgres -tAc \
				"SELECT 1 FROM pg_database WHERE datname = 'potocolom$(DB_SUFFIX)'" | grep -q 1; then \
				echo "dev database potocolom$(DB_SUFFIX) already exists"; \
				exit 0; \
			fi; \
			if docker exec $(DEV_POSTGRES) psql -U potocolom -d postgres \
				-c "CREATE DATABASE \"potocolom$(DB_SUFFIX)\""; then \
				echo "created dev database potocolom$(DB_SUFFIX)"; \
				exit 0; \
			elif docker exec $(DEV_POSTGRES) psql -U potocolom -d postgres -tAc \
				"SELECT 1 FROM pg_database WHERE datname = 'potocolom$(DB_SUFFIX)'" | grep -q 1; then \
				echo "dev database potocolom$(DB_SUFFIX) already exists"; \
				exit 0; \
			fi; \
			sleep 1; \
			i=$$((i+1)); \
		done; \
		echo 'dev database not created; is $(DEV_POSTGRES) running? (make deps)'; \
		exit 1; \
	fi

dco-hook: ## sign off every commit in this clone automatically (CONTRIBUTING.md)
	git config core.hooksPath .githooks
	@echo 'hooks now run from .githooks; git commit adds Signed-off-by for you.'
	@echo 'Undo with: git config --unset core.hooksPath'

verify-guards: ## prove setup refuses a too-old Python and recreates a pip-less venv
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	mkdir -p "$$tmp/bin"; \
	for c in python3 python3.11 python3.12 python3.13; do \
		printf '#!/bin/sh\nexit 1\n' > "$$tmp/bin/$$c"; chmod +x "$$tmp/bin/$$c"; done; \
	if PATH="$$tmp/bin:$$PATH" $(MAKE) --no-print-directory check-python >/dev/null 2>&1; then \
		echo 'error: check-python accepted a PATH with no Python 3.11+ on it.' >&2; \
		exit 1; \
	fi; \
	py=$$(command -v python3); \
	for d in backend worker; do \
		mkdir -p "$$tmp/tree/$$d/.venv/bin"; \
		ln -s "$$py" "$$tmp/tree/$$d/.venv/bin/python"; \
		ln -s "$$py" "$$tmp/tree/$$d/.venv/bin/python3"; \
	done; \
	$(MAKE) --no-print-directory ensure-venvs VENV_ROOT="$$tmp/tree" >/dev/null; \
	for d in backend worker; do \
		if [ ! -x "$$tmp/tree/$$d/.venv/bin/pip" ]; then \
			echo "error: ensure-venvs left $$d/.venv without pip." >&2; \
			exit 1; \
		fi; \
	done; \
	cp "$(CURDIR)/deploy/compose/.env.example" "$$tmp/.env"; \
	ENV_FILE="$$tmp/.env" ENV_EXAMPLE="$(CURDIR)/deploy/compose/.env.example" \
		bash "$(CURDIR)/scripts/ensure-env.sh" >/dev/null; \
	if ! grep -q '^FLEET_SECRET=.\+' "$$tmp/.env"; then \
		echo 'error: ensure-env.sh left FLEET_SECRET empty.' >&2; \
		exit 1; \
	fi; \
	echo 'setup guards ok: no 3.11+ interpreter is refused; pip-less venvs are recreated; empty FLEET_SECRET is filled'

verify-compose: ## validate every compose file and profile (no containers started)
	cd deploy/compose && ENV_FILE=$$([ -f .env ] && echo .env || echo .env.example) && \
	for p in gpu rocm smoke; do \
		docker compose --env-file $$ENV_FILE -f compose.yml --profile $$p config -q || exit 1; \
	done && \
	docker compose --env-file $$ENV_FILE -f dev.yml config -q \
		&& docker compose --env-file $$ENV_FILE -f dev.yml --profile cloud-sim config -q \
		&& docker compose --env-file $$ENV_FILE -f compose.smoke.yml config -q

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
# Supply the derived database only when the developer exported nothing (the
# alternate-port workflow in docs/local-development.md exports their own). Make
# expands an environment value recursively and the recipe shell expands it
# again, so a password containing $ arrives truncated if the value is passed
# through a variable; an inherited one make never touches survives byte-exact.
# Target-specific, never global: verify-backend must not see a dev database,
# because conftest.py takes DATABASE_URL with setdefault and the whole suite
# would then run against it.
DEV_DB_SUPPLIED := $(if $(strip $(value DATABASE_URL)),1,)
ifeq (,$(DEV_DB_SUPPLIED))
api dev-start dev-restart cleanup-failed: export DATABASE_URL := \
	postgresql://potocolom:potocolom@localhost:5432/potocolom$(DB_SUFFIX)
endif
# Empty by default so scripts/dev-stack.sh detects the GPU this machine has.
# Set it explicitly (WORKER=sim, WORKER=off, ...) to override the detection.
WORKER ?=

api: dev-db ## API server on the configured port; assets under ./data (make deps first)
	@set -a; \
	if [ -f "$(CURDIR)/deploy/compose/.env" ]; then . "$(CURDIR)/deploy/compose/.env"; fi; \
	set +a; \
	cd backend && STORAGE_LOCAL_PATH=$(CURDIR)/data \
		ALLOWED_ORIGINS=http://localhost:$(WEB_PORT) \
		PUBLIC_URL=http://localhost:$(API_PORT) \
		FLEET_TOKEN_KEY="$${FLEET_TOKEN_KEY:-$$FLEET_SECRET}" \
		BENCHMARK_API=1 TELEMETRY=false .venv/bin/uvicorn app.main:app --port $(API_PORT)

worker-rocm: ## inference worker on the AMD GPU (make setup-rocm once)
	@set -a; \
	if [ -f "$(CURDIR)/deploy/compose/.env" ]; then . "$(CURDIR)/deploy/compose/.env"; fi; \
	set +a; \
	cd worker && MODELS_DIR=models DEVICE=rocm \
		API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		FLEET_TOKEN="$${FLEET_TOKEN:-$$FLEET_SECRET}" \
		env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		.venv/bin/python -m worker

worker-cuda: ## inference worker on an NVIDIA GPU (make setup-cuda once)
	@set -a; \
	if [ -f "$(CURDIR)/deploy/compose/.env" ]; then . "$(CURDIR)/deploy/compose/.env"; fi; \
	set +a; \
	cd worker && MODELS_DIR=models DEVICE=cuda \
		API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		FLEET_TOKEN="$${FLEET_TOKEN:-$$FLEET_SECRET}" \
		env -u HF_HUB_OFFLINE -u TRANSFORMERS_OFFLINE -u HF_DATASETS_OFFLINE \
		.venv/bin/python -m worker

worker-sim: ## simulated worker: no GPU, echo frames, flat images
	@set -a; \
	if [ -f "$(CURDIR)/deploy/compose/.env" ]; then . "$(CURDIR)/deploy/compose/.env"; fi; \
	set +a; \
	cd worker && API_URL=ws://127.0.0.1:$(API_PORT)/api/v1/fleet \
		WORKER_LOCK="$(DEV_DIR)/worker.lock" \
		FLEET_TOKEN="$${FLEET_TOKEN:-$$FLEET_SECRET}" \
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

dev-start: ensure-env dev-db ## start API, frontend, and worker in the background (make deps first)
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		WORKER="$(WORKER)" bash "$(CURDIR)/scripts/dev-stack.sh" start

dev-restart: ensure-env dev-db ## restart background API, frontend, and worker
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		WORKER="$(WORKER)" bash "$(CURDIR)/scripts/dev-stack.sh" restart

dev-status: ## show pid files, ports, local workers, and /api/v1/models
	@DEV_DIR="$(DEV_DIR)" API_PORT="$(API_PORT)" WEB_PORT="$(WEB_PORT)" \
		bash "$(CURDIR)/scripts/dev-stack.sh" status

stack-up: dev-start ## alias for dev-start
stack-down: dev-stop ## alias for dev-stop
stack-restart: dev-restart ## alias for dev-restart

dev: deps ## native studio on :5173 (postgres + API + frontend + worker)
	@$(MAKE) dev-start
	@$(MAKE) --no-print-directory dev-status

# GitHub Actions self-hosted runner (docs/self-hosted-runner.md). Requires Docker
# for the backend workflow postgres service. Uses gh to fetch registration tokens.
CI_RUNNER_DIR ?= $(HOME)/.local/share/potocolom-actions-runner
CI_RUNNERS ?= 4
CI_RUNNER_DIRS = $(CI_RUNNER_DIR) $(foreach n,$(shell seq 2 $(CI_RUNNERS)),$(CI_RUNNER_DIR)-$(n))

ci-runner-install: ## register CI_RUNNERS self-hosted Actions runners (once)
	@i=0; for dir in $(CI_RUNNER_DIRS); do \
		i=$$((i+1)); \
		name="$$(hostname -s)-potocolom"; \
		[ $$i -gt 1 ] && name="$$name-$$i"; \
		RUNNER_INSTALL_DIR="$$dir" RUNNER_NAME="$$name" \
			bash "$(CURDIR)/scripts/install-actions-runner.sh" || exit 1; \
	done
	@echo "Next: make ci-runner-service-install && make ci-runner-start"

ci-runner-service-install: ## install the runners as systemd services (sudo, once)
	@for dir in $(CI_RUNNER_DIRS); do \
		test -f "$$dir/svc.sh" || { echo "run make ci-runner-install first" >&2; exit 1; }; \
		if [ -f "$$dir/.service" ]; then \
			echo "service already installed for $$dir, skipping"; \
			continue; \
		fi; \
		(cd "$$dir" && sudo ./svc.sh install) || exit 1; \
	done

ci-runner-start: ## start the self-hosted CI runners (systemd)
	@for dir in $(CI_RUNNER_DIRS); do \
		test -f "$$dir/.service" || { echo "run make ci-runner-service-install first" >&2; exit 1; }; \
		(cd "$$dir" && sudo ./svc.sh start) || exit 1; \
	done
	@$(MAKE) --no-print-directory ci-runner-status

ci-runner-stop: ## stop the self-hosted CI runners
	@for dir in $(CI_RUNNER_DIRS); do \
		test -f "$$dir/.service" || continue; \
		(cd "$$dir" && sudo ./svc.sh stop) || exit 1; \
	done

ci-runner-restart: ci-runner-stop ci-runner-start ## restart the self-hosted CI runners

ci-runner-status: ## show self-hosted runner service status
	@for dir in $(CI_RUNNER_DIRS); do \
		if [ -f "$$dir/.service" ]; then \
			(cd "$$dir" && sudo ./svc.sh status); \
		else \
			echo "service not installed ($$dir)"; \
		fi; \
	done

cleanup-failed: dev-db ## remove failed generation jobs from the database
	backend/.venv/bin/python scripts/cleanup-failed-jobs.py

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
