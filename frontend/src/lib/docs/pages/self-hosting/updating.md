---
title: Updating
subtitle: Upgrade to a new release, and go back if an upgrade fails.
tab: self-hosting
group: Run it
order: 13
source: docs/self-hosting.md "Upgrading", "Updating", "Accounts" ("First run")
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

The installer does this, in order:

- It checks the bundle against the release's `SHA256SUMS` and pulls the release's images. Until the pull succeeds, nothing in the install directory changes, so a failed pull leaves the install as it was.
- It keeps the profile that the install already runs. The profile is saved in `deploy/compose/.env` as `COMPOSE_PROFILES`. If this machine now detects a different GPU, the installer warns and keeps the saved profile. Set `POTOCOLOM_PROFILE` to `gpu`, `rocm` or `api` only to change the profile; an explicit choice also removes the worker of the old profile.
- It prints what it is doing: `Installing`, `Upgrading`, `Reinstalling` or `Downgrading`, with the versions.
- Before it replaces the install, it stops the API and workers, reads the database revision, and takes a backup into `backups/` in the install directory. It takes no backup for a new database or for a reinstall of the same release, unless an earlier upgrade stopped before the API became ready. The backup needs free space for the database and the assets, plus 10 percent and 1 GiB. When there is not enough space, the installer refuses and prints the numbers. `POTOCOLOM_SKIP_BACKUP=1` skips the backup, and then there is no recovery point.
- It refuses to run an older release on a database that a newer release migrated. It also refuses a database that has tables but no `alembic_version` table, since it cannot tell which release migrated it. Nothing changes in either case, and the services it stopped start again.

Notes for the upgrade:

- Run the upgrade as the same OS user that made the install. A second install or preflight under the same user fails with a retry message while a lock is held.
- The installer waits up to 120 seconds for the local API and store to pass `/api/v1/ready`. This check does not wait for GPU warmup or model downloads. If the API does not become ready, the script returns an error and prints a logs command. The data is intact: fix the cause, then run the script again. The rerun takes a backup first, because the failed run may already have migrated the database.
- An install that already exists keeps its choice. It keeps accounts if it had them, and it runs without accounts if it holds any user or work, so an upgrade does not show the setup page. See [First run](/docs/self-hosting/first-run).

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
- Bundled model manifests update with each release. Model volumes keep their files across image upgrades. To change a bundled model, put the edited manifest in the volume's `overrides/` directory. If an old SDXL Turbo manifest is still in the model volume, its model keeps working; to remove it, remove `/models/sdxl-turbo.json` from that worker's model volume and restart the worker. Saved images and cached weights do not need removal.
- Legacy model caches are adopted from the Hub pins bundled with the worker, so an offline worker adopts the bundled models without a Hub check. See [What persists where](/docs/self-hosting/what-persists-where).

> [!WARNING]
> Migrations run as soon as the new API starts. The source describes no way to undo them. Read the rollback section below before you upgrade.

## Rollback

Going back to an earlier release is not automatic. The installer refuses to run an older release on a database that a newer release migrated, so the older install script alone does not undo an upgrade. Two routes remain:

- Rerun the install script of the newer release. This is the forward fix, and it also works after a failed upgrade.
- Restore a backup into a new project. The installer prints the restore command only when a backup in `backups/` has a database revision that the release being installed knows. The command runs `scripts/restore.sh` from the install directory with that backup's compose file. It sets `POTOCOLOM_VERSION` to the release being installed, and `COMPOSE_PROFILES` so that the worker starts. If no backup qualifies, the forward fix is the route. Stop the broken project first, because it holds port 8080. The restored project starts with an empty model cache.

Backups are kept in the install directory's `backups/` folder. Each one is named by the old version, the database revision and the UTC time. Old backups are kept, and there is no retention policy: delete them by hand when you no longer need them. See [Backup and restore](/docs/self-hosting/backup-and-restore) for what a backup holds.
