---
title: Quickstart
subtitle: Install a release with one command, then open the studio in a browser.
tab: guide
group: Get started
order: 2
source: docs/self-hosting.md "Supported hosts", "First run"; README.md "Self-hosting"
---

## Prerequisites

Check these on the machine before you install.

- Linux on x86_64. The release has no ARM images, and it does not provide CPU inference or Intel or Apple GPU support.
- Docker with Compose v2.27.2 or newer.
- `curl`, `tar`, `sha256sum`, `openssl`, `flock` (from util-linux), and GNU coreutils (`cksum`, `stat`, `readlink`).
- One of these hosts:
  - An NVIDIA GPU with the NVIDIA Container Toolkit (profile `gpu`).
  - A supported AMD GPU with `/dev/kfd` and `/dev/dri` (profile `rocm`).
  - No local GPU. This uses profile `api`, and a GPU worker on another host does the generation.

To check your machine against this list, see [Checking a machine](/docs/self-hosting/checking-a-machine).

## Install

Run this one command:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | bash
```

The installer does these steps:

- It checks the downloaded bundle against the release's `SHA256SUMS` file.
- It installs into `~/potocolom`. Set `POTOCOLOM_DIR` to use another directory.
- It looks for an NVIDIA GPU first, then an AMD GPU. With no GPU, it selects the `api` profile. Set `POTOCOLOM_PROFILE` to `gpu`, `rocm` or `api` to choose yourself.
- It pulls the released images and starts the stack.
- It waits up to 120 seconds for the local API and store to pass `/api/v1/ready`. It does not wait for GPU warmup or model downloads.
- If the API is not ready, it stops with an error and prints a logs command. Your data stays in place. Fix the cause and run the script again.

## Open the studio

Open the studio in a browser:

```text
http://localhost:8080
```

The API container serves the studio. The first generation for each model downloads its weights from Hugging Face. Those weights are 2-7 GB for the SD and SDXL class models, so the first job waits in the queue until the download ends.

> [!WARNING]
> The install starts with no logins. Every request uses one local administrator account. Keep the machine on a trusted network until you turn on accounts.

## Next steps

- [Installing a release](/docs/self-hosting/installing-a-release): pin a specific release, and read the full install notes.
- [Reaching the studio](/docs/self-hosting/reaching-the-studio): open the studio from other machines by setting the public URL.
