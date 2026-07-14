from worker.manifests import Manifest
from worker.memory_ladder import (
    GB,
    capabilities_for_rung,
    effective_realtime_slots,
    full_residency_bytes,
    group_offload_bytes,
    measured_wire_manifest,
    measured_wire_manifests,
    model_offload_bytes,
    select_rung,
)


def manifest(min_vram_gb: int = 8) -> Manifest:
    return Manifest(
        id="test-model",
        name="Test",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        min_vram_gb=min_vram_gb,
    )


def test_select_rung_full_when_enough_vram():
    assert select_rung(8, full_residency_bytes(8), "auto", on_cpu=False) == "full"


def test_select_rung_model_offload_when_pipeline_too_large():
    free = model_offload_bytes(10) + 1
    assert select_rung(10, free, "auto", on_cpu=False) == "model_offload"


def test_select_rung_group_offload_when_tight():
    free = group_offload_bytes() - 1
    assert select_rung(10, free, "auto", on_cpu=False) == "group_offload"


def test_select_rung_respects_pin():
    assert select_rung(8, 100 * GB, "group_offload", on_cpu=False) == "group_offload"
    assert select_rung(8, 1, "full", on_cpu=False) == "full"


def test_select_rung_cpu_is_always_full():
    assert select_rung(8, 1, "auto", on_cpu=True) == "full"


def test_capabilities_drop_realtime_off_full():
    caps = ["text_to_image", "realtime"]
    assert capabilities_for_rung(caps, "full") == caps
    assert capabilities_for_rung(caps, "model_offload") == ["text_to_image"]
    assert capabilities_for_rung(caps, "group_offload") == ["text_to_image"]


def test_measured_wire_manifest():
    wire = measured_wire_manifest(manifest(), "model_offload")
    assert wire["id"] == "test-model"
    assert "realtime" not in wire["capabilities"]
    assert "text_to_image" in wire["capabilities"]


def test_measured_wire_manifests_per_model():
    small = manifest(min_vram_gb=4)
    large = manifest(min_vram_gb=16)
    large = large.model_copy(update={"id": "big"})
    wires = measured_wire_manifests([small, large], 6 * GB, "auto", on_cpu=False)
    by_id = {wire["id"]: wire for wire in wires}
    assert "realtime" in by_id["test-model"]["capabilities"]
    assert "realtime" not in by_id["big"]["capabilities"]


def test_effective_realtime_slots():
    full = measured_wire_manifest(manifest(), "full")
    offload = measured_wire_manifest(manifest(), "model_offload")
    assert effective_realtime_slots([full], 2) == 2
    assert effective_realtime_slots([offload], 2) == 0
    assert effective_realtime_slots([full, offload], 2) == 2
    assert effective_realtime_slots([full], 0) == 0
