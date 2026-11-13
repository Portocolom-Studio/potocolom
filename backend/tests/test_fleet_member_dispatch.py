"""Dispatch addresses for enrolled fleet members (issue #885, third PR).

An enrolled member is sent its upload and source-image addresses under the
origin it joined through, never under INTERNAL_URL. A shared-secret worker keeps
the worker address. Protocol 5 dispatches through jobs.dispatch and protocol 6
through jobs.dispatch_protocol6, so each test runs at both protocols.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db, jobs, worker_authority
from app.main import app
from app.storage import get_storage
from app.tables import Asset, Job, Model
from tests.test_fleet_members import ORIGIN, _create, _enroll, none_mode
from tests.test_fleet_protocol6 import (
    drop_worker_rows,
    fresh_pool,
    request_work_grant,
    root_keys,
)
from tests.test_fleet_protocol6 import hello as protocol6_hello
from tests.test_jobs import FLEET_HEADERS, MANIFEST

__all__ = ["none_mode"]
pytestmark = pytest.mark.db

MEMBER_BASE = "https://studio.example.com"


def _hello(protocol: int, worker_id: str, incarnation: uuid.UUID) -> dict:
    if protocol == 6:
        return protocol6_hello(worker_id, [MANIFEST], incarnation=incarnation)
    return {"type": "hello", "protocol_version": 5, "worker_id": worker_id,
            "models": [MANIFEST], "realtime_slots": 1}


async def _seed_job_with_source() -> uuid.UUID:
    """A queued job whose source image is a stored asset, so dispatch builds an input address."""
    assert db.local_user_id is not None
    assert db.session_factory is not None
    asset_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with db.session_factory() as session:
        await session.execute(
            pg_insert(Model).values(id=MANIFEST["id"], name=MANIFEST["name"],
                                    capabilities=MANIFEST["capabilities"],
                                    parameters_schema=MANIFEST["parameters"], min_vram_gb=0)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.add(Asset(
            id=asset_id,
            user_id=db.local_user_id,
            job_id=None,
            storage_key=f"{db.local_user_id}/source-{asset_id}.png",
            mime="image/png",
            width=512,
            height=512,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        await session.flush()
        session.add(Job(id=job_id, user_id=db.local_user_id, model_id=MANIFEST["id"],
                        params={"prompt": "edit"}, state="queued", attempt=1,
                        source_asset_id=asset_id))
        await session.commit()
    return job_id


def _assert_addresses_under(dispatch: dict, base: str) -> None:
    assert dispatch["upload"]["url"].startswith(f"{base}/api/v1/files/")
    assert dispatch["thumb_upload"]["url"].startswith(f"{base}/api/v1/files/")
    assert dispatch["input"]["url"].startswith(f"{base}/api/v1/worker-input?token=")


def _dispatch_to_worker(client, worker, protocol: int, worker_id: str,
                        incarnation: uuid.UUID) -> dict:
    """Register the worker, dispatch one job with a source image, return what it was sent."""
    # Without the lease a protocol 6 hello falls back to protocol 5, so it is held first.
    if protocol == 6:
        client.portal.call(worker_authority.acquire_scheduler_lease)
    worker.send_json(_hello(protocol, worker_id, incarnation))
    registered = worker.receive_json()
    assert registered["type"] == "registered"
    # A protocol 5 reply names no version unless it is a fallback from 6.
    assert registered.get("protocol_version", 5) == protocol
    if protocol == 6:
        request_work_grant(worker, worker_id, incarnation)
    job_id = client.portal.call(_seed_job_with_source)
    assert client.portal.call(jobs.dispatch, job_id) is True
    dispatch = worker.receive_json()
    assert dispatch["job_id"] == str(job_id)
    return dispatch


@pytest.mark.parametrize("protocol", [5, 6])
def test_an_enrolled_member_is_sent_addresses_under_the_base_it_joined_through(
        none_mode, monkeypatch, protocol):
    if protocol == 6:
        root_keys(monkeypatch)
    with TestClient(app, base_url=ORIGIN) as client:
        member = _create(client, {"Origin": ORIGIN}, label="Remote").json()
        enrolled = _enroll(client, member["join_token"], MEMBER_BASE).json()
        worker_id = enrolled["worker_id"]
        try:
            with client.websocket_connect(
                    "/api/v1/fleet",
                    headers={"x-fleet-token": enrolled["credential"]}) as worker:
                dispatch = _dispatch_to_worker(client, worker, protocol, worker_id,
                                               uuid.uuid4())
                _assert_addresses_under(dispatch, MEMBER_BASE)
        finally:
            client.portal.call(fresh_pool)
            client.portal.call(drop_worker_rows, worker_id)


@pytest.mark.parametrize("protocol", [5, 6])
def test_a_shared_secret_worker_is_sent_addresses_under_the_worker_url(
        none_mode, monkeypatch, protocol):
    if protocol == 6:
        root_keys(monkeypatch)
    worker_id = f"w-shared-dispatch-{uuid.uuid4().hex[:8]}"
    with TestClient(app, headers=FLEET_HEADERS) as client:
        try:
            with client.websocket_connect("/api/v1/fleet") as worker:
                dispatch = _dispatch_to_worker(client, worker, protocol, worker_id,
                                               uuid.uuid4())
                # The storage in use, not the settings: get_storage() is cached, and an
                # earlier test in the same process can have built it with another address.
                _assert_addresses_under(dispatch, get_storage().worker_url)
        finally:
            client.portal.call(fresh_pool)
            client.portal.call(drop_worker_rows, worker_id)
