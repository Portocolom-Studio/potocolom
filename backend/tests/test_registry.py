"""Model registry merge rules across concurrent workers."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from app import realtime, registry
from app.auth import current_user
from app.manifests import Manifest
from app.tables import User


@pytest.fixture
def models_client(monkeypatch):
    test_app = FastAPI()
    test_app.include_router(registry.router)
    monkeypatch.setitem(
        test_app.dependency_overrides,
        current_user,
        lambda: User(email="registry@example.test", role="admin"),
    )
    with TestClient(test_app) as client:
        yield client


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


def test_models_endpoint_survives_a_malformed_upscale_manifest(models_client):
    # A manifest is worker-supplied, so neither `properties` nor the factor
    # `enum` is guaranteed to be the shape it should be. A bare `for` over a
    # non-list 500s this endpoint for every caller (issue #232).
    from app import realtime
    worker = realtime.Worker(
        id="w-bad", ws=None, realtime_slots=0,
        manifests=[Manifest(id="up-bad", name="up-bad", capabilities=["upscale"],
                            parameters={"properties": {"factor": {"enum": 4}}}),
                   Manifest(id="up-worse", name="up-worse", capabilities=["upscale"],
                            parameters={"properties": "not-a-dict"})],
    )
    realtime.workers[worker.id] = worker
    try:
        assert models_client.get("/api/v1/models").status_code == 200
    finally:
        realtime.workers.pop(worker.id, None)


def test_models_endpoint_survives_manifest_numbers_that_overflow_the_estimate(models_client):
    # The overflow is in the scaling arithmetic, not the int() calls: an
    # arbitrary-precision int divides to a float that cannot hold it, and a
    # merely huge one reaches round() as infinity (issue #232).
    from app import realtime
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
            assert models_client.get("/api/v1/models").status_code == 200, steps
        finally:
            realtime.workers.pop(worker.id, None)


def test_models_endpoint_round_trips_realtime_p95_ms(models_client):
    # A worker's calibration measurement must reach the browser: the studio
    # picker labels each realtime model with its measured frame cost.
    from app import realtime
    worker = realtime.Worker(
        id="w-rt", ws=None, realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT",
                            capabilities=["text_to_image", "image_to_image", "realtime"],
                            min_vram_gb=8, realtime_p95_ms=408)],
    )
    realtime.workers[worker.id] = worker
    try:
        payload = models_client.get("/api/v1/models").json()
    finally:
        realtime.workers.pop(worker.id, None)
    entry = next(m for m in payload if m["id"] == "vega-rt")
    assert entry["realtime_p95_ms"] == 408


def test_models_endpoint_without_measurement_reports_null(models_client):
    # A worker that never calibrated (the simulator) carries no measurement;
    # the field must come through as null rather than break the endpoint.
    from app import realtime
    worker = realtime.Worker(
        id="w-sim", ws=None, realtime_slots=1,
        manifests=[Manifest(id="sd-sim", name="Simulated",
                            capabilities=["text_to_image", "image_to_image", "realtime"])],
    )
    realtime.workers[worker.id] = worker
    try:
        payload = models_client.get("/api/v1/models").json()
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


def test_public_logs_a_narrowed_to_nothing_drop_once(caplog):
    # A model whose studio set intersects nothing vanishes from the studio,
    # so the drop must be said out loud: the model id and both capability
    # sets in one line an operator can grep for (issue #269).
    worker = realtime.Worker(
        id="w-drop", ws=None, realtime_slots=1,
        manifests=[Manifest(id="m-drop", name="Drop",
                            capabilities=["text_to_image"],
                            studio_capabilities=["upscale"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-drop"] = worker
        with caplog.at_level(logging.WARNING, logger="potocolom.registry"):
            assert "m-drop" not in registry.public()
        assert "m-drop" in registry.available()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)
    records = [r for r in caplog.records if r.name == "potocolom.registry"]
    assert len(records) == 1
    assert records[0].message == (
        "model m-drop dropped from the studio: advertised capabilities "
        "['text_to_image'] narrow to nothing against studio_capabilities ['upscale']"
    )


def test_public_does_not_repeat_the_drop_log(caplog):
    # public() runs on every /api/v1/models request and for_jobs() call, so
    # a drop must be logged once, not once per poll of the studio.
    worker = realtime.Worker(
        id="w-repeat", ws=None, realtime_slots=1,
        manifests=[Manifest(id="m-repeat", name="Repeat",
                            capabilities=["text_to_image"],
                            studio_capabilities=["upscale"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-repeat"] = worker
        with caplog.at_level(logging.WARNING, logger="potocolom.registry"):
            for _ in range(3):
                assert "m-repeat" not in registry.public()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)
    records = [r for r in caplog.records if r.name == "potocolom.registry"]
    assert len(records) == 1


def test_public_relogs_a_drop_after_the_model_recovers(caplog):
    # The suppression is per state, not permanent: a model that recovers and
    # then drops again is a second event an operator needs to see.
    worker = realtime.Worker(
        id="w-again", ws=None, realtime_slots=1,
        manifests=[Manifest(id="m-again", name="Again",
                            capabilities=["text_to_image", "realtime"],
                            studio_capabilities=["upscale"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-again"] = worker
        with caplog.at_level(logging.WARNING, logger="potocolom.registry"):
            assert "m-again" not in registry.public()
            worker.manifests[0].studio_capabilities = ["realtime"]
            assert "m-again" in registry.public()
            worker.manifests[0].studio_capabilities = ["upscale"]
            assert "m-again" not in registry.public()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)
    records = [r for r in caplog.records if r.name == "potocolom.registry"]
    assert len(records) == 2


def test_public_does_not_log_a_benchmark_only_model(caplog):
    # benchmark_only is the field doing its job, not a drop: a model absent
    # for that reason must stay silent even when its studio set would narrow
    # to nothing.
    worker = realtime.Worker(
        id="w-bench", ws=None, realtime_slots=0,
        manifests=[Manifest(id="m-bench", name="Bench",
                            capabilities=["text_to_image"],
                            benchmark_only=True,
                            studio_capabilities=["upscale"])],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-bench"] = worker
        with caplog.at_level(logging.WARNING, logger="potocolom.registry"):
            assert "m-bench" not in registry.public()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)
    records = [r for r in caplog.records if r.name == "potocolom.registry"]
    assert not records


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
            assert refused.json()["detail"] == "model is not offered for text_to_image"
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


def test_shipped_sdxl_turbo_is_a_public_conditioned_realtime_model(models_client):
    # The shipped manifest is the contract the studio picker builds on:
    # studio-visible (benchmark_only false), sketch-conditioned like vega-rt,
    # with a steps ceiling that covers the shipped 512-4step benchmark entries,
    # and advertised for realtime only since that is the only measured path.
    import json
    from pathlib import Path

    from app import realtime
    from app.manifests import Manifest

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
        payload = models_client.get("/api/v1/models").json()
    finally:
        realtime.workers.pop(worker.id, None)
    entry = next(m for m in payload if m["id"] == "sdxl-turbo")
    assert entry["capabilities"] == ["realtime"]


def test_shipped_defaults_cannot_both_win():
    # The queued default is the model whose `default` flag survives a
    # text_to_image-filtered list. sdxl-turbo declares `default` for the
    # realtime picker, but public() narrows its advertised capabilities to
    # realtime alone, so it cannot appear in a capability-filtered queued
    # list and its flag cannot win there. sdxl-base keeps the queued default.
    import json
    from pathlib import Path

    from app.manifests import Manifest

    models_dir = Path(__file__).resolve().parents[2].joinpath("worker", "models")
    turbo_raw = json.loads((models_dir / "sdxl-turbo.json").read_text())
    base_raw = json.loads((models_dir / "sdxl-base.json").read_text())
    turbo = Manifest(**turbo_raw)
    base = Manifest(**base_raw)
    assert turbo.default is True
    assert base.default is True
    for worker_id, manifest in {
        "w-turbo": turbo,
        "w-base": base,
    }.items():
        realtime.workers[worker_id] = realtime.Worker(
            id=worker_id, ws=None, realtime_slots=1, manifests=[manifest],
        )
    try:
        advertised_turbo = registry.public()["sdxl-turbo"]
        assert advertised_turbo.default is True
        assert advertised_turbo.capabilities == ["realtime"]
        advertised_base = registry.public()["sdxl-base"]
        assert advertised_base.default is True
        assert "text_to_image" in advertised_base.capabilities
        text_to_image_models = [
            manifest for manifest in registry.public().values()
            if "text_to_image" in manifest.capabilities
        ]
        assert "sdxl-turbo" not in {manifest.id for manifest in text_to_image_models}
        assert "sdxl-base" in {manifest.id for manifest in text_to_image_models}
    finally:
        realtime.workers.pop("w-turbo", None)
        realtime.workers.pop("w-base", None)


def test_benchmark_matrix_cells_validate_against_shipped_manifests():
    # The shipped matrices are the record of what was measured, and a
    # manifest's schema is the contract that keeps those cells runnable: a
    # steps ceiling or enum that refuses a measured variant fails every
    # benchmark run with 422. Every shipped matrix is checked, so the same
    # mistake on any model fails here instead of only the one literal the
    # per-manifest tests assert. The harness always submits prompt alongside
    # the variant's settings (scripts/generate.py), so the check mirrors that
    # shape; a matrix naming a model that is not shipped is skipped, not a
    # failure.
    import json
    from pathlib import Path

    from app.manifests import validate_params

    root = Path(__file__).resolve().parents[2]
    shipped = {
        manifest.id: manifest
        for manifest in (
            Manifest(**json.loads(path.read_text()))
            for path in sorted((root / "worker" / "models").glob("*.json"))
        )
    }
    for matrix_path in sorted((root / "scripts").glob("benchmark-matrix*.json")):
        matrix = json.loads(matrix_path.read_text())
        for group in ("models", "capped_commercial"):
            for entry in matrix.get(group, []):
                manifest = shipped.get(entry.get("id"))
                if manifest is None:
                    continue
                for variant in entry.get("variants", []):
                    params = {"prompt": "x"}
                    params.update(variant.get("params") or {})
                    refused = validate_params(manifest, params)
                    assert refused is None, (
                        f"{matrix_path.name}: model {entry['id']} variant "
                        f"{variant.get('label')!r} is refused by its shipped "
                        f"manifest: {refused}"
                    )


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


def test_out_of_range_realtime_p95_ms_normalises_to_none_at_the_manifest():
    # hello is the primary source of the advertised p95 and it reaches the
    # browser, but a measurement is cosmetic while a manifest is not: an
    # out-of-range number costs the label, not the worker's registration,
    # which is what refusing the manifest did (and the heartbeat carrying
    # the identical number is merely skipped).
    from app.manifests import FRAME_P95_MAX_MS, parse_manifests

    base = {"id": "m", "name": "m", "capabilities": ["realtime"]}
    for bad in (-1, 0, FRAME_P95_MAX_MS + 1):
        parsed = parse_manifests([{**base, "realtime_p95_ms": bad}])
        assert parsed[0].realtime_p95_ms is None, bad
    accepted = parse_manifests([{**base, "realtime_p95_ms": FRAME_P95_MAX_MS}])
    assert accepted[0].realtime_p95_ms == FRAME_P95_MAX_MS


def test_public_drops_offloaded_realtime_only_studio_capabilities(caplog):
    """measured_manifests strips realtime on a rung below full and leaves
    studio_capabilities: ["realtime"]. public() drops that hello-shaped
    manifest from the studio and logs it (issue #269).
    """
    worker = realtime.Worker(
        id="w-offload-rt", ws=None, realtime_slots=0,
        manifests=[Manifest(
            id="sdxl-turbo", name="SDXL Turbo",
            capabilities=["text_to_image", "image_to_image"],
            studio_capabilities=["realtime"],
        )],
    )
    saved = dict(realtime.workers)
    try:
        realtime.workers.clear()
        realtime.workers["w-offload-rt"] = worker
        with caplog.at_level(logging.WARNING, logger="potocolom.registry"):
            assert "sdxl-turbo" not in registry.public()
        assert "sdxl-turbo" in registry.available()
    finally:
        realtime.workers.clear()
        realtime.workers.update(saved)
    records = [r for r in caplog.records if r.name == "potocolom.registry"]
    assert len(records) == 1
    assert records[0].message == (
        "model sdxl-turbo dropped from the studio: advertised capabilities "
        "['text_to_image', 'image_to_image'] narrow to nothing against "
        "studio_capabilities ['realtime']"
    )


@pytest.fixture
def fleet():
    """An empty in-memory fleet, restored to whatever it held before."""
    saved = dict(realtime.workers)
    realtime.workers.clear()
    yield realtime.workers
    realtime.workers.clear()
    realtime.workers.update(saved)


def _rt_worker(worker_id: str, *, live: int | None = None,
               hello: int | None = None) -> realtime.Worker:
    return realtime.Worker(
        id=worker_id, ws=MagicMock(), realtime_slots=1,
        manifests=[Manifest(id="vega-rt", name="VegaRT", capabilities=["realtime"],
                            realtime_p95_ms=hello)],
        frame_p95_ms={} if live is None else {"vega-rt": live},
    )


def _join(fleet: dict, *workers: realtime.Worker) -> None:
    for worker in workers:
        fleet[worker.id] = worker


def test_available_advertises_the_only_worker_that_measured(fleet):
    # The first worker won manifest deduplication and has never measured, so
    # the label used to be null while a second worker was serving frames.
    _join(fleet, _rt_worker("w-first"), _rt_worker("w-second", live=415))
    assert registry.available()["vega-rt"].realtime_p95_ms == 415


def test_available_is_not_dragged_by_a_stale_first_worker(fleet):
    # Two measurements, one of them stale or throttled: the label must not be
    # the one that happens to sit on the deduplication winner.
    _join(fleet, _rt_worker("w-stale-rt", live=5000), _rt_worker("w-healthy", live=415))
    assert registry.available()["vega-rt"].realtime_p95_ms == 415


def test_a_worker_without_a_measurement_is_not_counted_at_all(fleet):
    # Not counted, rather than counted as nothing: with the silent worker
    # excluded the median of 400, 600 and 800 is 600, while treating it as a
    # sample of any value would move the middle off it.
    _join(fleet, _rt_worker("w-silent"), _rt_worker("w-400", live=400),
          _rt_worker("w-600", live=600), _rt_worker("w-800", live=800))
    assert registry.available()["vega-rt"].realtime_p95_ms == 600


def test_no_measurement_anywhere_leaves_the_label_absent(fleet):
    _join(fleet, _rt_worker("w-quiet-a"), _rt_worker("w-quiet-b"))
    assert registry.available()["vega-rt"].realtime_p95_ms is None


def test_an_even_count_takes_the_lower_middle_measurement(fleet):
    # Pinned: 400, the lower of the two middle samples, not the mean of 400
    # and 600 (a number no card produced) and not the upper middle.
    _join(fleet, _rt_worker("w-200", live=200), _rt_worker("w-400", live=400),
          _rt_worker("w-600", live=600), _rt_worker("w-800", live=800))
    assert registry.available()["vega-rt"].realtime_p95_ms == 400


def test_an_odd_count_takes_the_middle_of_the_sorted_measurements(fleet):
    # Joined slowest first, so a positional middle would report 100.
    _join(fleet, _rt_worker("w-900", live=900), _rt_worker("w-100", live=100),
          _rt_worker("w-500", live=500))
    assert registry.available()["vega-rt"].realtime_p95_ms == 500


def test_a_hello_calibration_counts_as_that_worker_s_measurement(fleet):
    # One worker measured live, the other only calibrated at hello; both are
    # measurements of the model on a card, so both are samples.
    _join(fleet, _rt_worker("w-hello", hello=408), _rt_worker("w-live", live=333))
    assert registry.available()["vega-rt"].realtime_p95_ms == 333
