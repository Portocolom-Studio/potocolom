---
title: Share an existing model cache
subtitle: Reuse the Hugging Face models that are already on this machine.
tab: self-hosting
group: Install
order: 14
source: docs/self-hosting.md "Share an existing model cache", "Models you already have"
---

A release install can use a Hugging Face cache that already exists on this machine, so models downloaded earlier are not downloaded again. Give the installer the hub directory of that cache, the `hub` folder inside it:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | POTOCOLOM_HF_CACHE="$HOME/.cache/huggingface/hub" bash
```

The installer checks the directory before it changes anything in the install:

- It must be an absolute path to an existing directory. A link is followed, and the directory it points to is the one that is saved. Use only letters, digits and `. _ / @ + -` in the path, because the value is saved without quotes, so a space, a dollar sign or a new line is refused.
- It must not be a Hugging Face home directory: one that holds a `token` or `stored_tokens` file, or one called `huggingface` with a `hub` folder inside. Sharing it would expose the token. The installer names the `hub` folder to pass.
- It must not be `/models` or `/tmp/worker-home`, or a path inside or above either. Those are the worker's own paths.
- Docker must not run rootless.
- Every file in it must belong to your user. A root process, such as an older release worker, may have written files there. The installer then stops and prints a `sudo chown -R` command for your user and group. Run that command, then run the installer again.
- The installer refuses to run as root, because the cache must belong to the user who owns it.

When the checks pass, the installer saves three keys in `deploy/compose/.env`: `HF_CACHE_DIR` (the directory), `WORKER_UID` and `WORKER_GID` (your user and group). The worker mounts the directory at the same path inside the container and runs as your user. Only the hub directory is mounted, so the Hugging Face token file in your home directory is never shared. Because the path is the same inside and outside the container, the model markers match what a source checkout (`make dev`) writes. Later upgrades keep these keys. The models the worker finds there are listed in [Models you already have](/docs/self-hosting/what-persists-where).

Without `POTOCOLOM_HF_CACHE`, the installer prints how many model repositories it found in the default cache, with the command that shares them. It asks nothing and saves nothing. A default install keeps the `hf-cache` volume and runs the worker as root, as before.

To stop sharing, run the installer with `POTOCOLOM_HF_CACHE` set to an empty value. It removes the three keys, and the worker uses its own `hf-cache` volume again. The host cache is left as it is.

What sharing costs:

- Removing a model in the studio deletes its pinned files in the shared cache, including files that other tools on this machine read.
- Upscaler weights stay in the `models` volume and are not shared. Their markers can change between a host `make dev` worker and the container, because the paths differ. Each marker is rebuilt on the next model scan.
- Rootless Docker cannot share a host cache. The worker must drop to your user, and rootless Docker maps that user to one that cannot write the cache.

`scripts/preflight.sh` names the cache path in use and its free space.
