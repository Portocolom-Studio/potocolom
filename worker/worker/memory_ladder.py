"""Per-model memory ladder (docs/architecture.md, docs/decisions.md).

At load time the worker measures free VRAM and picks the highest rung that
fits, or the rung pinned by MEMORY_MODE. Only full residency advertises the
realtime capability.
"""

from typing import Literal, Protocol

from worker.manifests import Manifest

GB = 1024**3

MemoryMode = Literal["auto", "full", "model_offload", "group_offload"]
MemoryRung = Literal["full", "model_offload", "group_offload"]

# UNet is the largest single component; ~55% of full-pipeline VRAM is a
# stable heuristic across SD and SDXL class models.
LARGEST_COMPONENT_FRACTION = 0.55
# Group offload holds a few layer groups on the GPU.
GROUP_OFFLOAD_GB = 2
# Floor of the realtime bar (2 to 4 fps at 512 px): one frame every 500 ms.
REALTIME_BAR_MS = 500.0


def full_residency_bytes(min_vram_gb: int) -> int:
    return min_vram_gb * GB


def model_offload_bytes(min_vram_gb: int, model_offload_vram_gb: int | None = None) -> int:
    if model_offload_vram_gb is not None:
        return model_offload_vram_gb * GB
    return max(int(min_vram_gb * GB * LARGEST_COMPONENT_FRACTION), GROUP_OFFLOAD_GB * GB)


def group_offload_bytes() -> int:
    return GROUP_OFFLOAD_GB * GB


def select_rung(
    min_vram_gb: int,
    free_bytes: int,
    memory_mode: MemoryMode,
    *,
    on_cpu: bool,
    model_offload_vram_gb: int | None = None,
) -> MemoryRung:
    """Predict a rung; loading code must still check its VRAM estimate.

    Group offload is the lowest predicted rung even below its 2 GiB estimate,
    so manifests can drop realtime honestly while admission refuses the load.
    """
    if on_cpu:
        return "full"
    if memory_mode == "full":
        return "full"
    if memory_mode == "model_offload":
        return "model_offload"
    if memory_mode == "group_offload":
        return "group_offload"
    if free_bytes >= full_residency_bytes(min_vram_gb):
        return "full"
    if free_bytes >= model_offload_bytes(min_vram_gb, model_offload_vram_gb):
        return "model_offload"
    return "group_offload"


def capabilities_for_rung(capabilities: list[str], rung: MemoryRung) -> list[str]:
    if rung == "full":
        return list(capabilities)
    return [cap for cap in capabilities if cap != "realtime"]


def measured_wire_manifest(manifest: Manifest, rung: MemoryRung) -> dict:
    wire = manifest.wire()
    wire["capabilities"] = capabilities_for_rung(manifest.capabilities, rung)
    return wire


def measured_wire_manifests(
    manifests: list[Manifest],
    free_bytes: int,
    memory_mode: MemoryMode,
    *,
    on_cpu: bool,
) -> list[dict]:
    return [
        measured_wire_manifest(
            manifest,
            select_rung(
                manifest.min_vram_gb, free_bytes, memory_mode, on_cpu=on_cpu,
                model_offload_vram_gb=manifest.model_offload_vram_gb,
            ),
        )
        for manifest in manifests
    ]


def effective_realtime_slots(wire_manifests: list[dict], configured: int) -> int:
    if configured <= 0:
        return 0
    if any("realtime" in manifest["capabilities"] for manifest in wire_manifests):
        return configured
    return 0


def slots_from_frame_ms(
    p95_ms: float,
    configured: int,
    *,
    bar_ms: float = REALTIME_BAR_MS,
) -> int:
    """Map a measured single-frame p95 to concurrent realtime slots.

    This is the serialized fallback when no measured batch curve exists.
    """
    if configured <= 0 or p95_ms <= 0:
        return 0
    if p95_ms > bar_ms:
        return 0
    return min(configured, max(1, int(bar_ms // p95_ms)))


def slots_from_batch_curve(
    curve: list[int],
    configured: int,
    *,
    bar_ms: float = REALTIME_BAR_MS,
) -> int:
    if configured <= 0 or not curve:
        return 0
    count = 0
    for index, p95 in enumerate(curve):
        if index + 1 > configured or p95 <= 0 or p95 > bar_ms:
            break
        count = index + 1
    return count


def rung_vram_bytes(
    min_vram_gb: int, rung: MemoryRung, model_offload_vram_gb: int | None = None,
) -> int:
    if rung == "full":
        return full_residency_bytes(min_vram_gb)
    if rung == "model_offload":
        return model_offload_bytes(min_vram_gb, model_offload_vram_gb)
    return group_offload_bytes()


class VRAMCapacityError(ValueError):
    """The selected model rung cannot fit while preserving the VRAM reserve."""


class AdmissionGpu(Protocol):
    def free_bytes(self) -> int:
        """Usable VRAM right now, after the reserve. Measured fresh on every call."""

    def reclaim_cache(self) -> None:
        """Return the allocator cache to the device."""

    def evict_cold(self, required_bytes: int) -> None:
        """Evict the coldest other models until free_bytes() >= required_bytes or none are left."""


def admit(
    manifest: Manifest,
    memory_mode: MemoryMode,
    gpu: AdmissionGpu,
    *,
    on_cpu: bool,
    resident: bool,
    pinned_rung: MemoryRung | None,
) -> MemoryRung | None:
    """Return the rung to load at, reclaiming or evicting as needed.

    Returns None when the load shares weights that are already resident.
    Raises VRAMCapacityError when the load cannot fit after the reserve.
    """
    upscale = "upscale" in manifest.capabilities
    shared = resident and not upscale
    target_rung: MemoryRung = "full" if memory_mode == "auto" or upscale else memory_mode
    target = 1 if shared else max(1, rung_vram_bytes(
        manifest.min_vram_gb, target_rung, manifest.model_offload_vram_gb,
    ))
    if gpu.free_bytes() < target:
        gpu.reclaim_cache()
    if shared:
        # A new mode may add a small adapter or decoder while sharing the
        # resident model weights. Do not charge the whole model rung again,
        # but require some usable VRAM before starting that allocation.
        if gpu.free_bytes() <= 0:
            gpu.evict_cold(1)
        if gpu.free_bytes() <= 0:
            raise VRAMCapacityError(
                f"cannot load another pipeline mode for {manifest.id}: "
                "no usable VRAM remains after reserve"
            )
        return None
    if memory_mode == "auto" and pinned_rung is None:
        # Prefer evicting cold residents over degrading the new model to
        # an offload rung: make room for full residency before picking.
        wanted = rung_vram_bytes(manifest.min_vram_gb, "full", manifest.model_offload_vram_gb)
        if gpu.free_bytes() < wanted:
            gpu.evict_cold(wanted)
    rung: MemoryRung
    if pinned_rung is not None:
        rung = pinned_rung
    elif upscale:
        # Upscalers are plain nn.Modules run tiled; the offload rungs are
        # diffusers pipeline mechanics, so they always load fully resident.
        rung = "full"
    else:
        rung = select_rung(
            manifest.min_vram_gb, gpu.free_bytes(), memory_mode, on_cpu=on_cpu,
            model_offload_vram_gb=manifest.model_offload_vram_gb,
        )
    required = max(1, rung_vram_bytes(
        manifest.min_vram_gb, rung, manifest.model_offload_vram_gb,
    ))
    if gpu.free_bytes() < required:
        gpu.evict_cold(required)
    available = gpu.free_bytes()
    if available < required:
        raise VRAMCapacityError(
            f"cannot load {manifest.id} on the {rung} rung: "
            f"requires {required} bytes of usable VRAM, "
            f"but only {available} are available after reserve"
        )
    return rung
