---
title: Backup and restore
subtitle: Back up the database and the image files, and restore them.
tab: self-hosting
group: Run it
order: 10
source: docs/self-hosting.md "Backup and restore", "What persists where"
---

Two scripts make a backup and put it back. `scripts/backup.sh` writes one recovery point to a directory that you name. `scripts/restore.sh` restores that recovery point into a new Compose project. Both cover local asset storage, which is what the shipped compose file uses (`STORAGE_BACKEND=local`).

## What a backup contains

A backup is a directory with four files:

- `database.dump`: a PostgreSQL dump with the accounts, jobs, prompts and asset rows.
- `assets.tar`: the whole `assets` volume, with the PNG masters and the WebP thumbnails.
- `env`: a copy of the env file. It holds `ROOT_KEYS`, `POSTGRES_PASSWORD` and `FLEET_SECRET`.
- `SHA256SUMS`: checksums for the three files above. `restore.sh` checks them first and refuses a backup that is incomplete or does not match.

The database dump and the asset files are taken at the same point in time. Keep them together. See [What persists where](/docs/self-hosting/what-persists-where) for why.

> [!WARNING]
> The `env` file holds every secret the install has. The backup directory and its files are readable by the owner only. Keep the backup private. There is no retention policy, and nothing is copied off the machine. The backup is the directory that you named.

## Back up

Run the backup script with the output directory:

```bash
scripts/backup.sh /backups/potocolom-2026-10-09
```

The script stops the `api` service before it reads anything. The API is the only writer of database rows and asset files, so this keeps the dump and the volume consistent. The install serves nothing while the backup runs, so plan a short outage.

When the backup finishes or fails, the script starts the API again if it was running. An API that you had stopped stays stopped. A failed backup leaves the previous backup in the same directory intact.

## Restore

Restore into a new, empty project. `restore.sh` refuses a project that already has a `pgdata` or `assets` volume.

The target project name is `COMPOSE_PROJECT_NAME` when that is set. If it is not set, the script uses the name recorded in the backup's env file. If that is not set either, it uses the name of the compose file's directory.

```bash
COMPOSE_PROJECT_NAME=potocolom-restored scripts/restore.sh /backups/potocolom-2026-10-09
```

The script then:

1. Starts PostgreSQL alone and restores the dump.
2. Unpacks `assets.tar` into the new `assets` volume.
3. Starts the stack with the backup's env file. That file carries `AUTH_MODE`, `ROOT_KEYS` and every other setting.

Two steps stay with you:

- Compose reads `deploy/compose/.env` by default, not the backup's env file. Pass `--env-file <backup-dir>/env` to later Compose commands. If the restore used the project name recorded in that file, you can copy the file over `deploy/compose/.env` instead.
- The restore starts `api` and `postgres` only. Start the worker with its profile as usual, with `--profile gpu` or `--profile rocm`.

> [!WARNING]
> The scripts do not back up an S3-compatible bucket. They only handle local storage, and cloud backups are out of scope. If you use an S3-compatible bucket, keep a separate, versioned backup of it.

## Known limits

- If an account purge was interrupted by the backup stop, some rows can point at files that are already gone. The purge resumes when the restored API starts, and it removes those rows.
- The repository test `scripts/test-backup-restore.sh` runs the whole flow on the smoke stack with the simulated worker. You can run it with `make verify-backup`. It is a development check, not a step in a production restore.
