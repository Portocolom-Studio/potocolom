---
title: Remote GPU workers
subtitle: Run the API on one machine and the GPU worker on another.
tab: self-hosting
group: Install
order: 5
source: docs/self-hosting.md "An API host with remote GPU workers", "Upgrading", "First run"; deploy/compose/.env.example; deploy/compose/compose.yml
---

Use this layout when the machine that runs the studio has no GPU, or when the GPU sits on another machine.

- The **API host** runs the API and PostgreSQL with the `api` profile. It has no local worker.
- The **GPU host** runs only the worker container. The worker opens a WebSocket to `/api/v1/fleet` on the API host, and it takes jobs from there.

For image jobs, the worker also talks to the API over HTTP. It uploads results and fetches source images, using the address in `INTERNAL_URL`. The worker and the API must be able to reach each other at the addresses that the two settings name. SSH is not part of this setup.

## Secrets

These are the secrets in this setup. Their values never appear in this guide, so copy them from the files named here.

- `FLEET_SECRET`: in `deploy/compose/.env` on the API host. Preflight creates it. The worker needs the same value.
- `FLEET_TOKEN`: in `worker.env` on the GPU host. It holds a copy of `FLEET_SECRET`.
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

   Keep `AUTH_MODE=none` only on a trusted network. Anyone who can reach that API has full access. See [Accounts](/docs/self-hosting/accounts).

## Set up the GPU host

1. Create a private `worker.env` file. Set its mode to `0600` with `chmod 600 worker.env`:

   ```dotenv
   DEVICE=cuda
   API_URL=wss://studio.example.com/api/v1/fleet
   FLEET_TOKEN=<copy FLEET_SECRET from the API host>
   MODELS_DIR=/models
   VRAM_RESERVE_MB=1536
   ```

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

## Upgrade order

Upgrade the API host first, then the worker. The worker protocol promises that the API is at the same version as its workers or newer. See [Updating](/docs/self-hosting/updating).

## Failure modes

- **The worker connects, but image jobs fail.** A successful fleet WebSocket connection does not prove that image jobs work. Check `INTERNAL_URL` first, because its default works only on the local Compose network. Set it as in step 3 above, then run the image-to-image test again.
- **The API refuses fleet connections, or it does not start.** The API host has an empty `FLEET_SECRET`. An unset `FLEET_TOKEN_KEY` refuses every fleet handshake, and the API refuses to start. The error names `scripts/preflight.sh`. Run `scripts/preflight.sh` on the API host to fill the empty value, restart the API, then copy the new `FLEET_SECRET` into `worker.env` on the GPU host.
