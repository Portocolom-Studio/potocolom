---
title: GPU passthrough
subtitle: Give the containers access to an NVIDIA or AMD card.
tab: self-hosting
group: Install
order: 4
source: docs/self-hosting.md "GPU passthrough", "Supported hosts", "Hardware requirements"; scripts/gpu-profile.sh; deploy/compose/compose.yml
---

Passthrough lets the worker container use your GPU. The release has two GPU profiles: `gpu` for NVIDIA and `rocm` for AMD. A third profile, `api`, runs the API and database without a local worker. Each install runs one profile, and the profile decides which worker starts.

For the memory each model needs, see [Hardware requirements](/docs/guide/hardware-requirements).

## Profiles

| Host                                             | Profile | Local generation                             |
| ------------------------------------------------ | ------- | -------------------------------------------- |
| NVIDIA GPU with the Container Toolkit            | `gpu`   | CUDA worker                                  |
| Supported AMD GPU with `/dev/kfd` and `/dev/dri` | `rocm`  | ROCm worker                                  |
| No local GPU                                     | `api`   | None. Attach a GPU worker from another host. |

The installer runs `scripts/gpu-profile.sh`. The script checks NVIDIA first, by querying the device with `nvidia-smi`, because an AMD integrated GPU can also expose `/dev/kfd`. If no NVIDIA device answers, it checks for `/dev/kfd`. If neither exists, the release installer selects `api`.

To choose a profile yourself, set `POTOCOLOM_PROFILE` to `gpu`, `rocm` or `api`. Use it on each install or upgrade when the automatic choice is not suitable for the host:

```bash
curl -fsSL https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh | POTOCOLOM_PROFILE=api bash
```

Run one GPU profile, never both. The `gpu` and `rocm` workers share the model volumes.

A source checkout with no GPU uses a simulated worker from `scripts/compose-smoke.sh`. That worker is for source development only, and the release installer rejects it.

## Docker group

Both profiles need your user in the `docker` group. Add the user, then log out and back in:

```bash
sudo usermod -aG docker $USER
```

Without this group, every `docker compose` command fails with `permission denied ... /var/run/docker.sock`.

## NVIDIA

The worker image is built on CUDA 12.8. The host driver must be version 525.60.13 or newer. CUDA minor version compatibility lets the 12.8 runtime run on any 12.x driver at or above that version. A 535 driver that reports "CUDA Version: 12.2" in `nvidia-smi` is fine and does not need an upgrade. A driver below 525.60.13 makes the worker fail at import with a CUDA error.

To check for a driver, run `nvidia-smi`, or look for `/proc/driver/nvidia/version`. Do not use `lsmod | grep nvidia`. On a laptop with switchable graphics, the discrete GPU sleeps in `D3cold` with its modules unloaded, so `lsmod` prints nothing on a working install. Running `nvidia-smi` wakes the device.

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then restart the Docker daemon. If the driver or the toolkit is missing, preflight prints the commands for your distribution.
2. Prove that the driver, the toolkit and the Docker runtime work together:

   ```bash
   docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
   ```

3. Start the stack with the `gpu` profile. The compose file already reserves the GPU, so no other edit is needed. In a source checkout:

   ```bash
   docker compose -f deploy/compose/compose.yml --profile gpu up -d --build
   ```

   In a release install, run the same command with `--no-build` in place of `--build`. Release installs use the tagged images.

4. Verify that PyTorch sees the card. Run this from the install directory:

   ```bash
   docker compose -f deploy/compose/compose.yml exec worker python -c "import torch; print(torch.cuda.is_available())"
   ```

## AMD (ROCm)

1. Install the ROCm kernel driver on the host, so that `/dev/kfd` and `/dev/dri` exist. Add your user to the `video` group, and on some distributions to the `render` group as well. When `/dev/kfd` is missing, preflight prints the firmware install command for your distribution.
2. Check that your GPU is on the worker's supported list: gfx900, gfx906, gfx908, gfx90a, gfx942, gfx1030, gfx1100, gfx1101, gfx1102, gfx1200 and gfx1201. Preflight checks the target for you.
3. Start the stack with the `rocm` profile. In a source checkout, this builds the worker from `deploy/docker/Dockerfile.worker-rocm` and passes the devices through:

   ```bash
   docker compose -f deploy/compose/compose.yml --profile rocm up -d --build
   ```

   In a release install, use `--no-build` in place of `--build`.

4. RDNA3 consumer cards, such as the RX 7600 XT and cards of the gfx1102 class, work with the ROCm 6.3 or newer PyTorch wheels that the image installs. Do not set `HSA_OVERRIDE_GFX_VERSION`.
5. Verify that PyTorch sees the card:

   ```bash
   docker compose -f deploy/compose/compose.yml --profile rocm exec worker-rocm python -c "import torch; print(torch.cuda.is_available())"
   ```

## Choose the device

With no visibility setting, the worker uses the first visible GPU whose target its PyTorch build supports. It skips unsupported cards, and startup explains the supported targets when no card matches.

To choose a card yourself, set `CUDA_VISIBLE_DEVICES` (NVIDIA) or `HIP_VISIBLE_DEVICES` (AMD) in `deploy/compose/.env`. Leave the key out for automatic choice. An empty value hides every GPU, and the real worker then cannot start.

The worker uses one selected device for inference and memory reports. The default stack starts one worker, not one worker per card. These settings choose devices for the worker process. They do not block access to the device nodes.

## VRAM and the display

The worker keeps 1536 MiB of free VRAM in reserve before it picks a memory mode. Set `VRAM_RESERVE_MB` in `deploy/compose/.env` to change the reserve:

- Raise it when the same GPU also drives your display.
- Set it to `0` for a dedicated server GPU.

Each model lists a VRAM floor in its manifest. The floor is not a fixed gate. The worker measures the free VRAM and picks one of three modes: full residency, model offload, or group offload. Only full residency advertises the realtime capability. Offload can still run still-image jobs on a smaller card, at lower speed, when the chosen mode fits.

Preflight lists the shipped models that reach full residency on your card, so check that list before you pick a model.

## Next step

To run the worker on a different machine from the API, see [Remote GPU workers](/docs/self-hosting/remote-gpu-workers).
