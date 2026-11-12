---
title: Local development
subtitle: Set up the dev loop, run each part, and test your change.
tab: contributing
group: Develop
order: 2
source: docs/local-development.md "First run on a new machine", "Day-to-day development loop", "Running each component", "Trying accounts mode locally", "Capture the landing page"; Makefile targets init, dev, dev-start, dev-status, api, web, web-landing, worktree, worker-sim, worker-rocm, worker-cuda, verify-worker-inference, setup-link, dev-setup-link, auth-enable; CODING_STANDARDS.md "Wait for state across channels"
---

This page sets up a development machine and starts each part of the product. The product has three parts: the API (the backend), the studio (the browser app, also called the frontend), and the worker (the process that runs image models on a GPU, or a simulated one on a machine without a GPU).

You can run the product in two ways. Docker runs the whole stack in containers, which is how a self-hoster runs it. Native processes run each part directly on your machine, with hot reload and a debugger, which is how you change the code. This page covers the native loop. Use one door at a time, because both try to use the same ports.

## Before you start

Install these tools first. `make init` checks for them and stops on a blocking problem.

- Python 3.11 or newer.
- Node.js 24 or newer.
- Docker with Compose v2. Docker runs PostgreSQL for the development loop.
- A GPU driver, if you have a GPU.

The worker has three device targets, set with the `DEVICE` variable:

- `DEVICE=cuda` for NVIDIA GPUs.
- `DEVICE=rocm` for AMD GPUs.
- `DEVICE=cpu` for machines with no GPU. CI uses this target with a tiny model. It works, but it is slow.

On an AMD GPU, do not set `HSA_OVERRIDE_GFX_VERSION`. The PyTorch ROCm wheels ship kernels for the RX 7600 (gfx1102) without it, and on other RDNA3 cards the override forces the wrong instruction set.

## First run on a new machine

### Run the product in Docker

Use this path to try the product as a self-hoster would. The studio opens at http://localhost:8080.

```bash
make selfhost
```

`make selfhost` runs the preflight check and then starts the stack. Preflight checks Docker and the GPU and writes `deploy/compose/.env`. It does not install Python or Node.

The same steps without make:

```bash
scripts/preflight.sh
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
```

Use `--profile rocm` on an AMD GPU. Without a GPU, the stack uses `smoke`, which runs a simulated worker. `PROFILE=gpu|rocm|smoke` overrides the detected profile in make.

### Hack on the code

Use this path to change the code. The studio opens at http://localhost:5173.

```bash
make init        # checks the tools, creates the virtual environments, starts PostgreSQL
make dev         # starts the API, the studio and the worker in the background
make dev-status  # run this if the studio is blank
make verify      # optional: the same lint, test and build checks that CI runs
```

`make init` checks the tools and the GPU driver. Then it creates a virtual environment (`.venv`) for the backend and for the worker, installs their packages, installs the worker's inference packages on a GPU machine, and starts PostgreSQL.

`make dev` runs `make deps` (PostgreSQL), then `make dev-start`, then `make dev-status`.

A `.venv` made on one GPU type does not work on another. Do not copy `.venv` between an AMD machine and an NVIDIA machine. If a `.venv` has Python but no pip, a failed first run left it behind. Run `make setup` again to recreate it, or delete `backend/.venv` and `worker/.venv` and run `make setup`.

## The day-to-day loop

PostgreSQL runs in a container. The three applications run on your machine.

```bash
make deps        # start PostgreSQL in the potocolom-dev Compose project
make dev-start   # start the API, the studio and the worker in the background
make dev-status  # show process IDs, ports, the worker, and the model list
make dev-restart # restart all three
make dev-stop    # stop all three
```

`make deps` uses its own Compose project, so it does not share PostgreSQL with the self-hosted stack.

`make dev-start` picks the worker from the `WORKER` variable: `rocm`, `cuda`, `sim` or `off`. If you do not set it, the script detects your GPU. Use `WORKER=sim` to run the simulated worker, or `WORKER=off` to run no worker.

```bash
make dev-start WORKER=sim
```

To stop the PostgreSQL container, run `make deps-down`.

## Running each component

Run each part in its own terminal when you want to see its output. The make targets set the ports and the environment for you. The commands below them show the same steps by hand.

### Database

`make deps` starts PostgreSQL. `make api` creates this checkout's development database first, with `make dev-db`.

### API

Run `make api`. The API listens on port 8000 in the main checkout. To run it by hand, from `backend/`:

```bash
.venv/bin/uvicorn app.main:app --reload --ws-max-size 2097152
```

Check that it runs: http://localhost:8000/api/v1/health

### Studio

Run `make web`. To run it by hand, from `frontend/`:

```bash
npm install
npm run dev
```

The studio runs at http://localhost:5173 and sends `/api/v1` requests to the API.

`make web-landing` runs the studio dev server in landing mode. `/` shows the marketing page, and `/app` shows the studio preview instead of the studio.

### Worker

Pick the target that matches your machine:

```bash
make worker-sim    # no GPU: echo frames and flat images
make worker-rocm   # AMD GPU, after make setup-rocm
make worker-cuda   # NVIDIA GPU, after make setup-cuda
```

To run the worker by hand, from `worker/`:

```bash
.venv/bin/python -m worker
```

The worker dials the API and retries until one is running. Press Ctrl+C to stop it.

Model weights download into the Hugging Face hub cache, `$HF_HOME/hub` (or `HF_HUB_CACHE`), which defaults to `~/.cache/huggingface/hub`. Set `HF_HOME` to move it. The make worker targets clear `HF_HUB_OFFLINE`, so a first use downloads.

> [!NOTE]
> Each checkout gets its own ports and database. The main checkout uses port 8000 for the API and port 5173 for the studio. A linked git worktree gets different ports, derived from its path. Run `scripts/checkout-ports.sh` to see the ports for your checkout. The derivation applies to the make targets only. If you run `uvicorn`, `npm run dev` or `python -m worker` by hand, you get the main checkout's ports unless you export `API_PORT`, `WEB_PORT` and `DATABASE_URL` yourself.

## Work in a worktree

To work on a second branch without a second install, run `make worktree NAME=x`. It creates `.local/worktrees/x` on a new branch from `origin/main`. The worktree links `backend/.venv`, `worker/.venv` and `frontend/node_modules` from the main checkout, so it needs no install of its own. `make verify-backend` and `make verify-worker` in the worktree test that worktree's code, not the main checkout's.

## Tests for one component

Run `make lint` for only the lint steps of `make verify`: ruff on the backend, `scripts/` and the worker, each from the directory where its paths resolve, then the frontend lint, then shellcheck on every tracked shell script (`make shellcheck`, error severity). Reviewers apply [CODING_STANDARDS.md](https://github.com/Portocolom-Studio/potocolom/blob/main/CODING_STANDARDS.md) to every diff. Its first rule: a test that changes state through one channel, such as a socket, waits for that state before it reads it through another, such as HTTP or the DOM.

Each component has a make target that runs its lint, type check and tests. Run the one that matches your change:

```bash
make verify-backend
make verify-worker
make verify-frontend
```

To run one backend test file, from `backend/`:

```bash
.venv/bin/pytest tests/test_x.py
```

Each test run gets its own database, and the run drops it when it ends. If a run is killed, the database stays behind. `make test-db-clean` removes those leftovers and keeps the development database. If you export `DATABASE_URL` yourself, every run in that shell shares that database, so give it a name of its own.

`make verify-worker-inference` runs the CPU inference tests with a tiny model. It is not part of `make verify`, so run it when your change touches the worker.

Read [the testing ladder](/docs/contributing/testing-ladder) to choose which checks to run for your change. Run `make verify` before you push. It runs all three component targets.

## First-run setup and accounts

`make api` starts the API with `AUTH_MODE` set to `none`, unless `AUTH_MODE` already has a value in your shell or in `deploy/compose/.env`. A new dev database is decided as `none` the first time, so nothing asks you to sign in.

To try the first-run setup page instead, start the API and print its setup link as a pair, on this checkout's database and port:

```bash
make api FIRST_RUN=1     # the database decides the sign-in mode
make dev-setup-link      # prints the one-use link for that database and port
```

Open the link in a browser. The link lasts one hour and works once. Both targets derive the database and port from the checkout, so a linked worktree gets its own pair. `make api` without `FIRST_RUN=1` decides the database as `none`, and `make dev-setup-link` then reports that the install has already chosen. `make setup-link` is for the Compose install and needs `make compose-up`.

`make auth-enable` turns on accounts and prints a one-use link that claims the administrator account. The switch is one way. The database then refuses to start in `none` mode, so use a throwaway database. The only way back is an offline destructive reset, `make auth-collapse`, which destroys every account on the install.

Sign-in uses an email address and a password, with an optional second factor, and Google or GitHub when they are configured. A password must be 10 to 128 characters long, and it is checked against a bundled blocklist. After the administrator account exists, open `/login` in the studio, or call `POST /api/v1/auth/login`, and invite other people. An invitation is accepted at `/join#{token}`.

## Capture the landing page

Start `make web-landing` and use the URL it prints (port 5173 in the main checkout). A plain headless `--screenshot` captures the page before the landing entrance finishes, so this recipe waits for the loader to leave the DOM first. It uses `puppeteer-core`, which `frontend/node_modules` already has:

```bash
cd frontend && node --input-type=module <<'EOF'
import puppeteer from 'puppeteer-core';
const browser = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'], defaultViewport: { width: 1440, height: 900 } });
const page = await browser.newPage();
await page.goto('http://localhost:5173/', { waitUntil: 'load' });
await page.waitForFunction(() => !document.querySelector('.landing-loader')); // the entrance has revealed
await new Promise((r) => setTimeout(r, 1500)); // let the fade finish
await page.screenshot({ path: '../landing.png' });
await browser.close();
EOF
```

The entrance reveals once the assets load, or at 8000 ms at the latest (`LANDING_ENTRANCE_TIMEOUT_MS` in `frontend/src/lib/landing-entrance.ts`). The 30 second default wait in `waitForFunction` covers that deadline.

## Other targets

- `make simulate` runs a live demo of the connection handling.
- `make stress` runs a deterministic socket stress test against its own API.
- `make cleanup-failed` removes failed generation jobs from the development database.
