---
title: What persists where
subtitle: Which data lives in the database, which lives in files, and which is temporary.
tab: self-hosting
group: Run it
order: 11
source: docs/self-hosting.md "What persists where", "Models you already have"
---

The install keeps its data in four named Docker volumes, in PostgreSQL, and in container logs. The table shows where each kind of data lives and what happens to it on an upgrade.

| Data                                                                    | Where it lives                        | On upgrade or rebuild                    | If the data is lost                               |
| ----------------------------------------------------------------------- | ------------------------------------- | ---------------------------------------- | ------------------------------------------------- |
| Users, jobs (prompts, parameters, seeds) and asset records              | `pgdata` volume, PostgreSQL           | Kept. Volumes are untouched by rebuilds. | History and gallery are gone.                     |
| Job failure reasons, phase timings, GPU sample history and usage events | PostgreSQL                            | Kept, with the rest of PostgreSQL.       | Same as `pgdata`.                                 |
| Generated PNG masters and WebP thumbnails                               | `assets` volume                       | Kept. Volumes are untouched by rebuilds. | Images are gone, and asset rows point at nothing. |
| Operator manifests (JSON) and `overrides/`                              | `models` volume                       | Kept across image upgrades.              | Bundled ones come back from the worker image.     |
| Downloaded model weights                                                | `hf-cache` volume                     | Kept. Volumes are untouched by rebuilds. | Downloaded again on next use (2-7 GB per model).  |
| Container logs                                                          | Docker's `json-file` log, on the host | Kept across container restarts.          | Removed when the container is removed.            |

## Back up the database and the files together

Back up `pgdata` and `assets` together. Jobs and asset rows refer to files by storage key. If you restore one without the other, the rows point at files that do not exist. See [Backup and restore](/docs/self-hosting/backup-and-restore).

`hf-cache` can be rebuilt, so you do not need to back it up. Back up `models` if it holds operator manifests or `overrides/` files, because the worker image does not hold those.

## Model manifests

Bundled model manifests come from the worker image (`worker/models/`), so a release updates them. Operator manifests go in the `models` volume, and a file in its `overrides/` directory wins over a bundled model with the same id. Restart the worker after a change. Licensing notes for each model are in [third-party-models.md](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/third-party-models.md).

To remove an old SDXL Turbo manifest from an existing install, remove `/models/sdxl-turbo.json` from that worker's model volume and restart the worker. Saved images and cached weights do not need removal.

## Models you already have

The worker finds complete models already in its Hugging Face cache at the pinned revision. This includes downloads from older potocolom installs, the `hf` CLI, and other apps that use the standard cache. A different revision of the same model is not reused.

To share another app's cache, set `HF_HOME` to that cache when you run natively. In a container, replace the `hf-cache` volume in `deploy/compose/compose.yml` with the host cache mount shown in the file. If another app removes files, the model shows as not installed, and you can install it again. The worker adopts such a cache offline, from the Hub pins it ships. See [Updating](/docs/self-hosting/updating).

## Separate installs keep separate data

A release install uses the Compose project `potocolom`. A source checkout uses the Compose project `compose`. The two keep their data apart. To keep a checkout's data when you install a release, see [Installing a release](/docs/self-hosting/installing-a-release).

## Leftovers from failed jobs

The API deletes what a failed or retried job left behind. If a delete fails, the API records it in the `pending_deletes` table in PostgreSQL. It retries that delete every five minutes, and it does not only log the failure.

If the retries keep failing, the cause is usually the `assets` volume. Look for a full disk, or for permissions that the container cannot write through. The log line to look for names the key and says `still cannot delete`. The `pending_deletes` table lists what is still waiting.
