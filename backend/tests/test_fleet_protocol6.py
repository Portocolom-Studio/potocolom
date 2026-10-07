"""Protocol 6 on the /fleet socket: registration, grant, heartbeat, the
fallbacks when no root key ring exists, and the fact that such a worker is
never handed work in this release."""

import asyncio
import base64
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app import db, jobs, realtime, registry, worker_authority
from app.main import app
from app.protocol6 import validate_message
from app.settings import get_settings
from app.tables import Job, Model

FLEET_HEADERS = {"x-fleet-token": "test-fleet-token"}


@contextmanager
def protocol6_client(worker_id: str):
    """A lifespan-running client that removes this file's worker rows on exit.

    Registration and heartbeat persist an identity row and, for the
    heartbeat, a gpu sample; conftest clears neither, and
    tests/test_metrics.py reads the sample table expecting only its own.
    """
    with TestClient(app, headers=FLEET_HEADERS) as client:
        try:
            yield client
        finally:
            client.portal.call(fresh_pool)
            client.portal.call(drop_worker_rows, worker_id)


async def fresh_pool() -> None:
    """Replace the pool before cleanup. Closing a socket while its handler
    is still in a query makes TestClient cancel the handler through an anyio
    cancel scope, which also cancels SQLAlchemy's terminate of that
    connection, so a closed connection can go back into the pool."""
    assert db.engine is not None
    await db.engine.dispose()


def root_keys(monkeypatch, letter: str = "z") -> None:
    monkeypatch.setenv("ROOT_KEYS", "1:" + base64.b64encode(letter.encode() * 32).decode())
    get_settings.cache_clear()


def manifest(model_id: str, parameters: dict | None = None) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "capabilities": ["realtime"],
        "parameters": ({
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        } if parameters is None else parameters),
    }


def request_work_grant(ws, worker_id: str, incarnation: uuid.UUID) -> dict:
    renewal_nonce = uuid.uuid4()
    ws.send_json({
        "type": "grant_request",
        "worker_id": worker_id,
        "incarnation": str(incarnation),
        "region": worker_authority.REGION,
        "grant_nonce": str(renewal_nonce),
    })
    grant = ws.receive_json()
    assert grant["type"] == "work_grant"
    assert grant["ready"] is True
    return grant


def hello(worker_id: str, models: list[dict], *, incarnation: uuid.UUID | None = None,
          compatible: tuple[int, ...] = (6, 5)) -> dict:
    return {
        "type": "hello",
        "protocol_version": 6,
        "compatible_versions": list(compatible),
        "worker_id": worker_id,
        "incarnation": str(incarnation or uuid.uuid4()),
        "grant_nonce": str(uuid.uuid4()),
        "capabilities": ["authority_fence", "work_budget"],
        "models": models,
        "realtime_slots": 1,
        "device": "cpu-test",
        "memory_mode": "offload",
    }


async def _row(worker_id: str, incarnation: uuid.UUID, columns: str) -> dict:
    async with db.session_factory() as session:
        return (await session.execute(
            text(f"SELECT {columns} FROM worker_connections "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one()


async def _wait_for_row(worker_id: str, incarnation: uuid.UUID, columns: str,
                        done) -> dict:
    for _ in range(250):
        row = await _row(worker_id, incarnation, columns)
        if done(row):
            return row
        await asyncio.sleep(0.02)
    raise AssertionError("the durable worker row never reached the expected state")


async def seed_queued_job(model_id: str) -> uuid.UUID:
    assert db.local_user_id is not None
    assert db.session_factory is not None
    job_id = uuid.uuid4()
    async with db.session_factory() as session:
        if await session.get(Model, model_id) is None:
            session.add(Model(id=model_id, name=model_id,
                              capabilities=["text_to_image"],
                              parameters_schema={"type": "object"}, min_vram_gb=0))
        await session.flush()
        job = Job(id=job_id, user_id=db.local_user_id, model_id=model_id,
                  params={"prompt": "queued"}, state="queued", attempt=1)
        session.add(job)
        await session.commit()
        await session.refresh(job, ["created_at"])
    await jobs.queues.push(jobs.JOB_QUEUE,
                           jobs.QueueHint(jobs.TIER_DEFAULT, job.created_at, str(job_id)))
    return job_id


async def wait_for_sample(worker_id: str) -> None:
    """Block until the heartbeat's fire-and-forget sample has committed."""
    assert db.session_factory is not None
    for _ in range(250):
        async with db.session_factory() as session:
            present = await session.scalar(
                text("SELECT count(*) FROM gpu_samples WHERE worker_id = :worker_id"),
                {"worker_id": worker_id},
            )
        if present:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the heartbeat sample never persisted")


async def wait_for_identity(worker_id: str) -> None:
    assert db.session_factory is not None
    for _ in range(500):
        async with db.session_factory() as session:
            if await session.scalar(
                text("SELECT count(*) FROM workers WHERE worker_id = :worker_id"),
                {"worker_id": worker_id},
            ):
                return
        await asyncio.sleep(0.02)
    raise AssertionError("the worker identity row never persisted")


async def drop_worker_rows(worker_id: str) -> None:
    """Delete the identity and heartbeat sample rows this file wrote.

    Registration and heartbeat persist both, conftest clears neither, and
    tests/test_metrics.py reads the sample table expecting it to hold what its
    own heartbeat wrote and nothing else. The identity row is waited for
    first: it is written by a background task, and a delete that ran before
    that task would leave the row behind anyway.
    """
    assert db.session_factory is not None
    for _ in range(250):
        async with db.session_factory() as session:
            present = await session.scalar(
                text("SELECT count(*) FROM workers WHERE worker_id = :worker_id"),
                {"worker_id": worker_id},
            )
        if present:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the worker identity row never persisted")
    async with db.session_factory() as session:
        await session.execute(
            text("DELETE FROM gpu_samples WHERE worker_id = :worker_id"),
            {"worker_id": worker_id},
        )
        await session.execute(
            text("DELETE FROM workers WHERE worker_id = :worker_id"),
            {"worker_id": worker_id},
        )
        await session.commit()


@pytest.mark.db
def test_v6_hello_registers_grants_heartbeats_and_closes_on_disconnect(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"p6-fleet-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"p6-fleet-model-{uuid.uuid4()}"
    try:
        with protocol6_client(worker_id) as client:
            lease = client.portal.call(worker_authority.acquire_scheduler_lease)
            with client.websocket_connect("/api/v1/fleet") as ws:
                ws.send_json(hello(worker_id, [manifest(model_id)],
                                   incarnation=incarnation))
                registered = ws.receive_json()
                assert registered["type"] == "registered"
                assert registered["protocol_version"] == 6
                assert registered["worker_id"] == worker_id
                assert registered["incarnation"] == str(incarnation)
                assert registered["transport_owner_id"] == str(
                    worker_authority.TRANSPORT_OWNER_ID)
                assert registered["region"] == worker_authority.REGION
                assert registered["lease_id"] == str(lease.lease_id)
                assert registered["owner_epoch"] == lease.owner_epoch
                assert registered["lease_expires_at"].endswith("Z")
                assert registered["ready"] is False
                assert registered["remaining_ms"] == 0
                validate_message(registered, expected_worker_id=worker_id,
                                 expected_incarnation=incarnation)
                worker = realtime.workers[worker_id]
                assert worker.protocol_version == 6
                assert worker.incarnation == incarnation
                assert realtime.takes_work(worker) is False

                renewal_nonce = uuid.uuid4()
                ws.send_json({
                    "type": "grant_request",
                    "worker_id": worker_id,
                    "incarnation": str(incarnation),
                    "region": worker_authority.REGION,
                    "grant_nonce": str(renewal_nonce),
                })
                grant = ws.receive_json()
                assert grant["type"] == "work_grant"
                assert grant["worker_id"] == worker_id
                assert grant["incarnation"] == str(incarnation)
                assert grant["grant_nonce"] == str(renewal_nonce)
                assert grant["lease_id"] == str(lease.lease_id)
                assert grant["owner_epoch"] == lease.owner_epoch
                assert grant["ready"] is True
                assert 0 < grant["remaining_ms"] <= 10_000
                validate_message(grant, expected_worker_id=worker_id,
                                 expected_incarnation=incarnation)

                ws.send_json({
                    "type": "heartbeat",
                    "worker_id": worker_id,
                    "incarnation": str(incarnation),
                    "region": worker_authority.REGION,
                    "frame_p95_ms": {model_id: 421},
                    "gpu": None,
                    "loaded_models": [],
                    "slots_in_use": 0,
                })
                # The heartbeat answers nothing; the durable row is its receipt.
                observed = client.portal.call(
                    _wait_for_row, worker_id, incarnation, "observations",
                    lambda row: row["observations"].get("frame_p95_ms", {}).get(model_id) == 421,
                )
                assert observed["observations"]["frame_p95_ms"] == {model_id: 421}
                # Its gpu sample is fire-and-forget; wait for it here so the
                # cleanup at client exit cannot lose the race to it.
                client.portal.call(wait_for_sample, worker_id)

            assert worker_id not in realtime.workers
            closed = client.portal.call(
                _wait_for_row, worker_id, incarnation, "lifecycle, closed_at",
                lambda row: row["closed_at"] is not None,
            )
            assert closed["lifecycle"] == "ended"
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_v6_hello_without_root_keys_falls_back_to_protocol5(monkeypatch):
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    get_settings.cache_clear()
    worker_id = f"p6-fallback-{uuid.uuid4()}"
    model_id = f"p6-fallback-model-{uuid.uuid4()}"
    try:
        with protocol6_client(worker_id) as client:
            with client.websocket_connect("/api/v1/fleet") as ws:
                ws.send_json(hello(worker_id, [manifest(model_id)]))
                assert ws.receive_json() == {"type": "registered",
                                             "protocol_version": 5}
                worker = realtime.workers[worker_id]
                assert worker.protocol_version == 5
                assert worker.incarnation is None
                assert worker.grant_nonce is None
                assert realtime.takes_work(worker)
                assert realtime.model_known(model_id)
                assert realtime.pick_worker(model_id) is worker
                assert model_id in registry.available()
                # A downgraded worker is a protocol 5 worker on every later
                # message: an ordinary session opens and completes on it.
                with client.websocket_connect("/api/v1/realtime") as browser:
                    browser.send_json({"type": "open", "model_id": model_id,
                                       "params": {"prompt": "a red house"},
                                       "frame_header": 2})
                    opened = ws.receive_json()
                    assert opened["type"] == "open_session"
                    ws.send_json({
                        "type": "session_ready",
                        "session_id": opened["session_id"],
                        "control_generation": opened.get("control_generation", 1),
                    })
                    assert browser.receive_json()["type"] == "ready"
    finally:
        get_settings.cache_clear()


def test_v6_hello_without_root_keys_in_accounts_mode_is_rejected(monkeypatch):
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    worker_id = f"p6-accounts-{uuid.uuid4()}"
    try:
        # No lifespan: the refusal happens before anything needs the database,
        # and accounts mode would refuse to start on an installation that has
        # not been enabled.
        client = TestClient(app, headers=FLEET_HEADERS)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(f"{worker_id}-model")]))
            assert ws.receive_json() == {
                "type": "rejected",
                "reason": "recovery_unavailable",
                "min_supported_version": 5,
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == realtime.CLOSE_UNSUPPORTED_VERSION
        assert worker_id not in realtime.workers
    finally:
        get_settings.cache_clear()


@pytest.mark.db
def test_a_v6_worker_is_never_picked_for_work(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"p6-nopick-{uuid.uuid4()}"
    model_id = f"p6-nopick-model-{uuid.uuid4()}"
    try:
        with protocol6_client(worker_id) as client:
            client.portal.call(worker_authority.acquire_scheduler_lease)
            with client.websocket_connect("/api/v1/fleet") as ws:
                ws.send_json(hello(worker_id, [manifest(model_id)]))
                assert ws.receive_json()["protocol_version"] == 6
                # The identity row is scheduled after the post-registration
                # database work; closing before then would cancel the handler
                # first and leave cleanup waiting for a row that never comes.
                client.portal.call(wait_for_identity, worker_id)
                job_id = client.portal.call(seed_queued_job, model_id)

                real_dispatch_step = jobs.dispatch_step

                async def parked():
                    return None

                monkeypatch.setattr(jobs, "dispatch_step", parked)
                client.portal.call(real_dispatch_step)
                # The pass finds nobody it may hand this job to, so the row
                # never leaves the queue and the worker never sees a message.
                state = client.get(f"/api/v1/generations/{job_id}").json()["state"]
                assert state == "queued"
                # Nothing else counts the worker as available either.
                assert realtime.pick_any_worker() is None
                assert realtime.pick_worker_for_model(model_id) is None
                assert realtime.pick_worker(model_id) is None
                assert realtime.model_known(model_id) is False
                assert jobs.pick_job_worker(model_id) is None
                assert model_id not in registry.available()
    finally:
        get_settings.cache_clear()


def test_a_v6_hello_with_a_ref_parameter_schema_is_refused(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"p6-ref-{uuid.uuid4()}"
    model_id = f"p6-ref-model-{uuid.uuid4()}"
    try:
        with TestClient(app, headers=FLEET_HEADERS) as client:
            with client.websocket_connect("/api/v1/fleet") as ws:
                ws.send_json(hello(
                    worker_id,
                    [manifest(model_id, {"$ref": "#/$defs/prompt"})],
                ))
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == realtime.CLOSE_PROTOCOL_VIOLATION
                assert "$ref" in closed.value.reason
            assert worker_id not in realtime.workers
    finally:
        get_settings.cache_clear()


@pytest.mark.db
@pytest.mark.parametrize("slots", [0, 2**31])
def test_a_v6_hello_registers_at_the_edges_of_realtime_slots(monkeypatch, slots):
    """Protocol 6 admits zero slots (a jobs-only worker) and any signed 64 bit
    count; the row must store both instead of failing the insert."""
    root_keys(monkeypatch)
    worker_id = f"p6-zero-{uuid.uuid4()}"
    try:
        with protocol6_client(worker_id) as client:
            client.portal.call(worker_authority.acquire_scheduler_lease)
            with client.websocket_connect("/api/v1/fleet") as ws:
                message = hello(worker_id, [manifest(f"{worker_id}-model")])
                message["realtime_slots"] = slots
                ws.send_json(message)
                registered = ws.receive_json()
                assert registered["type"] == "registered"
                assert registered["protocol_version"] == 6
                ws.send_json({
                    "type": "grant_request",
                    "worker_id": worker_id,
                    "incarnation": message["incarnation"],
                    "region": worker_authority.REGION,
                    "grant_nonce": str(uuid.uuid4()),
                })
                assert ws.receive_json()["ready"] is True
    finally:
        get_settings.cache_clear()


def test_a_v6_fallback_with_a_malformed_version_list_is_refused(monkeypatch):
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    get_settings.cache_clear()
    worker_id = f"p6-badlist-{uuid.uuid4()}"
    try:
        client = TestClient(app, headers=FLEET_HEADERS)
        with client.websocket_connect("/api/v1/fleet") as ws:
            message = hello(worker_id, [manifest(f"{worker_id}-model")])
            message["compatible_versions"] = 5
            ws.send_json(message)
            reply = ws.receive_json()
            assert reply["type"] == "rejected"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        get_settings.cache_clear()


def test_a_v6_hello_without_durable_authority_registers_as_protocol5(monkeypatch):
    """Right after a restart the lease may not be held yet, or the database
    may be down. A worker that also speaks 5 is served as 5, not turned away."""
    root_keys(monkeypatch)
    worker_id = f"p6-noauth-{uuid.uuid4()}"

    async def unavailable(**_kwargs):
        raise worker_authority.AuthorityUnavailable("regional scheduler lease was lost")

    monkeypatch.setattr(worker_authority, "register_worker", unavailable)
    try:
        client = TestClient(app, headers=FLEET_HEADERS)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(f"{worker_id}-model")]))
            assert ws.receive_json() == {"type": "registered", "protocol_version": 5}
            worker = realtime.workers[worker_id]
            assert worker.protocol_version == 5
            assert worker.incarnation is None
            assert realtime.takes_work(worker)
    finally:
        get_settings.cache_clear()
