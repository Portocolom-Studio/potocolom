---
title: Gated models
subtitle: Use models that need an accepted license and an access token.
tab: self-hosting
group: Run it
order: 9
source: docs/self-hosting.md "Gated models"
---

A gated model is one whose weights sit in a Hugging Face repository that you can only download after you accept its license and use an access token. The source names one gated model: `sd35-medium` (Stable Diffusion 3.5 Medium).

## How the worker finds out

The worker has no separate check for gated models. It advertises the model even without credentials. The first job against the model fails when Hugging Face refuses the download.

The model manifest records the license name and link in [worker/models/sd35-medium.json](https://github.com/Portocolom-Studio/potocolom/blob/main/worker/models/sd35-medium.json). Read the terms in [model license terms](https://github.com/Portocolom-Studio/potocolom/blob/main/docs/third-party-models.md) before you use the model.

## Set up a gated model

1. Accept the license at [huggingface.co/stabilityai/stable-diffusion-3.5-medium](https://huggingface.co/stabilityai/stable-diffusion-3.5-medium) on the Hugging Face account that will download the weights.
2. Create a read token on that account. Put it in `deploy/compose/.env`:

```dotenv
HF_TOKEN=<your-read-token>
```

The compose file passes `HF_TOKEN` to the worker service only. The Hugging Face client reads it from the environment. No other configuration is needed.

3. Recreate the worker so that it picks up the variable:

```bash
docker compose --env-file deploy/compose/.env -f deploy/compose/compose.yml --profile gpu up -d
```

On AMD, use `--profile rocm` instead.

Watch the download with:

```bash
docker compose -f deploy/compose/compose.yml logs -f worker
```

## What to expect

- The weights are roughly 15 GB. The other models need 2-7 GB each.
- The source gives 14 GB as the top of the VRAM range for the models a self-hoster can select, and this model sits at that top.
- The first job per model starts the download. Jobs on that model wait in the queue until the download finishes.
- The download can be slow on some connections, so allow time for it.
