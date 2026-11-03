---
title: Hardware requirements
subtitle: What a machine needs to run potocolom, and which GPUs work.
tab: guide
group: Get started
order: 3
source: docs/self-hosting.md "Supported hosts", "Hardware requirements"
---

potocolom runs on one Linux x86_64 machine with Docker. To generate images on that machine, it needs an NVIDIA or AMD GPU. A machine with no GPU can run the API and use a GPU worker on another machine.

## Minimum and comfortable

- **GPU VRAM:** 8 GB minimum. 12-16 GB is comfortable for SDXL-class models at 1024 px, and it lets you switch models without eviction.
- **Disk:** 20 GB free minimum. The weights are 2-7 GB per model, plus your images. 50 GB or more is comfortable.
- **RAM:** 8 GB minimum. 16 GB is comfortable.

Each model manifest sets its own VRAM floor in `min_vram_gb`. The worker measures free VRAM before it loads a model. Run `scripts/preflight.sh` to see which shipped models fit on your card.

The worker keeps 1536 MiB of measured free VRAM in reserve. Raise `VRAM_RESERVE_MB` when the GPU also drives a display. Set it to `0` for a GPU that only runs potocolom.

## GPU vendors

| GPU    | Profile | What it needs                                                                                                                                                                                         |
| ------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| NVIDIA | `gpu`   | The NVIDIA Container Toolkit. A host driver of 525.60.13 or newer. The worker image uses CUDA 12.8.                                                                                                   |
| AMD    | `rocm`  | The ROCm kernel driver, so that `/dev/kfd` and `/dev/dri` exist. The GPU must be on the supported list: gfx900, gfx906, gfx908, gfx90a, gfx942, gfx1030, gfx1100, gfx1101, gfx1102, gfx1200, gfx1201. |
| None   | `api`   | A GPU worker on another host, with the shared `FLEET_SECRET`.                                                                                                                                         |

The installer checks for NVIDIA before AMD. Set `POTOCOLOM_PROFILE` to choose a profile yourself.

For RDNA3 consumer cards, such as the RX 7600 XT, the ROCm build supports the card directly. Do not set `HSA_OVERRIDE_GFX_VERSION`.

> [!NOTE]
> Run the `gpu` profile or the `rocm` profile, never both. They share the model volumes.

The release does not provide CPU inference, ARM images, or Intel or Apple GPU support.

To check a machine before you install, see [Checking a machine](/docs/self-hosting/checking-a-machine).
