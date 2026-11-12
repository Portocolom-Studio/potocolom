# potocolom

potocolom is an open source realtime generative image platform. You sketch on a canvas, frames stream to a GPU worker over one WebSocket, and generated frames stream back while you draw. One AGPL codebase runs two ways: self-hosted on your own GPU with docker compose, and as a paid managed cloud. Both run the same three container images: a FastAPI backend, a Python GPU worker and a SvelteKit frontend.

This file is for coding agents. Humans start with [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Everything here also applies to humans.

## What we never compromise on

1. Self-hosting is first class. A self-hosted install is not a demo of the cloud. Every difference between the two is configuration behind a documented seam ([docs/deployment-profiles.md](docs/deployment-profiles.md)). A feature that only works in the cloud needs a decision entry that says why.
2. Realtime is the product. The live loop has a latency bar ([docs/architecture.md](docs/architecture.md) "Capacity and the real time bar"). Do not add work to the frame path without a measurement that shows its cost.
3. Private by default. No public gallery, owner-checked asset URLs, self-serve export and deletion. New user-linked data joins the GDPR export and the account purge in the same change.
4. Open product, closed business. Billing and fleet operations live in a private repository that talks to this one only over HTTP. This repository never imports from it and never learns its internals ([docs/repository-boundary.md](docs/repository-boundary.md)).

## How to use these rules

Treat them as good defaults, not law. A maintainer's instruction overrides them. If a rule fights the task in front of you, say so plainly and get a maintainer's sign-off before you break it.

Prefer the smallest model that makes the correct behaviour unsurprising. YAGNI: no speculative seams, no config for a value that never changes, no work ahead of its issue. Do not keep complexity only because it exists.

[docs/decisions.md](docs/decisions.md) records each design decision with its rejected alternatives. Do not argue a recorded decision again in code or review. Propose a new entry instead.

## Glossary

- you: the agent that reads this file and changes potocolom.
- maintainers: the people who build potocolom. They review and merge.
- user: a person who draws and generates images in the studio.
- operator: a person who runs a self-hosted install.
- worker: a GPU process that loads models and renders frames and jobs. It connects to the API, never the other way.
- session: one realtime drawing connection. It holds a worker slot while it is live.
- job: one queued generation, such as a still, a refine or an upscale.
- slot: one unit of realtime capacity on a worker.
- manifest: the JSON file that describes a model and its parameters. The studio adapts to it.
- profile: a deployment profile, such as self-hosted or cloud. Auth mode `none` or `accounts` is part of it.
- site mode: the build-time switch `PUBLIC_SITE_MODE` between the studio and the marketing site.

## Ways to hurt yourself

1. Filling the GPU. On a dev machine the GPU often drives the display too. A job that fills VRAM can freeze the desktop, and an AMD driver hang needs a reboot. Check headroom first (`rocm-smi --showmeminfo vram` or `nvidia-smi`), cap resolution and batch size, and stop a run that falls back to disk offload.
2. Killing by pattern. Never `pkill -f` or kill a PID that you found by matching a name or a path. Other worktrees, dev stacks and CI runners can share the machine. Use `make dev-stop`, or kill only the PID you started.
3. Testing the wrong checkout. In a git worktree, the linked `backend/.venv` and `worker/.venv` resolve `app` and `worker` to the main checkout, so a bare `pytest` tests main's code. `make verify-backend` and `make verify-worker` set `PYTHONPATH` correctly. For a single file, run `cd backend && PYTHONPATH=$PWD ./.venv/bin/pytest tests/test_x.py`.
4. Touching real data. Never run tests or a dev server against a real install's database, volumes or asset folder. A linked worktree gets its own dev database (`make dev-db`), and the test suite makes its own database per run.
5. Trusting the site mode gate. `PUBLIC_SITE_MODE` hides studio routes in the marketing build. It is never a security boundary: the API enforces access.

## Hit every surface

The most common defect is a change that works on the path you tested and is missing everywhere else. Before you call a change done, walk this list and say which items applied:

- Profiles: self-hosted with auth mode `none`, self-hosted with `accounts`, and cloud.
- Site modes: the studio build and the `PUBLIC_SITE_MODE=landing` build.
- Hardware: CUDA, ROCm, the simulated worker (`make worker-sim`) and the low VRAM memory ladder.
- Wire: REST endpoints in [docs/api.md](docs/api.md), the socket protocol in [docs/connection-handling.md](docs/connection-handling.md). The worker protocol promises N-1 compatibility, so a new field is optional for an older peer.
- Languages: every user-facing string goes into both `frontend/src/lib/i18n/en.json` and `es.json`.
- Reverse states: a way in needs a way out and a way to see it. Pause needs resume, enroll needs revoke, create needs delete.
- Data: a schema change needs an Alembic migration in `backend/migrations/`, and user-linked data joins export and purge.
- Security: apply [docs/security-checklist.md](docs/security-checklist.md) to each affected path and record the evidence.
- Docs: see "Documentation" below.

## Dev loop

- `make setup` installs everything. `make init` also checks the host and starts PostgreSQL. Read [docs/local-development.md](docs/local-development.md) once.
- `make dev` starts the API, the frontend and a worker in the background. `make dev-status` shows ports and pid files, and `make dev-stop` stops them. `make worker-sim` runs without a GPU.
- Ports and the dev database name derive from the checkout path (`scripts/checkout-ports.sh`), so two checkouts can run at once. Read the real ports from `make dev-status`.
- `make worktree NAME=x` creates `.local/worktrees/x` from `origin/main` with the virtual environments and `node_modules` linked. Remove it when you are done.

## Verifying

- Inner loop: run one suite (`make verify-backend`, `make verify-worker`, `make verify-frontend`) or one file. `make lint` runs every linter the way CI does.
- Before you push: `make verify`. It is the full gate and it is slow. CI also runs the other gates listed in [CONTRIBUTING.md](CONTRIBUTING.md).
- Climb the testing ladder only as far as the change needs ([docs/local-development.md](docs/local-development.md) "Testing ladder").
- A logic, data, state or protocol bug gets a failing test first, then the fix. Test at the lowest layer that fails on main. Do not repeat the case in a wrapper test or at each call site.
- Prove a new test guards its fix: disable the fix and watch the test fail.
- Extend before you add. When an existing test covers the behaviour, add a case or an assertion there.
- A purely visual bug gets before/after screenshots as its proof, not a UI test.
- Do not test what types already guarantee, copy text, or trivial wiring.
- Never wait with a fixed sleep. Wait for the state you need ([CODING_STANDARDS.md](CODING_STANDARDS.md) "Wait for state across channels").

## Commits, issues and pull requests

- Open a pull request only when the person you work for asks for one.
- Trunk based: one short-lived branch per issue, one PR into main. One problem per PR.
- Every commit carries a DCO sign-off: `git commit -s` ([CONTRIBUTING.md](CONTRIBUTING.md)).
- No AI attribution: no `Co-Authored-By` lines for agents, no "generated with" footers. The commit hooks and CI reject them.
- No emojis or decorative characters in code, comments, commits, issues or PRs. Use plain ASCII punctuation.
- Issues and PRs use the headers `# Description`, `# Motivation`, `# Functionality`, `# Details`. The PR template has them.
- A PR is ready only when every required check on its head is green. Update a stale test or CI step in the same PR. Never skip, disable or loosen a check to get green.
- UI changes need before/after screenshots in the PR. Motion or timing needs a short recording. Upload them to GitHub; do not commit PR-only assets.

## Documentation

- The authoritative docs: [architecture](docs/architecture.md), [blueprint](docs/blueprint.md), [connection handling](docs/connection-handling.md), [API](docs/api.md), [deployment profiles](docs/deployment-profiles.md), [decisions](docs/decisions.md), [local development](docs/local-development.md), [repository boundary](docs/repository-boundary.md), [security checklist](docs/security-checklist.md) and the [design system](docs/design-system.md). Read the ones a change touches before you change it.
- Docs upkeep is part of done. Before you commit, search `docs/*.md`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md` and `frontend/src/lib/docs/pages/` for text your change makes wrong or incomplete, and fix it in the same PR.
- The public site at `/docs` mirrors repository docs. Each page under `frontend/src/lib/docs/pages/` names its sources in a `source:` front matter line, and `scripts/check-docs-site.sh` fails a PR that changes a source without its page. A change with no reader-facing effect carries a `Docs-site: <reason>` commit trailer that names the page you checked. Never add the trailer only to pass the check.
- When a decision changes, add a new entry to `docs/decisions.md` with its rejected alternatives. Rewrite stale text in place; do not append a second account.
- Mermaid diagrams must render: `make verify-mermaid`.
- Do not commit implementation plans, research notes or agent scratch files. Keep them outside the repository, and track work in the GitHub issue that owns it.

## Where code lives

- `backend/app/` - the FastAPI API: auth, jobs, storage, and the realtime relay and worker placement (`realtime.py`). Migrations in `backend/migrations/`, tests in `backend/tests/`.
- `worker/worker/` - the GPU worker: model loading, inference, the socket client (`client.py`).
- `frontend/src/` - one SvelteKit app (Svelte 5) for the studio, the marketing site and `/docs`. Docs pages are in `frontend/src/lib/docs/pages/`.
- `deploy/compose/`, `deploy/docker/` - the self-hosted stack and the container images.
- `scripts/` - install, backup, benchmark and CI helper scripts.
- `docs/` - the public design and operating docs.

## Taste

- Reviewers apply [CODING_STANDARDS.md](CODING_STANDARDS.md) to every diff. UI work follows [docs/design-system.md](docs/design-system.md).
- Match the style of the file you edit.
- Default to no comment. Write one only for a constraint, an external quirk or an invariant the code cannot show.
- The UI stays dumb. The API enforces every rule the UI shows.
- Users watch frames arrive. They notice a dropped frame, a spinner that lies and a stale label. Avoid animations that repaint all the time.
