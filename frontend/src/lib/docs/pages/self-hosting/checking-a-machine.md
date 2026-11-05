---
title: Checking a machine before you start
subtitle: Run preflight to see which GPU profile this machine can use.
tab: self-hosting
group: Install
order: 3
source: docs/self-hosting.md "Checking a machine before you start", "First run"; scripts/preflight.sh; scripts/ensure-env.sh
---

`scripts/preflight.sh` checks this machine against the install requirements. It names the Compose profile the machine can run, and it prints a fix for each missing item. With make, run `make preflight`.

Run it from the install directory (`~/potocolom` by default) or from a source checkout:

```bash
scripts/preflight.sh
```

The install script runs preflight for you. Run it yourself to check a machine before you install, or after you change the hardware or the Docker setup.

## What it checks

- **Host:** Linux, at least 8 GB of RAM (16 GB is comfortable), at least 20 GB of free disk (50 GB is comfortable), and port 8080 either free or already serving potocolom.
- **Docker:** the Docker daemon answers for your user, and Compose v2 is available.
- **GPU:** the NVIDIA driver and the NVIDIA Container Toolkit, or the AMD devices `/dev/kfd` and `/dev/dri` with a GPU target that the ROCm worker supports. It also reads the VRAM size.
- **Models:** the shipped models that your card can run at full residency, and the ones that load with offload instead.
- **Compose secrets:** the file `deploy/compose/.env`, which the next section describes.

On an NVIDIA host with the Container Toolkit, preflight also runs one throwaway container to prove that the GPU reaches Docker:

```bash
docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
```

The first run pulls `ubuntu:24.04`. To skip this test, set `POTOCOLOM_SKIP_GPU_TEST=1`:

```bash
POTOCOLOM_SKIP_GPU_TEST=1 scripts/preflight.sh
```

Preflight prints each result as ok, warn or fail. A fail is a blocking problem. Preflight then exits with a count of blocking problems, and you fix them and run it again. A warning does not stop the install.

## What it writes

Preflight writes one file, `deploy/compose/.env`. When the file is missing, preflight creates it from `deploy/compose/.env.example`. It then fills these keys when they are empty:

- `POSTGRES_PASSWORD`, with a value from `openssl rand -hex 32`. An empty value is filled only when Docker shows that no database volume exists. If a database exists and the value is empty, preflight stops and asks you to restore the saved password.
- `FLEET_SECRET`, with a value from `openssl rand -hex 32`. Preflight prints the value once. A worker on another machine needs a copy of it.
- `ROOT_KEYS`, the root key ring for accounts. It starts as one entry, version `1`, with 32 random bytes in base64 from `openssl rand -base64 32`. See [Accounts](/docs/self-hosting/accounts).

Preflight never replaces a non-empty value. The one exception is the example database password `change-me`. Preflight replaces it only when Docker answers and no database volume exists yet, because PostgreSQL reads the password once, when it creates the volume. When a database already uses `change-me`, preflight warns and prints the `ALTER ROLE` command that changes it.

The file holds the database password, `FLEET_SECRET` and the root key ring. Do not share it.

## What it does not do

- It installs nothing. It does not install Docker, the NVIDIA driver, the NVIDIA Container Toolkit or ROCm. When something is missing, it prints the command or the link for your distribution.
- It starts no service, and it does not pull the potocolom images.

Source contributors can run `scripts/preflight.sh --contributor` instead. That mode checks the development toolchain for `make init`.

## Next step

The GPU lines tell you which profile to use. Set up that profile in [GPU passthrough](/docs/self-hosting/gpu-passthrough). For the memory each model needs, see [Hardware requirements](/docs/guide/hardware-requirements). The preflight script is in [scripts/preflight.sh](https://github.com/Portocolom-Studio/potocolom/blob/main/scripts/preflight.sh).
