"""Models installed on worker hardware (issue #793): suggestions, report parsing,
the admin routes and the worker gate on the fleet socket."""

import time

import pytest
from app import realtime, worker_models
from app.manifests import parse_manifests
from app.main import app
from app.protocol6 import validate_message
from fastapi.testclient import TestClient

from tests.test_fleet_protocol6 import FLEET_HEADERS, drop_worker_rows
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

__all__ = ["accounts"]

GIB = 2**30


def entry(model_id, capabilities, *, min_vram_gb, default=False, benchmark_only=False,
          needs_token=False) -> dict:
    return {
        "id": model_id,
        "capabilities": capabilities,
        "min_vram_gb": min_vram_gb,
        "default": default,
        "benchmark_only": benchmark_only,
        "needs_token": needs_token,
    }


CATALOG = [
    entry("rt-default", ["realtime"], min_vram_gb=10, default=True),
    entry("rt-small", ["realtime"], min_vram_gb=4),
    entry("rt-bench", ["realtime"], min_vram_gb=8, benchmark_only=True),
    entry("t2i-default", ["text_to_image"], min_vram_gb=10, default=True),
    entry("t2i-big", ["text_to_image"], min_vram_gb=14),
    entry("t2i-gated", ["text_to_image"], min_vram_gb=12, needs_token=True),
    entry("up-fast", ["upscale"], min_vram_gb=1),
    entry("up-big", ["upscale"], min_vram_gb=4),
]


@pytest.mark.parametrize("vram_gb, expected", [
    (0, {"realtime": None, "text_to_image": None, "upscale": None}),
    (4, {"realtime": "rt-small", "text_to_image": None, "upscale": "up-big"}),
    (8, {"realtime": "rt-small", "text_to_image": None, "upscale": "up-big"}),
    (10, {"realtime": "rt-default", "text_to_image": "t2i-default", "upscale": "up-big"}),
    (12, {"realtime": "rt-default", "text_to_image": "t2i-default", "upscale": "up-big"}),
    (16, {"realtime": "rt-default", "text_to_image": "t2i-default", "upscale": "up-big"}),
    (24, {"realtime": "rt-default", "text_to_image": "t2i-default", "upscale": "up-big"}),
    (48, {"realtime": "rt-default", "text_to_image": "t2i-default", "upscale": "up-big"}),
])
def test_suggestions_by_vram_never_offer_benchmark_or_gated_models(vram_gb, expected):
    assert worker_models.suggest(CATALOG, vram_gb * GIB) == expected


def test_suggestion_without_a_known_vram_offers_nothing():
    assert worker_models.suggest(CATALOG, None) == {
        "realtime": None, "text_to_image": None, "upscale": None}


def test_a_default_that_does_not_fit_yields_the_largest_model_that_does():
    catalog = [
        entry("t2i-default-big", ["text_to_image"], min_vram_gb=14, default=True),
        entry("t2i-mid", ["text_to_image"], min_vram_gb=10),
        entry("t2i-small", ["text_to_image"], min_vram_gb=6),
    ]
    assert worker_models.suggest(catalog, 12 * GIB)["text_to_image"] == "t2i-mid"


def test_suggestion_ties_break_on_the_smaller_id():
    catalog = [
        entry("up-b", ["upscale"], min_vram_gb=2),
        entry("up-a", ["upscale"], min_vram_gb=2),
    ]
    assert worker_models.suggest(catalog, 8 * GIB)["upscale"] == "up-a"


def manifest_row(model_id="pipe-test", **flags) -> dict:
    row = {
        "id": model_id,
        "name": model_id,
        "capabilities": ["text_to_image"],
        "tier": "standard",
        "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        "min_vram_gb": 10,
        "default": True,
        "benchmark_only": False,
        "installed": False,
        "needs_token": False,
        "download_bytes": None,
    }
    row.update(flags)
    return row


def test_catalog_keeps_manifest_fields_and_the_install_flags():
    (row,) = worker_models.parse_catalog([manifest_row(installed=True, download_bytes=7)])
    assert row["id"] == "pipe-test"
    assert row["installed"] is True
    assert row["download_bytes"] == 7
    assert row["needs_token"] is False


@pytest.mark.parametrize("override", [
    {"installed": "yes"},
    {"needs_token": 1},
    {"download_bytes": -1},
    {"download_bytes": True},
    {"id": 7},
])
def test_catalog_refuses_wrong_types(override):
    with pytest.raises(ValueError):
        worker_models.parse_catalog([manifest_row(**override)])


def test_downloads_accept_each_state_and_refuse_unknown_ones():
    rows = [{"model_id": "pipe-test", "state": "downloading", "bytes_done": 5,
             "bytes_total": 10, "error": None, "kind": "install"},
            {"model_id": "pipe-b", "state": "failed", "bytes_done": 0,
             "bytes_total": 10, "error": "disk", "kind": "remove"}]
    parsed = worker_models.parse_downloads(rows)
    assert parsed["pipe-b"]["error"] == "disk"
    assert parsed["pipe-b"]["kind"] == "remove"
    for invalid in (None, "other", 1, []):
        with pytest.raises(ValueError):
            worker_models.parse_downloads([{**rows[0], "kind": invalid}])
    with pytest.raises(ValueError):
        worker_models.parse_downloads([{k: v for k, v in rows[0].items() if k != "kind"}])
    assert worker_models.parse_downloads([{**rows[0], "op_id": "attempt"}])["pipe-test"]["op_id"] == "attempt"
    with pytest.raises(ValueError):
        worker_models.parse_downloads([{**rows[0], "op_id": 42}])
    with pytest.raises(ValueError):
        worker_models.parse_downloads([{**rows[0], "state": "paused"}])
    with pytest.raises(ValueError):
        worker_models.parse_downloads([{**rows[0], "error": "x" * 65}])


def test_hardware_refuses_negative_disk_and_accepts_an_unknown_vram():
    parsed = worker_models.parse_hardware({"gpu_name": "Card", "vram_total_bytes": None,
                                           "disk_total_bytes": 10, "disk_free_bytes": 5})
    assert parsed["vram_total_bytes"] is None
    with pytest.raises(ValueError):
        worker_models.parse_hardware({"gpu_name": "Card", "vram_total_bytes": None,
                                      "disk_total_bytes": 10, "disk_free_bytes": -5})


def fake_worker(worker_id="w-view", *, capabilities=(), catalog=None, hardware=None):
    worker = realtime.Worker(id=worker_id, ws=None, manifests=[], realtime_slots=0,
                             capabilities=list(capabilities))
    worker.catalog = catalog
    worker.hardware = hardware
    worker.store_downloads = {}
    return worker


def _admin_as(client, email: str) -> None:
    client.portal.call(_make, email, "admin")
    assert _login(client, email).status_code == 204


def test_a_worker_without_the_capability_is_not_manageable():
    view = worker_models.worker_view(fake_worker(capabilities=["command_dedupe"]))
    assert view["manageable"] is False
    assert view["models"] == []
    assert view["reason"] == "worker predates model management"


def test_a_capable_worker_view_marks_fit_and_suggestion():
    worker = fake_worker(
        capabilities=["model_store"],
        catalog=[{**entry("pipe-test", ["text_to_image"], min_vram_gb=10, default=True),
                  "name": "pipe-test", "installed": True, "download_bytes": 3,
                  "needs_token": False}],
        hardware={"gpu_name": "Card", "vram_total_bytes": 8 * GIB,
                  "disk_total_bytes": 10, "disk_free_bytes": 5},
    )
    view = worker_models.worker_view(worker)
    (model,) = view["models"]
    assert view["manageable"] is True
    assert model["fits"] is False  # 10 GB needed, 8 GB present
    assert model["suggested"] is False
    assert view["suggested"]["text_to_image"] is None


def test_admin_routes_are_closed_to_other_roles(accounts):
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        client.portal.call(_make, "member-models@example.com", "user")
        assert _login(client, "member-models@example.com").status_code == 204
        assert client.get("/api/v1/admin/models").status_code == 403
        headers = _csrf(client)
        for path in ("/api/v1/admin/workers/w-x/models/pipe-test/install",
                     "/api/v1/admin/workers/w-x/models/pipe-test/cancel"):
            assert client.post(path, headers=headers).status_code == 403
        assert client.delete("/api/v1/admin/workers/w-x/models/pipe-test",
                             headers=headers).status_code == 403


def hello_for(worker_id: str, *, capabilities, models=()) -> dict:
    """A hello as the worker sends it. The store's hardware and catalog never ride in it."""
    return {
        "type": "hello",
        "protocol_version": 5,
        "worker_id": worker_id,
        "models": list(models),
        "realtime_slots": 1,
        "device": "rocm",
        "memory_mode": "auto",
        "capabilities": list(capabilities),
    }


def inventory_for(catalog) -> dict:
    """store_inventory, which a capable worker sends once after the API's registration."""
    return {
        "type": "store_inventory",
        "hardware": {"gpu_name": "Card", "vram_total_bytes": 16 * GIB,
                     "disk_total_bytes": 100 * GIB, "disk_free_bytes": 50 * GIB},
        "catalog": catalog,
    }


def _wait_until_manageable(client, worker_id: str) -> dict:
    """The inventory travels over the socket, so the HTTP view sees it a moment later."""
    deadline = time.monotonic() + 5
    while True:
        listing = client.get("/api/v1/admin/models").json()
        (row,) = [r for r in listing if r["worker_id"] == worker_id]
        if row["manageable"] or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    assert row["manageable"] is True
    return row


@pytest.mark.db
def test_a_capable_worker_registers_empty_and_its_commands_and_reports_land(accounts):
    worker_id = "w-models-capable"
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        _admin_as(client, "models-capable-admin@example.com")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for(worker_id, capabilities=["model_store"]))
            registered = ws.receive_json()
            assert registered["type"] == "registered"
            assert registered["model_store"] is True
            ws.send_json(inventory_for([manifest_row(installed=False)]))
            row = _wait_until_manageable(client, worker_id)
            assert row["models"][0]["installed"] is False
            assert row["models"][0]["suggested"] is True

            response = client.post(
                f"/api/v1/admin/workers/{worker_id}/models/pipe-test/install",
                headers=_csrf(client))
            assert response.status_code == 202
            op_id = response.json()["op_id"]
            assert ws.receive_json() == {"type": "install_model", "model_id": "pipe-test",
                                         "op_id": op_id}

            ws.send_json({"type": "store_status", "disk_free_bytes": 40 * GIB,
                          "downloads": [{"model_id": "pipe-test", "state": "downloading",
                                         "bytes_done": 5, "bytes_total": 10, "error": None,
                                         "op_id": op_id, "kind": "install"}]})
            ws.send_json({"type": "models_changed",
                          "models": [manifest_row(installed=True)],
                          "catalog": [manifest_row(installed=True)]})

            deadline = time.monotonic() + 5
            while True:
                listing = client.get("/api/v1/admin/models").json()
                (row,) = [r for r in listing if r["worker_id"] == worker_id]
                if row["models"][0]["installed"] or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            assert row["disk_free_bytes"] == 40 * GIB
            assert row["models"][0]["download"]["state"] == "downloading"
            assert row["models"][0]["download"]["kind"] == "install"
            assert row["models"][0]["download"]["op_id"] == op_id
            assert row["models"][0]["installed"] is True
            assert [m["id"] for m in row["models"]] == ["pipe-test"]
            client.portal.call(drop_worker_rows, worker_id)


@pytest.mark.db
def test_an_older_worker_is_not_manageable_and_is_never_sent_a_command(accounts):
    worker_id = "w-models-older"
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        _admin_as(client, "models-older-admin@example.com")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for(worker_id, capabilities=["command_dedupe"]))
            assert ws.receive_json()["type"] == "registered"
            listing = client.get("/api/v1/admin/models").json()
            (row,) = [r for r in listing if r["worker_id"] == worker_id]
            assert row["manageable"] is False
            response = client.post(
                f"/api/v1/admin/workers/{worker_id}/models/pipe-test/install",
                headers=_csrf(client))
            assert response.status_code == 409
            client.portal.call(drop_worker_rows, worker_id)


@pytest.mark.db
def test_a_gated_model_is_refused_before_it_reaches_the_worker(accounts):
    worker_id = "w-models-gated"
    gated = manifest_row("gated-test", needs_token=True)
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        _admin_as(client, "models-gated-admin@example.com")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for(worker_id, capabilities=["model_store"]))
            assert ws.receive_json()["type"] == "registered"
            ws.send_json(inventory_for([gated]))
            _wait_until_manageable(client, worker_id)
            response = client.post(
                f"/api/v1/admin/workers/{worker_id}/models/gated-test/install",
                headers=_csrf(client))
            assert response.status_code == 409
            assert response.json()["detail"] == "needs_token"
            client.portal.call(drop_worker_rows, worker_id)


def test_store_messages_are_closed_shapes_on_the_api_side():
    import uuid

    identity = {"worker_id": "w-shape", "incarnation": str(uuid.uuid4())}
    validate_message({"type": "models_changed", "models": [], "catalog": [], **identity},
                     expected_worker_id="w-shape",
                     expected_incarnation=uuid.UUID(identity["incarnation"]))
    validate_message({"type": "store_status", "downloads": [], "disk_free_bytes": 0,
                      **identity}, expected_worker_id="w-shape",
                     expected_incarnation=uuid.UUID(identity["incarnation"]))



def test_store_status_requires_valid_kind():
    import uuid

    identity = {"worker_id": "w-shape", "incarnation": str(uuid.uuid4())}
    for kind in ("install", "remove", "cancel"):
        validate_message({"type": "store_status", "downloads": [{"kind": kind}],
                          "disk_free_bytes": 0, **identity})
    for entry in ({}, {"kind": "other"}, {"kind": None}, {"kind": []}):
        with pytest.raises(ValueError):
            validate_message({"type": "store_status", "downloads": [entry],
                              "disk_free_bytes": 0, **identity})


def test_store_operation_id_protocol_types():
    import uuid

    identity = {"worker_id": "w-shape", "incarnation": str(uuid.uuid4())}
    for value in (None, "attempt"):
        validate_message({"type": "install_model", "model_id": "x", "op_id": value, **identity})
        validate_message({"type": "store_status", "downloads": [{"op_id": value, "kind": "install"}],
                          "disk_free_bytes": 0, **identity})
    for message in ({"type": "install_model", "model_id": "x", "op_id": 42, **identity},
                    {"type": "store_status", "downloads": [{"op_id": 42, "kind": "install"}],
                     "disk_free_bytes": 0, **identity}):
        with pytest.raises(ValueError):
            validate_message(message)

def test_a_removal_stops_routing_to_the_worker_at_once(monkeypatch):
    import asyncio

    from app import registry

    async def no_persist(_manifests):
        return None

    monkeypatch.setattr(registry, "persist_manifests", no_persist)
    installed = [manifest_row(installed=True)]
    worker = fake_worker("w-route", capabilities=["model_store"], catalog=worker_models.parse_catalog(installed))
    worker.manifests = parse_manifests([
        {key: value for key, value in row.items() if key not in worker_models.CATALOG_FLAGS}
        for row in installed
    ])
    monkeypatch.setitem(realtime.workers, worker.id, worker)
    assert realtime.pick_worker_for_model("pipe-test") is worker

    asyncio.run(worker_models.apply(worker, {
        "type": "models_changed",
        "models": [],
        "catalog": [manifest_row(installed=False)],
    }))

    assert realtime.pick_worker_for_model("pipe-test") is None
    assert worker.catalog[0]["installed"] is False


@pytest.mark.db
def test_the_api_signals_model_store_only_to_a_worker_that_offered_it(accounts):
    with TestClient(app, base_url=ORIGIN, headers=FLEET_HEADERS) as client:
        _admin_as(client, "models-signal-admin@example.com")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for("w-signal-older", capabilities=["command_dedupe"]))
            older = ws.receive_json()
            client.portal.call(drop_worker_rows, "w-signal-older")
        with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as ws:
            ws.send_json(hello_for("w-signal-capable", capabilities=["model_store"]))
            capable = ws.receive_json()
            client.portal.call(drop_worker_rows, "w-signal-capable")
    assert "model_store" not in older
    assert capable["model_store"] is True



def test_model_commands_return_the_sent_unique_operation_id(monkeypatch):
    import asyncio
    import re
    from types import SimpleNamespace

    sent = []

    async def send_json(message):
        sent.append(message)

    worker = SimpleNamespace(id="worker", protocol_version=5, store_downloads={},
                             ws=SimpleNamespace(send_json=send_json))
    monkeypatch.setattr(worker_models, "_managed", lambda *_: (worker, {"needs_token": False}))

    async def scenario():
        return [await route("worker", "model", None) for route in
                (worker_models.install_model, worker_models.cancel_install,
                 worker_models.remove_model)]

    responses = asyncio.run(scenario())
    ids = [response["op_id"] for response in responses]
    assert len(set(ids)) == 3
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"[0-9]{20}-[0-9a-f]{8}", value) for value in ids)
    assert sent == [{"type": kind, "model_id": "model", "op_id": op_id}
                    for kind, op_id in zip(("install_model", "cancel_install", "remove_model"), ids)]


def test_operation_id_follows_worker_records_after_clock_reset(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def send_json(_message):
        pass

    previous = '00000000000000005000-ffffffff'
    worker = SimpleNamespace(id='worker', protocol_version=5,
                             store_downloads={'model': {'op_id': previous}},
                             ws=SimpleNamespace(send_json=send_json))
    monkeypatch.setattr(worker_models, '_last_op_time', {})
    monkeypatch.setattr(worker_models.time, 'time_ns', lambda: 100)
    assert asyncio.run(worker_models._send(worker, 'install_model', 'model')) > previous


def test_reconnect_inventory_orders_first_command_before_status(monkeypatch):
    import asyncio

    async def scenario():
        class Socket:
            async def send_json(self, message):
                self.message = message
        socket = Socket()
        worker = realtime.Worker(id="reconnect", ws=socket, manifests=[], realtime_slots=1,
                                 protocol_version=5, capabilities=["model_store"])
        old = "00000000000000005000-aaaaaaaa"
        monkeypatch.setattr(worker_models, "_last_op_time", {})
        monkeypatch.setattr(worker_models.time, "time_ns", lambda: 100)
        await worker_models.apply(worker, {**inventory_for([manifest_row()]), "op_id": old})
        assert worker_models.manageable(worker)
        new = await worker_models._send(worker, "install_model", "pipe-test")
        await worker_models.apply(worker, {"type": "store_status", "disk_free_bytes": 1,
            "downloads": [{"model_id": "pipe-test", "state": "done", "bytes_done": 1,
                           "bytes_total": 1, "error": None, "op_id": old, "kind": "install"}]})
        assert new > old
        assert socket.message["op_id"] == new
    asyncio.run(scenario())


def test_future_worker_clock_is_ignored_and_isolated(monkeypatch, caplog):
    import asyncio
    from types import SimpleNamespace
    async def send_json(_message):
        pass
    monkeypatch.setattr(worker_models, '_last_op_time', {})
    monkeypatch.setattr(worker_models.time, 'time_ns', lambda: 100)
    def worker(name, stamp):
        return SimpleNamespace(id=name, protocol_version=5,
                               store_downloads={'m': {'op_id': f'{stamp:020d}-aaaaaaaa'}},
                               ws=SimpleNamespace(send_json=send_json))
    bad = worker('bad', 10**20 - 1)
    good = worker('good', 0)
    async def scenario():
        first = await worker_models._send(bad, 'install_model', 'm')
        second = await worker_models._send(good, 'install_model', 'm')
        assert first[:20] == second[:20] == '00000000000000000100'
        assert len(first.split('-')[0]) == 20
    asyncio.run(scenario())
    assert 'ignoring future' in caplog.text


def test_inventory_future_watermark_is_ignored_per_worker(monkeypatch, caplog):
    import asyncio
    monkeypatch.setattr(worker_models, '_last_op_time', {})
    monkeypatch.setattr(worker_models.time, 'time_ns', lambda: 100)
    async def scenario():
        a = realtime.Worker(id='a', ws=None, manifests=[], realtime_slots=1, protocol_version=5)
        b = realtime.Worker(id='b', ws=None, manifests=[], realtime_slots=1, protocol_version=5)
        await worker_models.apply(a, {**inventory_for([manifest_row()]), 'op_id': '99999999999999999999-deadbeef'})
        await worker_models.apply(b, {**inventory_for([manifest_row()]), 'op_id': '00000000000000005000-deadbeef'})
        assert worker_models._last_op_time == {'b': 5000}
    asyncio.run(scenario())
    assert 'ignoring future' in caplog.text
