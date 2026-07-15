# Self-hosted GitHub Actions runner

Run pull request CI on the reference development desktop when GitHub-hosted minutes are exhausted (2,000/month on the Free plan). Hosted minutes reset at the start of each billing cycle; this runner costs nothing extra.

## What it runs

The four path-filtered workflows in `.github/workflows/`:

| Workflow | Needs on the host |
|---|---|
| backend | Docker (postgres service container), Python 3.11 |
| worker | Python 3.11 |
| frontend | Node 24 |
| simulation | Python 3.11 |

GPU inference is not in CI. `make verify` locally matches what these jobs run.

## One-time setup

### 1. Host prerequisites

On the machine that will run jobs (this desktop):

```bash
# Docker for the backend postgres service container
docker --version

# Optional but faster: pre-install what setup-* actions would fetch
python3.11 -m venv /dev/null 2>/dev/null || sudo apt install python3.11 python3.11-venv
node --version  # setup-node installs 24 if missing
```

Keep Docker running (`systemctl enable --now docker`).

### 2. Register and start the runner

```bash
make ci-runner-install
make ci-runner-service-install   # once, sudo
make ci-runner-start
make ci-runner-status            # should show active (running)
```

Day to day: `make ci-runner-start` before opening PRs, `make ci-runner-stop` when done (optional).

Manual install (same as the Makefile targets):

**Recommended** - script fetches a fresh token via `gh` (repo admin):

```bash
./scripts/install-actions-runner.sh
```

Or paste a token from **Settings → Actions → Runners → New self-hosted runner** (repo page, not org). Use the `./config.sh --token` value from step 2 - **not** a personal access token. Tokens expire in one hour and are single-use.

```bash
RUNNER_TOKEN='<registration-token>' ./scripts/install-actions-runner.sh
```

Fresh token via CLI:

```bash
RUNNER_TOKEN="$(gh api repos/Portocolom-Studio/potocolom/actions/runners/registration-token -X POST --jq .token)" \
  ./scripts/install-actions-runner.sh
```

If you use an **organization** runner token (org admin), set the URL to match:

```bash
RUNNER_URL='https://github.com/Portocolom-Studio' \
  RUNNER_TOKEN='<org-registration-token>' \
  ./scripts/install-actions-runner.sh
```

A failed run may leave a half-installed tree; the script removes it and retries. To clean up manually: `rm -rf ~/.local/share/potocolom-actions-runner`.

### 3. Wire PRs to this runner

Workflows must request the runner labels. On `main` (and PR branches that include this change), all four jobs use:

```yaml
runs-on: [self-hosted, Linux, X64, potocolom]
```

Merge the workflow change to `main` (via PR #93 or a dedicated CI PR) so **every pull request** routes here instead of `ubuntu-latest`. GitHub matches any online runner with those labels.

Checklist:

1. Runner **Idle** in Settings → Actions → Runners
2. Workflow files on the PR branch point at `self-hosted` (not `ubuntu-latest`)
3. Push or re-run checks: `gh pr checks <n>` then re-run failed jobs from the PR UI

While the runner is offline, matching jobs **queue** (they do not fall back to hosted runners).

### 4. Re-run failed PR checks

With the runner **idle** (green in Settings → Actions → Runners):

```bash
gh pr checks 93   # or your PR number
gh run rerun <run-id> --failed
```

## Switching hosted vs self-hosted

Workflows use:

```yaml
runs-on: [self-hosted, Linux, X64, potocolom]
```

When hosted minutes are available again, change back to `ubuntu-latest` in all four workflow files (or open a small PR). Do not run both on every PR - that doubles minute usage.

## Security notes

- Self-hosted runners execute workflow code with access to the checkout and any repo secrets scoped to the environment.
- For a solo org this is fine. If you accept outside contributions, enable **Settings → Actions → General → Fork pull request workflows → Require approval for all outside collaborators** so untrusted fork PRs cannot run on your runner before review.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Job queued forever | Runner offline - start `svc.sh` or `./run.sh` |
| Job fails in 2 s, no steps | Hosted minutes / billing - use self-hosted workflows + online runner |
| `404` on registration | Expired or wrong token type; use `gh api ... registration-token` or repo UI token, not a PAT |
| `404` on registration | `RUNNER_URL` must match token scope (repo vs org) |
| Backend job fails on postgres | Docker not running or user not in `docker` group |
| Wrong labels | Re-configure: `./config.sh remove` then re-run install script |

## Remove the runner

```bash
cd ~/.local/share/potocolom-actions-runner
sudo ./svc.sh stop
sudo ./svc.sh uninstall
./config.sh remove --token <removal-token-from-github-ui>
```
