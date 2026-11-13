---
title: Remote GPU workers
subtitle: Run the API on one machine and the GPU worker on another.
tab: self-hosting
group: Install
order: 6
source: docs/self-hosting.md "An API host with remote GPU workers", "Upgrading", "First run", "Accounts" ("First run"); deploy/compose/.env.example; deploy/compose/compose.yml
---

Use this layout when the machine that runs the studio has no GPU, or when the GPU sits on another machine.

- The **API host** runs the API and PostgreSQL with the `api` profile. It has no local worker.
- The **GPU host** runs only the worker container. The worker opens a WebSocket to `/api/v1/fleet` on the API host, and it takes jobs from there.

For image jobs, the worker also talks to the API over HTTP. It uploads results and fetches source images, using the address in `INTERNAL_URL`. The worker and the API must be able to reach each other at the addresses that the two settings name. SSH is not part of this setup.

## Secrets

These are the secrets in this setup. Their values never appear in this guide, so copy them from the files named here.

- `FLEET_SECRET`: in `deploy/compose/.env` on the API host. Preflight creates it. The worker needs the same value.
- `FLEET_TOKEN`: in `worker.env` on the GPU host. It holds a copy of `FLEET_SECRET`.
- `JOIN_TOKEN`: in `worker.env` on the GPU host, for a worker that joins with a one-use token. The worker spends it once, at its first start. The section below describes the join.
- The worker credential: `fleet-credential.json` in the worker's state volume. The worker saves it after it joins, and it is the worker's identity. Keep it private.
- `ROOT_KEYS`: the root key ring for accounts, in the API host's `deploy/compose/.env`. This setup does not need it. See [Accounts](/docs/self-hosting/accounts).

> [!WARNING]
> Any worker that holds `FLEET_SECRET` receives jobs for the models it announces, together with their prompts and source images. Give the secret only to machines that you would trust with every user's prompts. Keep `worker.env` private, with mode `0600`.

## Set up the API host

1. Configure an HTTPS reverse proxy in front of the API before you use this setup. See [TLS and HSTS](/docs/self-hosting/tls-and-hsts).

2. Install the API and the database, without a local worker. Replace `studio.example.com` with your own HTTPS origin:

   ```bash
   curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | POTOCOLOM_PROFILE=api POTOCOLOM_PUBLIC_URL=https://studio.example.com bash
   ```

3. In `deploy/compose/.env` on the API host, set `INTERNAL_URL` to the same HTTPS origin:

   ```text
   INTERNAL_URL=https://studio.example.com
   ```

4. Apply the change from the install directory:

   ```bash
   env -u INTERNAL_URL docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml --profile api up -d --no-build
   ```

   `PUBLIC_URL` and `INTERNAL_URL` must both use addresses that the remote worker can reach. The default `INTERNAL_URL`, `http://api:8080`, works only on the local Compose network.

   The installer prints a one-use setup link. Spend it in a browser on the API host before you use the studio. The fleet socket answers before setup, so a worker can report its hardware while the install is still unclaimed.

   If you choose no accounts, keep the install on a trusted network. Anyone who can reach that API has full access. See [First run](/docs/self-hosting/first-run) and [Accounts](/docs/self-hosting/accounts).

## Set up the GPU host

1. Create a private `worker.env` file. Set its mode to `0600` with `chmod 600 worker.env`:

   ```dotenv
   DEVICE=cuda
   API_URL=wss://studio.example.com/api/v1/fleet
   FLEET_TOKEN=<copy FLEET_SECRET from the API host>
   MODELS_DIR=/models
   VRAM_RESERVE_MB=1536
   ```

   The worker refuses a plain `ws://` address to a remote host, because it would send the fleet token in clear text. It accepts `ws://` only for `localhost`, a loopback address, or a single-label name such as the compose service `api`. Keep `API_URL` on `wss://`. On a private test network without TLS, set `ALLOW_INSECURE_FLEET=true` in `worker.env` to allow `ws://` to another host. Without that setting the worker stops at startup, before it connects, and the error names `ALLOW_INSECURE_FLEET`.

   Replace `<copy FLEET_SECRET from the API host>` with the value of `FLEET_SECRET` from the API host's `deploy/compose/.env`. Replace `studio.example.com` with the same HTTPS origin you used on the API host.

2. Start the NVIDIA worker. Replace `vX.Y.Z` with the release tag that the API host runs:

   ```bash
   docker run -d --name potocolom-worker --restart unless-stopped \
     --gpus all --env-file ./worker.env \
     -v potocolom-models:/models \
     -v potocolom-hf-cache:/root/.cache/huggingface \
     ghcr.io/portocolom-studio/potocolom-worker:vX.Y.Z-cuda
   ```

   For an AMD card, set `DEVICE=rocm` in `worker.env`, use the `vX.Y.Z-rocm` image, and replace `--gpus all` with `--device /dev/kfd --device /dev/dri --group-add video`.

   Use the same driver checks and VRAM reserve as for a local worker. See [GPU passthrough](/docs/self-hosting/gpu-passthrough).

3. Test the worker. Run one generation and one image-to-image job. The image-to-image job checks both the image upload and the source fetch.

## Join with a one-use token

A worker can join without a copy of `FLEET_SECRET`. Use `POST /api/v1/admin/fleet/members` on the API host to create a join token. The studio screen that creates join tokens is not built yet. A join token works once and lives one hour. In `worker.env`, set `JOIN_TOKEN` and `STATE_DIR` in place of `FLEET_TOKEN`:

```dotenv
DEVICE=cuda
API_URL=wss://studio.example.com/api/v1/fleet
JOIN_TOKEN=<join token from the API host>
STATE_DIR=/var/lib/potocolom-worker
MODELS_DIR=/models
VRAM_RESERVE_MB=1536
```

Mount a volume on `STATE_DIR`, so the credential survives a container that is created again. Give each worker its own volume: a second worker that starts on a `STATE_DIR` in use is refused.

```bash
docker run -d --name potocolom-worker --restart unless-stopped \
  --gpus all --env-file ./worker.env \
  -v potocolom-models:/models \
  -v potocolom-hf-cache:/root/.cache/huggingface \
  -v potocolom-worker-state:/var/lib/potocolom-worker \
  ghcr.io/portocolom-studio/potocolom-worker:vX.Y.Z-cuda
```

The worker spends the join token at its first start. It saves the worker ID and the credential that it receives in `STATE_DIR/fleet-credential.json`, with mode `0600`. Every later start uses that file, and `JOIN_TOKEN` is then ignored. A worker with `JOIN_TOKEN` and no `STATE_DIR` refuses to start: the credential would be lost at the next start, and the token would already be spent. The worker also checks that it can write to `STATE_DIR` before it uses the token, so a volume that is not mounted costs no token. With `WORKER_UID` set, `STATE_DIR` must lie outside `/models`: the container gives that directory to the worker user. The state volume holds the only copy of the credential, so include it in your backups. Without it, the machine must join again.

If the worker stops with a message that the join token is spent, expired or removed, create a new join token, set it as `JOIN_TOKEN` in `worker.env`, and create the container again. The `FLEET_TOKEN` setup above still works for a worker that has no `JOIN_TOKEN` and no saved credential.

When the studio removes a machine, the next handshake from its worker fails with HTTP 403. The worker logs this on each attempt, and it waits 30 seconds between attempts:

```text
this machine was removed from the install; add it again from the studio and remove /var/lib/potocolom-worker/fleet-credential.json
```

The worker does not delete the file. To add the machine again, stop the container, delete the file, set a new `JOIN_TOKEN`, and create the container again. For example, delete the file with `docker run --rm -v potocolom-worker-state:/state alpine rm /state/fleet-credential.json`.

## Upgrade order

Upgrade the API host first, then the worker. The worker protocol promises that the API is at the same version as its workers or newer. See [Updating](/docs/self-hosting/updating).

## Failure modes

- **The worker connects, but image jobs fail.** A successful fleet WebSocket connection does not prove that image jobs work. Check `INTERNAL_URL` first, because its default works only on the local Compose network. Set it as in step 3 above, then run the image-to-image test again.
- **The API refuses fleet connections, or it does not start.** The API host has an empty `FLEET_SECRET`. An unset `FLEET_TOKEN_KEY` refuses every fleet handshake, and the API refuses to start. The error names `scripts/preflight.sh`. Run `scripts/preflight.sh` on the API host to fill the empty value, restart the API, then copy the new `FLEET_SECRET` into `worker.env` on the GPU host.
