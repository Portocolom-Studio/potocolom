---
title: Quickstart
subtitle: Install a release with one command, then open the studio in a browser.
tab: guide
group: Get started
order: 2
source: docs/self-hosting.md "Supported hosts", "First run", "Accounts" ("First run"); README.md "Self-hosting"; docs/self-hosting.md "Installing models"
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
- It prints a one-use setup link. On a desktop session, it also opens the link in your browser.

## Choose how the install works

A new install starts unclaimed. Open the setup link that the installer printed. It has this form:

```text
http://localhost:8080/setup#<token>
```

The link is good for one hour and works once. Choose one of these:

- An administrator account, with an email and a password.
- No accounts. Every request then uses one local administrator. This suits a single operator on a trusted network.

Keep the setup link private until you have spent it. If it has expired or was lost, see [First run](/docs/self-hosting/first-run) for how to get a new one.

## Open the studio

Until you spend the setup link, the studio shows the setup page. After that, open the studio in a browser:

```text
http://localhost:8080
```

The API container serves the studio.

## Install models

A new install has no models. As an administrator, open Administration, then Models (`/app?view=admin&tab=models`). An empty model list in the Generate panel links there too. Each card shows the download size, whether the model fits your GPU, and a Suggested badge on the pick for it. Select models and download them. The worker refuses a download that would leave less than 5 GiB free, shows progress, and lets you cancel, retry or remove. Downloads are 2-7 GB for the SD and SDXL class models. A generation never downloads weights, so only installed models are offered.

> [!WARNING]
> If you choose no accounts, anyone who can reach the API has full access to the install. Keep the machine on a trusted network, or choose an administrator account. You can turn accounts on later, see [Accounts](/docs/self-hosting/accounts).

## Next steps

- [First run](/docs/self-hosting/first-run): the setup link, the choice, and what the studio does until you make it.
- [Installing a release](/docs/self-hosting/installing-a-release): pin a specific release, and read the full install notes.
- [Reaching the studio](/docs/self-hosting/reaching-the-studio): open the studio from other machines by setting the public URL.
