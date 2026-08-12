"""Model registry merge rules across concurrent workers."""

import pytest
from unittest.mock import MagicMock

from app import realtime, registry
from app.manifests import Manifest


def _manifest(model_id: str, *, benchmark_only: bool) -> Manifest:
    return Manifest(
        id=model_id,
        name=model_id,
        capabilities=["text_to_image"],
        benchmark_only=benchmark_only,
    )


def test_available_prefers_studio_visible_over_stale_benchmark_only():
    stale = realtime.Worker(
        id="w-stale",
        ws=MagicMock(),
        manifests=[_manifest("sd-turbo", benchmark_only=True)],
        realtime_slots=0,
    )
    fresh = realtime.Worker(
        id="w-fresh",
        ws=MagicMock(),
        manifests=[_manifest("sd-turbo", benchmark_only=False)],
        realtime_slots=1,
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-stale"] = stale
        realtime.workers["w-fresh"] = fresh
        public = registry.public()
        assert "sd-turbo" in public
        assert public["sd-turbo"].benchmark_only is False
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_models_endpoint_survives_a_malformed_upscale_manifest():
    # A manifest is worker-supplied, so neither `properties` nor the factor
    # `enum` is guaranteed to be the shape it should be. A bare `for` over a
    # non-list 500s this endpoint for every caller (issue #232).
    from app import realtime
    from app.main import app
    from fastapi.testclient import TestClient

    worker = realtime.Worker(
        id="w-bad", ws=None, realtime_slots=0,
        manifests=[Manifest(id="up-bad", name="up-bad", capabilities=["upscale"],
                            parameters={"properties": {"factor": {"enum": 4}}}),
                   Manifest(id="up-worse", name="up-worse", capabilities=["upscale"],
                            parameters={"properties": "not-a-dict"})],
    )
    realtime.workers[worker.id] = worker
    try:
        assert TestClient(app).get("/api/v1/models").status_code == 200
    finally:
        realtime.workers.pop(worker.id, None)


def test_models_endpoint_survives_manifest_numbers_that_overflow_the_estimate():
    # The overflow is in the scaling arithmetic, not the int() calls: an
    # arbitrary-precision int divides to a float that cannot hold it, and a
    # merely huge one reaches round() as infinity (issue #232).
    from app import realtime
    from app.main import app
    from fastapi.testclient import TestClient

    for steps in (10 ** 400, 10 ** 308):
        worker = realtime.Worker(
            id="w-est", ws=None, realtime_slots=0,
            manifests=[Manifest(id="sdxl-base", name="sdxl-base",
                                capabilities=["text_to_image"],
                                parameters={"properties": {
                                    "steps": {"default": steps},
                                    "width": {"default": 1024},
                                    "height": {"default": 1024}}})],
        )
        realtime.workers[worker.id] = worker
        try:
            assert TestClient(app).get("/api/v1/models").status_code == 200, steps
        finally:
            realtime.workers.pop(worker.id, None)


def test_models_endpoint_round_trips_realtime_p95_ms():
    # A worker's calibration measurement must reach the browser: the studio
    # picker labels each realtime model with its measured frame cost.
    from app import realtime
    from app.main import app
    from fastapi.testclient import TestClient

    worker = realtime.Worker(
        id="w-rt", ws=None, realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=8, realtime_p95_ms=408)],
    )
    realtime.workers[worker.id] = worker
    try:
        payload = TestClient(app).get("/api/v1/models").json()
    finally:
        realtime.workers.pop(worker.id, None)
    entry = next(m for m in payload if m["id"] == "vega-rt")
    assert entry["realtime_p95_ms"] == 408


def test_models_endpoint_without_measurement_reports_null():
    # A worker that never calibrated (the simulator) carries no measurement;
    # the field must come through as null rather than break the endpoint.
    from app import realtime
    from app.main import app
    from fastapi.testclient import TestClient

    worker = realtime.Worker(
        id="w-sim", ws=None, realtime_slots=1,
        manifests=[Manifest(id="sd-sim", name="Simulated",
                            capabilities=["text_to_image", "image_to_image", "realtime"])],
    )
    realtime.workers[worker.id] = worker
    try:
        payload = TestClient(app).get("/api/v1/models").json()
    finally:
        realtime.workers.pop(worker.id, None)
    entry = next(m for m in payload if m["id"] == "sd-sim")
    assert entry["realtime_p95_ms"] is None


def test_available_replaces_hello_measurement_with_the_live_value():
    worker = realtime.Worker(
        id="w-live", ws=None, realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=8, realtime_p95_ms=408)],
        frame_p95_ms={"vega-rt": 333},
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-live"] = worker
        advertised = registry.available()["vega-rt"]
        # One field with one meaning: the live number replaced the
        # calibration value from hello, not a second field layered on.
        assert advertised.realtime_p95_ms == 333
        assert advertised.capabilities == ["text_to_image", "image_to_image", "realtime"]
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_available_keeps_calibration_when_no_live_measurement():
    worker = realtime.Worker(
        id="w-cal", ws=None, realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT",
                            capabilities=["realtime"], realtime_p95_ms=408)],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-cal"] = worker
        advertised = registry.available()["vega-rt"]
        assert advertised.realtime_p95_ms == 408
        # No live measurement means no copy: the worker's own manifest is
        # served unchanged.
        assert advertised is worker.manifests[0]
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_public_composes_live_measurement_with_capability_narrowing():
    # The case most likely to break: a manifest that is both
    # capability-narrowed (studio_capabilities) and live-measured must keep
    # both in public(). available() copies for the live number and public()
    # copies again for narrowing; one must not lose the other.
    worker = realtime.Worker(
        id="w-compose", ws=None, realtime_slots=1,
        manifests=[Manifest(id="sdxl-turbo", name="SDXL Turbo",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=10, studio_capabilities=["realtime"],
                            realtime_p95_ms=408)],
        frame_p95_ms={"sdxl-turbo": 333},
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-compose"] = worker
        live = registry.available()["sdxl-turbo"]
        assert live.realtime_p95_ms == 333
        assert live.capabilities == ["text_to_image", "image_to_image", "realtime"]
        narrowed = registry.public()["sdxl-turbo"]
        assert narrowed.capabilities == ["realtime"]
        assert narrowed.realtime_p95_ms == 333
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_public_narrows_studio_capabilities_but_available_keeps_full():
    worker = realtime.Worker(
        id="w-narrow", ws=None, realtime_slots=1,
        manifests=[Manifest(id="sdxl-turbo", name="SDXL Turbo",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=10, studio_capabilities=["realtime"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-narrow"] = worker
        narrowed = registry.public()["sdxl-turbo"]
        assert narrowed.capabilities == ["realtime"]
        assert narrowed.studio_capabilities == ["realtime"]
        # The full manifest stays the registry's: the benchmark page and the
        # realtime session path read available() and must not be narrowed.
        assert registry.available()["sdxl-turbo"].capabilities == [
            "text_to_image", "image_to_image", "realtime",
        ]
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_public_leaves_an_unrestricted_manifest_alone():
    worker = realtime.Worker(
        id="w-open", ws=None, realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=8)],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-open"] = worker
        advertised = registry.public()["vega-rt"]
        assert advertised.capabilities == ["text_to_image", "image_to_image", "realtime"]
        # No narrowing means no copy: the registry's manifest is the one served.
        assert advertised is registry.available()["vega-rt"]
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_public_omits_a_manifest_narrowed_to_nothing():
    # A model whose studio set intersects nothing would be advertised with an
    # empty capability list: listed in the studio but offered by no picker,
    # which reads as a removal the user never made.
    worker = realtime.Worker(
        id="w-none", ws=None, realtime_slots=1,
        manifests=[Manifest(id="m-none", name="None",
                            capabilities=["text_to_image"],
                            studio_capabilities=["upscale"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-none"] = worker
        assert "m-none" not in registry.public()
        # Still available to the benchmark page and the realtime path.
        assert "m-none" in registry.available()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)


@pytest.mark.db
def test_for_jobs_narrows_outside_benchmark_mode(monkeypatch):
    from app.settings import get_settings
    from app.main import app
    from fastapi.testclient import TestClient

    get_settings.cache_clear()
    worker = realtime.Worker(
        id="w-jobs", ws=None, realtime_slots=1,
        manifests=[Manifest(id="sdxl-turbo", name="SDXL Turbo",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=10, studio_capabilities=["realtime"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-jobs"] = worker
        # Queued generations read for_jobs(): realtime only, so the endpoint
        # refuses a prompt-only job even though the full manifest admits one
        # (issue #268).
        assert registry.for_jobs()["sdxl-turbo"].capabilities == ["realtime"]
        with TestClient(app) as client:
            refused = client.post("/api/v1/generations",
                                  json={"model_id": "sdxl-turbo",
                                        "params": {"prompt": "x"}})
            assert refused.status_code == 422
            assert "text_to_image" in refused.json()["detail"]
        monkeypatch.setenv("BENCHMARK_API", "1")
        get_settings.cache_clear()
        # The benchmark harness is exempt: it drives every shipped path.
        assert registry.for_jobs()["sdxl-turbo"].capabilities == [
            "text_to_image", "image_to_image", "realtime",
        ]
        with TestClient(app) as client:
            accepted = client.post("/api/v1/generations",
                                   json={"model_id": "sdxl-turbo",
                                         "params": {"prompt": "x"}})
            assert accepted.status_code == 202
    finally:
        monkeypatch.delenv("BENCHMARK_API", raising=False)
        get_settings.cache_clear()
        realtime.workers.clear()
        realtime.workers.update(saved)


def test_shipped_sdxl_turbo_is_a_public_conditioned_realtime_model():
    # The shipped manifest is the contract the studio picker builds on:
    # studio-visible (benchmark_only false), sketch-conditioned like vega-rt,
    # with a steps ceiling that covers the shipped 512-4step benchmark entries,
    # and advertised for realtime only since that is the only measured path.
    import json
    from pathlib import Path

    from app import realtime
    from app.main import app
    from app.manifests import Manifest
    from fastapi.testclient import TestClient

    raw = json.loads(Path(__file__).resolve().parents[2].joinpath(
        "worker", "models", "sdxl-turbo.json").read_text())
    manifest = Manifest(**raw)
    assert manifest.benchmark_only is False
    assert manifest.studio_capabilities == ["realtime"]
    assert "realtime" in manifest.capabilities
    # The backend model is extra="ignore": worker-only weight fields never
    # enter it, so they cannot leak into the /api/v1/models dump.
    assert "t2i_adapter" not in manifest.model_dump()
    properties = manifest.parameters["properties"]
    assert properties["structure_strength"] == {
        "type": "number", "minimum": 0, "maximum": 1.5, "default": 1.0,
    }
    assert properties["steps"]["maximum"] == 4
    assert properties["steps"]["default"] == 1

    worker = realtime.Worker(
        id="w-sdxl", ws=None, realtime_slots=1, manifests=[manifest],
    )
    realtime.workers[worker.id] = worker
    try:
        payload = TestClient(app).get("/api/v1/models").json()
    finally:
        realtime.workers.pop(worker.id, None)
    entry = next(m for m in payload if m["id"] == "sdxl-turbo")
    assert entry["capabilities"] == ["realtime"]


def test_manifest_parameters_must_be_json_storable():
    # parameters is persisted to JSONB, which has no NaN or Infinity, while
    # json.loads produces both. Without this the upsert killed the fleet
    # socket on every reconnect (issue #232).
    from app.manifests import parse_manifests

    good = [{"id": "m", "name": "m", "capabilities": ["text_to_image"],
             "parameters": {"properties": {"steps": {"default": 4}}}}]
    assert parse_manifests(good)[0].id == "m"

    bad = [{"id": "m", "name": "m", "capabilities": ["text_to_image"],
            "parameters": {"properties": {"steps": {"default": float("inf")}}}}]
    try:
        parse_manifests(bad)
    except ValueError as error:
        assert "JSON storage" in str(error)
    else:
        raise AssertionError("a non-finite manifest parameter was accepted")


def test_min_vram_gb_is_bounded_at_the_manifest():
    # models.min_vram_gb is int4; an unbounded value failed the upsert, which
    # ran before the fleet handler's cleanup could see the worker.
    from app.manifests import parse_manifests

    entry = {"id": "m", "name": "m", "capabilities": ["text_to_image"],
             "min_vram_gb": 3_000_000_000}
    try:
        parse_manifests([entry])
    except ValueError:
        pass
    else:
        raise AssertionError("an out-of-range min_vram_gb was accepted")


def test_realtime_p95_ms_is_bounded_at_the_manifest():
    # hello is the primary source of the advertised p95 and it reaches the
    # browser, so an out-of-range value must refuse the hello where manifests
    # are already rejected rather than be silently corrected.
    from app.manifests import FRAME_P95_MAX_MS, parse_manifests

    base = {"id": "m", "name": "m", "capabilities": ["realtime"]}
    for bad in (-1, 0, FRAME_P95_MAX_MS + 1):
        entry = {**base, "realtime_p95_ms": bad}
        try:
            parse_manifests([entry])
        except ValueError as error:
            assert "realtime_p95_ms" in str(error), bad
        else:
            raise AssertionError(f"an out-of-range realtime_p95_ms was accepted: {bad}")
    accepted = parse_manifests([{**base, "realtime_p95_ms": FRAME_P95_MAX_MS}])
    assert accepted[0].realtime_p95_ms == FRAME_P95_MAX_MS
