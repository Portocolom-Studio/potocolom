---
title: What persists where
subtitle: Which data lives in the database, which lives in files, and which is temporary.
tab: self-hosting
group: Run it
order: 10
source: docs/self-hosting.md "What persists where"
---

The install keeps its data in four named Docker volumes, in PostgreSQL, and in container logs. The table shows where each kind of data lives and what happens to it on an upgrade.

| Data                                                                    | Where it lives                        | On upgrade or rebuild                    | If the data is lost                               |
| ----------------------------------------------------------------------- | ------------------------------------- | ---------------------------------------- | ------------------------------------------------- |
| Users, jobs (prompts, parameters, seeds) and asset records              | `pgdata` volume, PostgreSQL           | Kept. Volumes are untouched by rebuilds. | History and gallery are gone.                     |
| Job failure reasons, phase timings, GPU sample history and usage events | PostgreSQL                            | Kept, with the rest of PostgreSQL.       | Same as `pgdata`.                                 |
| Generated PNG masters and WebP thumbnails                               | `assets` volume                       | Kept. Volumes are untouched by rebuilds. | Images are gone, and asset rows point at nothing. |
| Model manifests (JSON)                                                  | `models` volume                       | Kept across image upgrades.              | Re-seeded from the worker image on the next boot. |
| Downloaded model weights                                                | `hf-cache` volume                     | Kept. Volumes are untouched by rebuilds. | Downloaded again on next use (2-7 GB per model).  |
| Container logs                                                          | Docker's `json-file` log, on the host | Kept across container restarts.          | Removed when the container is removed.            |

## Back up the database and the files together

Back up `pgdata` and `assets` together. Jobs and asset rows refer to files by storage key. If you restore one without the other, the rows point at files that do not exist. See [Backup and restore](/docs/self-hosting/backup-and-restore).

`hf-cache` and `models` can be rebuilt. You do not need to back them up.

## Model manifests

Models are JSON manifests in the `models` volume. The worker seeds that volume from `worker/models/` on first boot. To add or edit a manifest, change it in the volume, or rebuild the image, and then restart the worker. Licensing notes for each model are in [third-party-models.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/third-party-models.md).

To remove an old SDXL Turbo manifest from an existing install, remove `/models/sdxl-turbo.json` from that worker's model volume and restart the worker. Saved images and cached weights do not need removal.

## Separate installs keep separate data

A release install uses the Compose project `potocolom`. A source checkout uses the Compose project `compose`. The two keep their data apart. To keep a checkout's data when you install a release, see [Installing a release](/docs/self-hosting/installing-a-release).

## Leftovers from failed jobs

The API deletes what a failed or retried job left behind. If a delete fails, the API records it in the `pending_deletes` table in PostgreSQL. It retries that delete every five minutes, and it does not only log the failure.

If the retries keep failing, the cause is usually the `assets` volume. Look for a full disk, or for permissions that the container cannot write through. The log line to look for names the key and says `still cannot delete`. The `pending_deletes` table lists what is still waiting.
