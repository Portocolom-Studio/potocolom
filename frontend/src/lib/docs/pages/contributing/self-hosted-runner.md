---
title: Self-hosted runner
subtitle: How project CI runs on machines the maintainers operate.
tab: contributing
group: Maintain
order: 4
source: docs/self-hosted-runner.md "What it runs", "One-time setup" (1. Host prerequisites, 2. Register and start the runners, 3. How many runners), "Pre-PR checklist", "Wire PRs to this runner", "Re-run failed PR checks", "Switching hosted vs self-hosted", "Security notes", "Troubleshooting", "Remove the runner"; Makefile targets ci-runner-install, ci-runner-service-install, ci-runner-start, ci-runner-stop, ci-runner-status
---

The self-hosted runner is a GitHub Actions runner that a maintainer installs on a machine they operate. It runs the project's CI jobs. The project uses it so that CI keeps working when GitHub-hosted runner minutes run out. A runner you install yourself costs nothing extra.

## What it runs

The runner runs the four path-filtered product workflows, plus the deploy, docs and toolchain workflows. Each workflow needs these tools on the host:

| Workflow   | Needs on the host                                                                              |
| ---------- | ---------------------------------------------------------------------------------------------- |
| backend    | Docker (for the PostgreSQL service container), Python 3.11                                     |
| worker     | Python 3.11                                                                                    |
| frontend   | Node 24, Chrome or Chromium, Docker (for the PostgreSQL service container), Python 3.11        |
| simulation | Docker (for the PostgreSQL service container), Python 3.11                                     |
| deploy     | Docker (`make verify-compose` and the compose smoke test)                                      |
| docs       | mermaid-cli and Chrome (`make verify-mermaid`)                                                 |
| toolchain  | Python 3.11, Node, curl or wget (`make verify-guards`, `make setup` and `make lint-workflows`) |

Pull request jobs do not run GPU inference. They run the same component checks that `make verify` runs.

Each browser check runs in a private network with only the loopback interface (`scripts/isolated-net.sh`). Other jobs on the machine create and remove Docker networks, and Chrome cancels its page loads when that happens. The host needs unprivileged user namespaces, which Debian 12 allows by default. Without them, the script warns and uses the normal network. The sign-in and setup checks always use the normal network, because they connect to the PostgreSQL service container on the host.

A version tag also runs the real installer on the AMD GPU. That check needs port 8080 to be free, and it takes the release GPU lock. See the [release checks](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/release-checklist.md).

## Labels

Each workflow asks for a runner with these labels:

```yaml
runs-on: [self-hosted, Linux, X64, potocolom]
```

- `self-hosted` marks the runner as one you host. GitHub sets it on every self-hosted runner.
- `Linux` and `X64` name the operating system and the processor type.
- `potocolom` marks the runner as this project's runner.

A job runs only on a runner that has all four labels. While no matching runner is online, the matching jobs wait in the queue. They do not fall back to GitHub-hosted runners.

## Setup

The steps below set up one machine. Replace the placeholders in angle brackets with your own values.

### 1. Install the host tools

Install Docker and keep it running:

```bash
docker --version
sudo systemctl enable --now docker
```

Install Python 3.11 and Node 24 on the system. The setup actions do not install them on a self-hosted Debian machine, so the jobs use the system versions. Python jobs create a virtual environment for each run, because Debian blocks system-wide pip installs.

```bash
python3.11 --version
node --version   # 24.x
```

The user that runs the runner must be able to use Docker. Read the security warning below before you add it to the `docker` group.

### 2. Register and start the runners

Run these commands from the repository root:

```bash
make ci-runner-install
make ci-runner-service-install   # once, needs sudo
make ci-runner-start
make ci-runner-status            # should show active (running)
```

`make ci-runner-install` registers each runner with GitHub. It needs a registration token for each runner. The install script can get one for you through the `gh` command-line tool, if you are a repository admin:

```bash
./scripts/install-actions-runner.sh
```

You can also pass a token that you copy from the repository's Settings page. Go to Settings, then Actions, then Runners, then New self-hosted runner. Use the registration token from that page, not a personal access token:

```bash
RUNNER_TOKEN='<registration-token>' ./scripts/install-actions-runner.sh
```

To get a fresh token with the `gh` tool:

```bash
RUNNER_TOKEN="$(gh api repos/<owner>/<repo>/actions/runners/registration-token -X POST --jq .token)" \
  ./scripts/install-actions-runner.sh
```

Registration tokens expire after one hour and work only once. If you register a runner for an organization instead of a repository, set the URL to match the token:

```bash
RUNNER_URL='https://github.com/<organization>' \
  RUNNER_TOKEN='<organization-registration-token>' \
  ./scripts/install-actions-runner.sh
```

If an install fails, the script removes the partial directory and tries again. To clean up by hand, delete the runner directory, `$HOME/.local/share/potocolom-actions-runner`.

### 3. Choose the number of runners

The default is 4 runners. Each runner has its own directory and its own service, so the checks of one push can run at the same time. The directories are `$HOME/.local/share/potocolom-actions-runner`, then the same name with `-2`, `-3` and `-4` added.

To use a different number, pass `CI_RUNNERS` to the install target, and pass the same value to the start, stop and status targets:

```bash
make ci-runner-install CI_RUNNERS=6
make ci-runner-start CI_RUNNERS=6
```

To add runners later, run `make ci-runner-install` again. It skips the directories that are already set up.

### 4. Check that the runner is idle

Go to Settings, then Actions, then Runners, in the repository on GitHub. Each runner should show Idle. Start the runners before you open a pull request:

```bash
make ci-runner-start
```

Stopping the runners is optional when you are done: `make ci-runner-stop`.

### 5. Re-run failed checks

When the runners are idle, you can re-run the failed jobs of a pull request:

```bash
gh pr checks <pull-request-number>
gh run rerun <run-id> --failed
```

## Switching between hosted and self-hosted runners

The workflow files in `.github/workflows/` use the self-hosted labels. To use GitHub-hosted runners again, change `runs-on` to `ubuntu-latest` in each workflow file. Do not run both kinds on every pull request, because that uses twice the minutes.

## Security

> [!WARNING]
> A self-hosted runner runs workflow code on your machine. For a pull request, that code and the workflow file both come from the pull request, so a check written in the workflow YAML does not protect you from a fork. The runner can read the checkout and any secrets that the job's environment gives it. The runner user is in the `docker` group, and membership in that group gives root-level access to the host.

The control is GitHub's fork approval setting. Set it in Settings, then Actions, then General, under "Approval for running fork pull request workflows from contributors", to "Require approval for all external contributors". The setting for first-time contributors is not enough. After one merged pull request, a contributor's later runs do not need approval.

Before you approve a fork's workflow run, read its changes to `.github/`, the `Makefile`, `scripts/`, `deploy/` and every Dockerfile.

## Troubleshooting

| Symptom                                    | Fix                                                                                                                                                                                                                                                 |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A job waits in the queue and never starts  | The runner is offline. Start it with `make ci-runner-start`, or run `./run.sh` in the runner directory.                                                                                                                                             |
| A job fails in a few seconds with no steps | GitHub-hosted minutes or billing are the problem. Check that the workflow uses the self-hosted labels and that a runner is online.                                                                                                                  |
| Registration returns 404                   | The token expired, is single use, or is a personal access token. Get a fresh registration token. Also check that `RUNNER_URL` matches the token: a repository token needs the repository URL, and an organization token needs the organization URL. |
| The backend job fails on PostgreSQL        | Docker is not running, or the runner user is not in the `docker` group.                                                                                                                                                                             |
| The runner has the wrong labels            | Run `./config.sh remove` in the runner directory, then run the install script again.                                                                                                                                                                |

## Remove a runner

Run these commands in the runner directory. Use the removal token from the GitHub Settings page:

```bash
cd "$HOME/.local/share/potocolom-actions-runner"
sudo ./svc.sh stop
sudo ./svc.sh uninstall
./config.sh remove --token <removal-token>
```
