---
title: Updating
subtitle: Upgrade to a new release, and go back if an upgrade fails.
tab: self-hosting
group: Run it
order: 12
source: docs/self-hosting.md "Upgrading", "Updating"
---

The steps depend on how you installed. Use the release steps for an install made with the install script. Use the checkout steps for an install made from a source checkout.

Upgrade the API before you upgrade any remote worker. The worker protocol promises that a worker can be one version behind its API. A worker that is ahead of its API is not supported.

## Upgrade a release install

Rerun the install script of the newer release. The `latest` address serves the newest release's script, and that script installs its own release:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | bash
```

To install one specific release, use the script of that release. Replace `vX.Y.Z` with the release tag:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/download/vX.Y.Z/install.sh | bash
```

Notes for the upgrade:

- Run the upgrade as the same OS user that made the install. A second install or preflight under the same user fails with a retry message while a lock is held.
- The installer checks the downloaded bundle against the release's `SHA256SUMS`. It then sets `POTOCOLOM_VERSION` in `deploy/compose/.env`.
- Use the same profile that you first started with. Set `POTOCOLOM_PROFILE=gpu`, `rocm` or `api` on each install or upgrade when the automatic choice is not suitable.
- The installer waits up to 120 seconds for the local API and store to pass `/api/v1/ready`. This check does not wait for GPU warmup or model downloads. If the API does not become ready, the script returns an error and prints a logs command. Keep the data, fix the cause, and retry.

## Upgrade a source checkout

From the checkout, pull the new code and rebuild:

```bash
git pull
docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
# AMD: --profile rocm, the same profile you first started with
```

Use the same profile that you first started with. The `gpu` and `rocm` profiles both run a worker that uses the model volumes, so run one or the other, never both.

## Upgrade a remote GPU worker

Upgrade the API host first. Then start each worker with the new release tag, using the `docker run` command in [An API host with remote GPU workers](/docs/self-hosting/remote-gpu-workers).

When a worker is one version ahead of its API, the API drops the extra fields in the worker's handshake. From protocol 4, the same mismatch also leaves realtime unready. Jobs still run. Compose installs bring the API and the worker from one image, so this mismatch does not arise for them.

## Data safety

- Volumes are untouched by rebuilds, so your jobs, assets and downloaded weights stay in place. See [What persists where](/docs/self-hosting/what-persists-where).
- Database migrations run automatically when the API starts. See [the decision on migrations at startup](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/decisions.md).
- Model volumes keep their files across image upgrades. If an old SDXL Turbo manifest is still in the model volume, remove `/models/sdxl-turbo.json` from that worker's model volume and restart the worker. Saved images and cached weights do not need removal.

> [!WARNING]
> Migrations run as soon as the new API starts. The source describes no way to undo them. Read the rollback section below before you upgrade.

## Rollback

The source describes no rollback to an earlier release. It does not describe running an older install script or image to go back. The only documented way to recover data is a backup taken with `scripts/backup.sh`, restored into a new project. See [Backup and restore](/docs/self-hosting/backup-and-restore).
