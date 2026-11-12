---
title: Testing ladder
subtitle: The test levels, from quick checks to the full gate.
tab: contributing
group: Develop
order: 3
source: docs/local-development.md "Testing ladder", "First run on a new machine" (make verify-worker-inference), "What rung 1 covers, and what it deliberately does not" (the frontend gate paragraphs), "Continuous integration"; Makefile targets verify, verify-backend, verify-worker, verify-worker-inference, verify-frontend, verify-compose, verify-guards, verify-mermaid, verify-secrets, verify-backup, lint-workflows, check-python-deps
---

The project checks a change in levels. Each level is slower than the one before it, and each one is closer to the real product. Start with the cheapest level that covers your change. Climb only as far as the change needs.

## The levels

| Level                                    | What it covers                                                                                                                        | Make target or command                                                                | Who runs it                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 1. Unit tests                            | Lint, type checks and unit tests for each component. Backend tests use PostgreSQL. Runs in seconds to minutes.                        | `make verify-backend`, `make verify-worker`, `make verify-frontend`                   | You, and CI on every pull request                      |
| 2. CPU worker tests with a tiny model    | Builds a tiny random model and tests the inference path on the CPU: loading a manifest, dispatch, safety checks and the frame header. | `make verify-worker-inference`                                                        | CI, and you on any machine with the inference packages |
| 3. Compose stack with a simulated worker | The shipped Docker stack runs one generation with a simulated worker. No GPU needed.                                                  | `make verify-compose` (config check only), then `scripts/compose-smoke.sh` (full run) | CI, when deploy files change                           |
| 4. ROCm smoke test                       | The API and a worker on an AMD GPU, with real frames.                                                                                 | `make worker-rocm`                                                                    | The maintainer, before a release                       |
| 5. CUDA smoke test                       | The same check on an NVIDIA GPU.                                                                                                      | `make worker-cuda`                                                                    | The maintainer, by hand, before a release              |

> [!PLANNED]
> A staging level on cloud infrastructure is planned for the cloud launch. It is not part of contributor work.

Levels 1 and 2 cover most changes. CI runs level 3 when you change the backend, the frontend, the deploy files or the smoke script. Levels 4 and 5 need a GPU and are maintainer steps, so a contributor without a GPU runs levels 1 to 3.

Level 2 uses the inference virtual environment at `worker/.venv-inference` when it exists. The worker workflow creates it with the CPU-only PyTorch 2.9.0 wheel. Without that environment, the target uses `worker/.venv`, which needs the inference packages: run `make setup-cuda` or `make setup-rocm` first. Without those packages, the tests fail at import. Read [local development](/docs/contributing/local-development) for the setup.

## The full gate: make verify

`make verify` runs the three component targets in order: backend, worker and frontend. These are the same lines that CI runs, so a green local run matches what CI checks for those components.

```bash
make verify
```

Run `make verify` before you push a branch. Some checks are not part of `make verify`, so run them when your change needs them:

- `make verify-worker-inference` runs the CPU inference tests with a tiny model. Run it when your change touches the worker.
- `make verify-secrets` scans the working tree for committed secrets. Run it when your change adds configuration, scripts or fixtures.
- `make verify-guards` proves that the setup checks still refuse a toolchain that is too old. CI runs it when the Makefile, a dependency file or a workflow changes.
- `make verify-mermaid` renders every diagram under `docs/`. It needs `mmdc` and Chrome.
- `make verify-backup` proves that a self-hosted backup restores the database and the asset files together. It needs the Docker daemon, and it starts and removes Compose projects of its own.

## What the frontend gate checks

`make verify-frontend` runs more than unit tests. It:

1. Runs lint, type checks and unit tests.
2. Builds the landing mode and checks the studio preview, the docs, and the footer's contrast and keyboard focus.
3. Builds the studio only after every landing check passes. It then checks the self-hosted build, studio shell, setup, canvas, sign-in, saved views, owner focus, Models and the first-run tour. The sign-in and setup checks use separate throwaway databases and API processes.

CI uses `make -j3 --output-sync=target verify-frontend` to run up to three checks of one build mode at once. Both builds and the static checks stay in order. Plain `make verify-frontend` runs the same checks one at a time.

The canvas check drives the built studio in headless Chrome. Set `PUPPETEER_EXECUTABLE_PATH` or `CHROME_PATH` if Chrome is not at a standard Linux path. The gate fails when it finds no browser. It does not skip the checks.

To run the canvas check by itself, build the studio first:

```bash
cd frontend
PUBLIC_SITE_MODE= npm run build
npm run test:canvas
```

A landing build left in `frontend/build` makes `/app` show the static preview, and every canvas check then fails. Build the studio before you run the canvas check.

## Continuous integration

Every pull request starts all component workflows, so each check always reports a result. A component job is skipped only when the change selection works and finds no matching file. If the selection fails, the job runs.

A new push to a pull request cancels the runs of the commit that it replaced. This keeps old runs off the runner. A push to `main` is never cancelled.

Each component job runs the matching `make verify-<component>` target. Other jobs run for certain paths:

- A change to the Makefile, a dependency file or a workflow runs `make verify-guards`, `make lint-workflows` and `make setup`. `make setup` tests the new-contributor path from start to finish. `make lint-workflows` checks the workflow files with actionlint and zizmor. The linters are pinned releases that the script downloads and checks against a SHA-256.
- A change to the backend, the frontend, the deploy files, the smoke script or the backup scripts runs `make verify-compose`, then `scripts/compose-smoke.sh` and `scripts/test-backup-restore.sh`.
- A change to the worker runs `make verify-worker-inference`.
- The backend and worker jobs run `make check-python-deps`, which audits the installed Python packages for known vulnerabilities with pip-audit and fails on any finding that the script does not record.
- A change to the backend runs the backend integration tests against a PostgreSQL service container.
- Every pull request and every push to `main` runs a secret scan.
- A change to the docs runs `make verify-mermaid`.

A version tag runs the release workflow. It runs the full gate, real CPU inference, image smoke tests and vulnerability scans before it publishes images. It then runs the real installer on the AMD GPU. See the [release checks](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/release-checklist.md) for the details.

Pull request jobs run on a self-hosted runner. Read [the self-hosted runner](/docs/contributing/self-hosted-runner) to see what that runner needs.

## Test reports

`make verify-backend` and `make verify-worker` write JUnit results and line and branch coverage to `.local/reports/backend/` and `.local/reports/worker/`. CI keeps these files for 14 days, including after a failed run.

Coverage measures the Python code in the test process. Subprocesses and real GPU inference need their own checks. There is no coverage percentage gate. Review the uncovered branches in the code you changed, and add tests for the behavior you changed, including the failure paths.

## Related pages

- [Local development](/docs/contributing/local-development) sets up the tools and runs each part.
- [Security checklist](/docs/contributing/security-checklist) lists the checks that a change touching security must pass.
