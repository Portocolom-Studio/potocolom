"""Per-model memory ladder (docs/architecture.md, docs/decisions.md).

At load time the worker measures free VRAM and picks the highest rung that
fits, or the rung pinned by MEMORY_MODE. Only full residency advertises the
realtime capability.
"""

from typing import Literal

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


def model_offload_bytes(min_vram_gb: int) -> int:
    return max(int(min_vram_gb * GB * LARGEST_COMPONENT_FRACTION), GROUP_OFFLOAD_GB * GB)


def group_offload_bytes() -> int:
    return GROUP_OFFLOAD_GB * GB


def select_rung(
    min_vram_gb: int,
    free_bytes: int,
    memory_mode: MemoryMode,
    *,
    on_cpu: bool,
) -> MemoryRung:
    """Pick the highest rung that fits (group offload is the floor even when
    free memory is below its working set), or the pinned rung when not auto."""
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
    if free_bytes >= model_offload_bytes(min_vram_gb):
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
            select_rung(manifest.min_vram_gb, free_bytes, memory_mode, on_cpu=on_cpu),
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

    Cross-session frame batching is deferred (docs/decisions.md "GPU session
    density"), so N sessions serialize on the GPU lock. Admit the largest N
    whose serialized inter-frame time still meets the 2 fps floor, capped by
    the configured upper bound.
    """
    if configured <= 0 or p95_ms <= 0:
        return 0
    if p95_ms > bar_ms:
        return 0
    return min(configured, max(1, int(bar_ms // p95_ms)))


def rung_vram_bytes(min_vram_gb: int, rung: MemoryRung) -> int:
    if rung == "full":
        return full_residency_bytes(min_vram_gb)
    if rung == "model_offload":
        return model_offload_bytes(min_vram_gb)
    return group_offload_bytes()
