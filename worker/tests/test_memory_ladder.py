import pytest

from worker.manifests import Manifest
from worker.memory_ladder import (
    GB,
    VRAMCapacityError,
    admit,
    capabilities_for_rung,
    effective_realtime_slots,
    full_residency_bytes,
    group_offload_bytes,
    measured_wire_manifest,
    measured_wire_manifests,
    model_offload_bytes,
    select_rung,
    slots_from_batch_curve,
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


def test_group_offload_has_a_two_gibibyte_admission_floor():
    assert group_offload_bytes() == 2 * GB


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


def test_measured_wire_keeps_studio_capabilities_when_realtime_drops():
    """A rung below full strips realtime from capabilities and leaves
    studio_capabilities alone. That wire shape is what the API drops from
    the studio (issue #269).
    """
    model = Manifest(
        id="sdxl-turbo",
        name="SDXL Turbo",
        capabilities=["text_to_image", "image_to_image", "realtime"],
        studio_capabilities=["realtime"],
        min_vram_gb=10,
    )
    wire = measured_wire_manifest(model, "model_offload")
    assert "realtime" not in wire["capabilities"]
    assert wire["studio_capabilities"] == ["realtime"]


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


def test_slots_from_frame_ms():
    assert slots_from_frame_ms(200.0, 4) == 2
    assert slots_from_frame_ms(100.0, 2) == 2
    assert slots_from_frame_ms(100.0, 8) == 5
    assert slots_from_frame_ms(501.0, 4) == 0
    assert slots_from_frame_ms(250.0, 0) == 0
    assert slots_from_frame_ms(0.0, 4) == 0


def test_slots_from_batch_curve():
    assert slots_from_batch_curve([139, 309, 404, 501], 8) == 3
    assert slots_from_batch_curve([139, 309, 404, 501], 2) == 2
    assert slots_from_batch_curve([501], 8) == 0
    assert slots_from_batch_curve([], 8) == 0
    assert slots_from_batch_curve([139], 0) == 0


FREE = ("free",)


class FakeGpu:
    """Plain stand-in for AdmissionGpu that records every call in order.

    free_bytes() is recorded too, so tests can pin how often admit measures.
    """

    def __init__(
        self, free: int, *, reclaimable: int = 0, evictable: list[int] | None = None
    ) -> None:
        self.free = free
        self.reclaimable = reclaimable
        self.evictable = list(evictable or [])
        self.calls: list[tuple] = []

    def free_bytes(self) -> int:
        self.calls.append(FREE)
        return self.free

    def reclaim_cache(self) -> None:
        self.calls.append(("reclaim",))
        self.free += self.reclaimable
        self.reclaimable = 0

    def evict_cold(self, required_bytes: int) -> None:
        self.calls.append(("evict", required_bytes))
        while self.free < required_bytes and self.evictable:
            self.free += self.evictable.pop(0)


def effects(gpu: FakeGpu) -> list[tuple]:
    """The reclaim and evict calls only, for tests that are not about measuring."""
    return [call for call in gpu.calls if call != FREE]


def admission_manifest(
    min_vram_gb: int = 8,
    *,
    capabilities: list[str] | None = None,
    model_offload_vram_gb: int | None = None,
) -> Manifest:
    return Manifest(
        id="test-model",
        name="Test",
        capabilities=capabilities if capabilities is not None else ["text_to_image"],
        min_vram_gb=min_vram_gb,
        model_offload_vram_gb=model_offload_vram_gb,
    )


def test_admit_full_with_enough_free_vram_in_auto():
    gpu = FakeGpu(16 * GB)
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "full"
    assert effects(gpu) == []


def test_admit_cpu_is_full_and_touches_nothing():
    gpu = FakeGpu(1 << 60)
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=True, resident=False, pinned_rung=None)
    assert rung == "full"
    assert effects(gpu) == []


def test_admit_reclaim_alone_makes_room_for_full():
    gpu = FakeGpu(4 * GB, reclaimable=6 * GB)
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "full"
    assert effects(gpu) == [("reclaim",)]


def test_admit_evicts_for_full_when_reclaim_is_not_enough():
    gpu = FakeGpu(4 * GB, reclaimable=1 * GB, evictable=[2 * GB, 2 * GB])
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "full"
    assert ("evict", full_residency_bytes(8)) in effects(gpu)


def test_admit_falls_back_to_model_offload_when_eviction_cannot_reach_full():
    gpu = FakeGpu(model_offload_bytes(8))
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "model_offload"
    first_evict = next(call for call in effects(gpu) if call[0] == "evict")
    assert first_evict == ("evict", full_residency_bytes(8))


def test_admit_group_offload_at_exactly_the_two_gibibyte_floor():
    gpu = FakeGpu(group_offload_bytes())
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "group_offload"


@pytest.mark.parametrize("free", [group_offload_bytes() - 1, 0])
def test_admit_below_group_offload_floor_raises(free):
    gpu = FakeGpu(free)
    with pytest.raises(VRAMCapacityError, match="usable VRAM"):
        admit(
            admission_manifest(8),
            "auto",
            gpu,
            on_cpu=False,
            resident=False,
            pinned_rung=None,
        )


def test_admit_zero_min_vram_still_requires_one_byte():
    gpu = FakeGpu(0)
    with pytest.raises(VRAMCapacityError, match="requires 1 bytes"):
        admit(
            admission_manifest(0),
            "auto",
            gpu,
            on_cpu=False,
            resident=False,
            pinned_rung=None,
        )


def test_admit_refusal_message_is_exact():
    gpu = FakeGpu(13 * GB)
    expected = (
        "cannot load test-model on the model_offload rung: "
        "requires 15032385536 bytes of usable VRAM, "
        "but only 13958643712 are available after reserve"
    )
    with pytest.raises(VRAMCapacityError) as excinfo:
        admit(
            admission_manifest(32, model_offload_vram_gb=14),
            "model_offload",
            gpu,
            on_cpu=False,
            resident=False,
            pinned_rung=None,
        )
    assert str(excinfo.value) == expected


def test_admit_honours_pinned_rung_even_with_room_for_full():
    gpu = FakeGpu(16 * GB)
    rung = admit(
        admission_manifest(8),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung="model_offload",
    )
    assert rung == "model_offload"
    assert effects(gpu) == []


def test_admit_pinned_rung_skips_evicting_for_full():
    gpu = FakeGpu(3 * GB, evictable=[GB] * 8)
    rung = admit(
        admission_manifest(8),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung="group_offload",
    )
    assert rung == "group_offload"
    assert ("evict", full_residency_bytes(8)) not in effects(gpu)


def test_admit_shared_load_with_free_vram_returns_none():
    gpu = FakeGpu(1 * GB)
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=True, pinned_rung=None)
    assert rung is None
    assert effects(gpu) == []


def test_admit_shared_load_with_no_free_vram_evicts_one_byte_and_shares():
    gpu = FakeGpu(0, evictable=[GB])
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=True, pinned_rung=None)
    assert rung is None
    assert effects(gpu) == [("reclaim",), ("evict", 1)]


def test_admit_shared_load_with_nothing_to_free_raises():
    gpu = FakeGpu(0)
    with pytest.raises(VRAMCapacityError) as excinfo:
        admit(
            admission_manifest(8),
            "auto",
            gpu,
            on_cpu=False,
            resident=True,
            pinned_rung=None,
        )
    assert str(excinfo.value) == (
        "cannot load another pipeline mode for test-model: no usable VRAM remains after reserve"
    )


def test_resident_upscaler_is_not_shared_and_gets_full():
    gpu = FakeGpu(16 * GB)
    rung = admit(
        admission_manifest(8, capabilities=["upscale"]),
        "auto",
        gpu,
        on_cpu=False,
        resident=True,
        pinned_rung=None,
    )
    assert rung == "full"


def test_resident_upscaler_with_too_little_free_raises():
    gpu = FakeGpu(4 * GB)
    with pytest.raises(VRAMCapacityError, match="on the full rung"):
        admit(
            admission_manifest(8, capabilities=["upscale"]),
            "auto",
            gpu,
            on_cpu=False,
            resident=True,
            pinned_rung=None,
        )


def test_upscaler_is_full_even_in_group_offload_mode():
    gpu = FakeGpu(16 * GB)
    rung = admit(
        admission_manifest(8, capabilities=["upscale"]),
        "group_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "full"


def test_pinned_memory_mode_sets_the_reclaim_target():
    gpu = FakeGpu(3 * GB, reclaimable=GB)
    rung = admit(
        admission_manifest(8),
        "group_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "group_offload"
    assert effects(gpu) == []


def test_reclaim_runs_before_any_eviction():
    gpu = FakeGpu(4 * GB, reclaimable=1 * GB, evictable=[2 * GB, 2 * GB])
    admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert effects(gpu)[0] == ("reclaim",)
    assert effects(gpu)[1][0] == "evict"


def test_admit_passes_model_offload_vram_through():
    # With the explicit 2 GiB offload estimate, 3 GiB free fits model offload.
    # The default estimate for 8 GiB would be about 4.7 GiB and fall to group.
    gpu = FakeGpu(3 * GB)
    rung = admit(
        admission_manifest(8, model_offload_vram_gb=2),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "model_offload"
    assert select_rung(8, 3 * GB, "auto", on_cpu=False) == "group_offload"


def test_admit_call_sequence_auto_plenty_free():
    gpu = FakeGpu(16 * GB)
    admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert gpu.calls == [FREE, FREE, FREE, FREE, FREE]


def test_admit_call_sequence_auto_upscaler_plenty_free():
    gpu = FakeGpu(16 * GB)
    admit(
        admission_manifest(8, capabilities=["upscale"]),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert gpu.calls == [FREE, FREE, FREE, FREE]


def test_admit_call_sequence_pinned_rung_plenty_free():
    gpu = FakeGpu(16 * GB)
    admit(
        admission_manifest(8),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung="model_offload",
    )
    assert gpu.calls == [FREE, FREE, FREE]


def test_admit_call_sequence_model_offload_plenty_free():
    gpu = FakeGpu(16 * GB)
    admit(
        admission_manifest(8),
        "model_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert gpu.calls == [FREE, FREE, FREE, FREE]


def test_admit_call_sequence_model_offload_upscaler_plenty_free():
    gpu = FakeGpu(16 * GB)
    admit(
        admission_manifest(8, capabilities=["upscale"]),
        "model_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert gpu.calls == [FREE, FREE, FREE]


def test_admit_call_sequence_shared_load_with_free_vram():
    gpu = FakeGpu(1 * GB)
    admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=True, pinned_rung=None)
    assert gpu.calls == [FREE, FREE, FREE]


def test_admit_call_sequence_shared_load_reclaim_then_one_byte_evict():
    gpu = FakeGpu(0, evictable=[GB])
    admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=True, pinned_rung=None)
    assert gpu.calls == [FREE, ("reclaim",), FREE, ("evict", 1), FREE]


def test_admit_call_sequence_auto_short_of_full_reclaim_and_evict():
    gpu = FakeGpu(4 * GB, reclaimable=GB, evictable=[2 * GB, 2 * GB])
    admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert gpu.calls == [
        FREE,
        ("reclaim",),
        FREE,
        ("evict", full_residency_bytes(8)),
        FREE,
        FREE,
        FREE,
    ]


def test_admit_final_eviction_makes_room_for_the_offload_rung():
    # Not auto, so the evict-for-full step never runs; only the final
    # eviction (for the rung's own bytes) can make room here.
    gpu = FakeGpu(3 * GB, evictable=[2 * GB])
    rung = admit(
        admission_manifest(8),
        "model_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "model_offload"
    assert effects(gpu) == [("reclaim",), ("evict", model_offload_bytes(8))]


def test_admit_final_eviction_makes_room_for_pinned_full():
    gpu = FakeGpu(4 * GB, evictable=[8 * GB])
    rung = admit(
        admission_manifest(8),
        "auto",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung="full",
    )
    assert rung == "full"
    assert effects(gpu) == [("reclaim",), ("evict", full_residency_bytes(8))]


def test_admit_refuses_after_final_eviction_frees_nothing():
    gpu = FakeGpu(3 * GB)
    with pytest.raises(VRAMCapacityError) as excinfo:
        admit(
            admission_manifest(8),
            "model_offload",
            gpu,
            on_cpu=False,
            resident=False,
            pinned_rung=None,
        )
    assert effects(gpu)[-1] == ("evict", model_offload_bytes(8))
    assert str(excinfo.value) == (
        "cannot load test-model on the model_offload rung: "
        f"requires {model_offload_bytes(8)} bytes of usable VRAM, "
        f"but only {3 * GB} are available after reserve"
    )


def test_admit_shared_load_treats_negative_free_as_no_vram():
    # A negative reading must count as no free VRAM, not as spare room: the
    # shared path evicts one byte, and refuses if that frees nothing.
    gpu = FakeGpu(-1, evictable=[GB])
    assert (
        admit(
            admission_manifest(8),
            "auto",
            gpu,
            on_cpu=False,
            resident=True,
            pinned_rung=None,
        )
        is None
    )
    assert effects(gpu) == [("reclaim",), ("evict", 1)]
    with pytest.raises(VRAMCapacityError, match="another pipeline mode"):
        admit(
            admission_manifest(8),
            "auto",
            FakeGpu(-1),
            on_cpu=False,
            resident=True,
            pinned_rung=None,
        )


def test_admit_auto_reclaims_toward_full_before_choosing_a_lower_rung():
    gpu = FakeGpu(5 * GB, reclaimable=GB)
    rung = admit(admission_manifest(8), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "model_offload"
    assert effects(gpu) == [("reclaim",), ("evict", full_residency_bytes(8))]


def test_admit_upscaler_reclaims_toward_full_even_in_group_offload_mode():
    gpu = FakeGpu(4 * GB, reclaimable=5 * GB)
    rung = admit(
        admission_manifest(8, capabilities=["upscale"]),
        "group_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "full"
    assert effects(gpu) == [("reclaim",)]


def test_admit_reclaims_and_evicts_only_when_strictly_short():
    # Exactly enough for the target is not short, so nothing is called.
    gpu = FakeGpu(full_residency_bytes(8))
    assert (
        admit(
            admission_manifest(8),
            "auto",
            gpu,
            on_cpu=False,
            resident=False,
            pinned_rung=None,
        )
        == "full"
    )
    assert effects(gpu) == []
    gpu = FakeGpu(group_offload_bytes())
    rung = admit(
        admission_manifest(8),
        "group_offload",
        gpu,
        on_cpu=False,
        resident=False,
        pinned_rung=None,
    )
    assert rung == "group_offload"
    assert effects(gpu) == []


def test_admit_zero_min_vram_targets_one_byte_for_reclaim():
    gpu = FakeGpu(0, reclaimable=GB)
    rung = admit(admission_manifest(0), "auto", gpu, on_cpu=False, resident=False, pinned_rung=None)
    assert rung == "full"
    assert effects(gpu) == [("reclaim",)]


def test_vram_capacity_error_is_a_value_error():
    assert issubclass(VRAMCapacityError, ValueError)
