"""Model registry merge rules across concurrent workers."""

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
