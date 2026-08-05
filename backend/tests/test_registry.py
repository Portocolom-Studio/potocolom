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
