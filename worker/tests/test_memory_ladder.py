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
    slots_from_frame_ms,
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


def named_manifest(model_id: str, *, benchmark_only: bool = False,
                   capabilities: tuple[str, ...] = ("realtime",)) -> Manifest:
    return Manifest(id=model_id, name=model_id, capabilities=list(capabilities),
                    min_vram_gb=8, benchmark_only=benchmark_only)


def test_two_realtime_models_advertise_one_slot():
    """Calibration times one model but hello carries one realtime_slots for all
    of them, so a number above one would promise the bar for a model nobody
    measured: two slots earned at 240 ms become 560 ms per cycle once a session
    runs a 280 ms model (issue #285). One session cannot serialise against
    another, so a single slot is honest whichever model is chosen."""
    pair = [measured_wire_manifest(named_manifest("fast"), "full"),
            measured_wire_manifest(named_manifest("slow"), "full")]
    assert effective_realtime_slots(pair, 2) == 1
    assert effective_realtime_slots(pair, 1) == 1
    assert effective_realtime_slots(pair, 0) == 0


def test_one_realtime_model_keeps_the_configured_capacity():
    """Nothing is unmeasured then, so the measured model's own capacity stands
    even beside models that only take queued jobs."""
    queued_only = measured_wire_manifest(
        named_manifest("queued", capabilities=("text_to_image",)), "full")
    solo = measured_wire_manifest(named_manifest("only"), "full")
    assert effective_realtime_slots([solo, queued_only], 2) == 2


def test_a_benchmark_only_realtime_model_still_counts():
    """The studio never offers it, but the realtime endpoint opens any model in
    available(), and the repository ships sd-turbo as benchmark_only with
    realtime. A session on it would serialise unmeasured frames, so it counts
    towards the cap even though warmup would never warm it."""
    offered = measured_wire_manifest(named_manifest("offered"), "full")
    anchor = measured_wire_manifest(
        named_manifest("anchor", benchmark_only=True), "full")
    assert effective_realtime_slots([offered, anchor], 2) == 1


def test_effective_realtime_slots():
    full = measured_wire_manifest(manifest(), "full")
    offload = measured_wire_manifest(manifest(), "model_offload")
    assert effective_realtime_slots([full], 2) == 2
    assert effective_realtime_slots([offload], 2) == 0
    assert effective_realtime_slots([full, offload], 2) == 2
    assert effective_realtime_slots([full], 0) == 0


def test_slots_from_frame_ms():
    assert slots_from_frame_ms(200.0, 4) == 2
    assert slots_from_frame_ms(100.0, 2) == 2
    assert slots_from_frame_ms(100.0, 8) == 5
    assert slots_from_frame_ms(501.0, 4) == 0
    assert slots_from_frame_ms(250.0, 0) == 0
    assert slots_from_frame_ms(0.0, 4) == 0
