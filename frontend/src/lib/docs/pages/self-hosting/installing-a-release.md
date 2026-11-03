---
title: Installing a release
subtitle: Install the latest release with the install script, or pin a specific version.
tab: self-hosting
group: Install
order: 1
source: docs/self-hosting.md "Installing a release", "First run"; README.md "Self-hosting"; scripts/install.sh
---

The install script downloads a release, checks it, starts the stack, and leaves the studio running on this machine. Run the script as the OS user who will own the install.

## Requirements

Release images run on Linux x86_64 only. The release does not provide CPU inference, ARM images, or Intel or Apple GPU support. Install these tools first:

- Docker with Compose v2.27.2 or newer
- `curl`, `tar`, `sha256sum` and `openssl`
- `flock` (from util-linux)
- GNU coreutils (`cksum`, `stat` and `readlink`)

Check the machine before you install. See [Checking a machine before you start](/docs/self-hosting/checking-a-machine). The install script runs the same check and stops on a blocking problem.

## Install the latest release

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | bash
```

The `latest` address always serves the install script of the newest release, and that script installs its own release.

## What the script does

1. Downloads the release bundle and checks it against the release `SHA256SUMS` file. It stops if the check fails.
2. Picks a Compose profile for this machine from the GPU it finds. See [GPU passthrough](/docs/self-hosting/gpu-passthrough).
3. Runs preflight, which writes `deploy/compose/.env` when that file is missing.
4. Writes the release version into `deploy/compose/.env` as `POTOCOLOM_VERSION`.
5. Pulls the released images and starts the stack.
6. Waits up to 120 seconds for the local API and its data store to pass `/api/v1/ready`.

Step 6 does not wait for GPU warmup or model downloads. The first job for each model downloads that model's weights, and jobs for that model wait in the queue until the download ends.

When the readiness check fails, the script stops and prints a command that shows the logs. Keep the data, fix the cause, and run the script again.

The script installs into `~/potocolom`. To use another directory, set `POTOCOLOM_DIR` before `bash`:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | POTOCOLOM_DIR=<directory> bash
```

> [!WARNING]
> Keep `deploy/compose/.env` in the install directory. It holds the database password and `FLEET_SECRET`. When the database volume exists and this file is missing, the script refuses to continue, because a new password would not match the database. The backup script copies this file too.

Open http://localhost:8080 in a browser to reach the studio on this machine.

## Upgrade

Run the install script of a newer release as the same OS user. The script upgrades the install in place. Existing model volumes keep their files across image upgrades.

A second install or preflight run by the same user, while one is still running, fails with a retry message. Wait for the first one to finish, then run the script again.

For upgrade steps beyond this script, see [Updating](/docs/self-hosting/updating).

## Install a pinned version

Put the release tag in the URL. Replace `vX.Y.Z` with the tag you want:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/download/vX.Y.Z/install.sh | bash
```

Each release copy of the install script installs its own tag. The tag in the URL is the version you get.

## Keep the data of a source checkout

A release install uses the Compose project name `potocolom`. A source checkout uses the project name `compose`. To keep the data of a checkout, do these steps before the first run of the release install:

1. Create `deploy/compose/` in the install directory (`~/potocolom` by default).
2. Copy the checkout's `deploy/compose/.env` into that directory.
3. Add the line `COMPOSE_PROJECT_NAME=compose` to the copy.
4. Run the install script.

The script keeps a project name that it finds in `.env`, and it does not read one from your shell.
