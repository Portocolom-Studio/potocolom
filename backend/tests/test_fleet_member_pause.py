"""Pausing an enrolled fleet member (issue #889).

A paused member keeps its socket and the work it already has, and is given no
new job and no new realtime session. The pause is a column, so it outlives a
reconnect and an API restart. These tests drive the real fleet socket, the
browser realtime socket and the job queue against one member.
"""

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db, fleet_members, jobs, realtime, worker_authority
from app.storage import get_storage
from app.tables import User
from app.main import app
from tests.test_fleet_member_dispatch import MEMBER_BASE
from tests.test_fleet_members import (
    MEMBERS,
    ORIGIN,
    _create,
    _enroll,
    none_mode,
)
from tests.test_fleet_protocol6 import (
    drop_worker_rows,
    fresh_pool,
    request_work_grant,
    root_keys,
)
from tests.test_fleet_protocol6 import hello as protocol6_hello
from tests.test_jobs import (
    FLEET_HEADERS,
    MANIFEST,
    png_bytes,
    poll_until,
    put_upload,
    webp_bytes,
)
from tests.test_realtime import answer_ready, expect

__all__ = ["none_mode"]
pytestmark = pytest.mark.db

REALTIME_PARAMETERS = {"type": "object", "properties": {}}


def _realtime_model() -> dict:
    model_id = f"pause-realtime-{uuid.uuid4().hex[:8]}"
    return {"id": model_id, "name": model_id, "capabilities": ["realtime"],
            "parameters": REALTIME_PARAMETERS}


def _hello(protocol: int, worker_id: str, models: list[dict], incarnation: uuid.UUID) -> dict:
    if protocol == 6:
        return protocol6_hello(worker_id, models, incarnation=incarnation)
    return {"type": "hello", "protocol_version": 5, "worker_id": worker_id,
            "models": models, "realtime_slots": 1}


def _member(client, label: str) -> tuple[str, str, str]:
    """Enrolls a member and returns its id, worker id and credential."""
    member = _create(client, {"Origin": ORIGIN}, label=label).json()
    enrolled = _enroll(client, member["join_token"], MEMBER_BASE).json()
    return member["id"], enrolled["worker_id"], enrolled["credential"]


def _connect(client, credential: str):
    return client.websocket_connect("/api/v1/fleet", headers={"x-fleet-token": credential})


def _register(client, worker, protocol: int, worker_id: str, models: list[dict]) -> None:
    incarnation = uuid.uuid4()
    worker.send_json(_hello(protocol, worker_id, models, incarnation))
    assert worker.receive_json()["type"] == "registered"
    if protocol == 6:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        request_work_grant(worker, worker_id, incarnation)


def _set_pause(client, member_id: str, paused: bool):
    return client.patch(f"{MEMBERS}/{member_id}", headers={"Origin": ORIGIN},
                        json={"paused": paused})


def _queue_job(client) -> uuid.UUID:
    created = client.post("/api/v1/generations", headers={"Origin": ORIGIN},
                          json={"model_id": MANIFEST["id"],
                                "params": {"prompt": "a lighthouse"}})
    assert created.status_code == 202, created.text
    return uuid.UUID(created.json()["job_id"])


def _job_state(client, job_id: uuid.UUID) -> str:
    return client.get(f"/api/v1/generations/{job_id}").json()["state"]


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


async def _set_column(member_id: str, paused: bool) -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE fleet_members SET paused = :paused WHERE id = :id"),
            {"paused": paused, "id": uuid.UUID(member_id)})
        await session.commit()


def _cleanup(client, worker_id: str) -> None:
    client.portal.call(fresh_pool)
    client.portal.call(drop_worker_rows, worker_id)


@pytest.mark.parametrize("protocol", [5, 6])
def test_a_paused_member_is_given_no_queued_job_until_it_resumes(none_mode, monkeypatch,
                                                                 protocol):
    if protocol == 6:
        root_keys(monkeypatch)
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Queued")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, protocol, worker_id, [MANIFEST])
                # Paused while connected, so the live worker must see it at once.
                assert _set_pause(client, member_id, True).status_code == 204
                job_id = _queue_job(client)
                client.portal.call(jobs.dispatch_step)
                assert job_id not in jobs.inflight
                assert _job_state(client, job_id) == "queued"

                assert _set_pause(client, member_id, False).status_code == 204
                # The scheduler's own tick takes the job; nothing else is sent.
                _wait_for(lambda: job_id in jobs.inflight, "the queued job to be dispatched")
                dispatch = worker.receive_json()
                assert dispatch["type"] == "dispatch_job"
                assert dispatch["job_id"] == str(job_id)
        finally:
            _cleanup(client, worker_id)


def test_a_job_running_on_a_member_finishes_after_the_member_is_paused(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Running")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, 5, worker_id, [MANIFEST])
                job_id = _queue_job(client)
                dispatch = worker.receive_json()
                assert dispatch["type"] == "dispatch_job"
                assert dispatch["job_id"] == str(job_id)

                assert _set_pause(client, member_id, True).status_code == 204
                assert realtime.workers[worker_id].paused is True
                assert put_upload(client, dispatch["upload"],
                                  png_bytes(320, 240)).status_code == 200
                assert put_upload(client, dispatch["thumb_upload"],
                                  webp_bytes()).status_code == 200
                worker.send_json({"type": "job_progress", "job_id": str(job_id),
                                  "progress": 0.5,
                                  "dispatch_token": dispatch["dispatch_token"]})
                worker.send_json({"type": "job_done", "job_id": str(job_id),
                                  "gpu_ms": 1234, "width": 512, "height": 512,
                                  "has_thumbnail": True,
                                  "dispatch_token": dispatch["dispatch_token"]})
                assert poll_until(client, job_id, "succeeded")["gpu_ms"] == 1234
                # The worker is still connected and still paused: no new work after it.
                assert worker_id in realtime.workers
                assert realtime.workers[worker_id].paused is True
                assert jobs.pick_job_worker(MANIFEST["id"]) is None
        finally:
            _cleanup(client, worker_id)


def test_a_paused_member_with_free_slots_queues_a_session_until_it_resumes(none_mode):
    model = _realtime_model()
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Session")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, 5, worker_id, [model])
                assert _set_pause(client, member_id, True).status_code == 204
                with client.websocket_connect("/api/v1/realtime",
                                              headers={"Origin": ORIGIN}) as browser:
                    browser.send_json({"type": "open", "model_id": model["id"]})
                    queued = expect(browser, "queued")
                    assert queued["position"] == 1
                    waiting = next(s for s in realtime.sessions.values()
                                   if s.model_id == model["id"])
                    assert waiting.state == "queued" and waiting.worker is None

                    assert _set_pause(client, member_id, False).status_code == 204
                    # The resume itself admits the queue; the sweep runs every 30 seconds.
                    _wait_for(lambda: waiting.state in ("assigning", "live"),
                              "the queued session to be admitted")
                    opened = expect(worker, "open_session")
                    assert opened["session_id"] == str(waiting.id)
                    answer_ready(worker, opened)
                    assert expect(browser, "ready")["session_id"] == str(waiting.id)
        finally:
            _cleanup(client, worker_id)


def test_a_paused_member_leaves_the_job_queue_untouched(none_mode, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Queue")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, 5, worker_id, [MANIFEST])
                assert _set_pause(client, member_id, True).status_code == 204
                job_id = _queue_job(client)
                pops: list[str] = []
                real_pop = jobs.queues.pop

                async def counting_pop(queue, *args, **kwargs):
                    pops.append(queue)
                    return await real_pop(queue, *args, **kwargs)

                monkeypatch.setattr(jobs.queues, "pop", counting_pop)
                # The member is the only worker with free capacity, so a tick
                # must not take a job off the queue to find that out.
                client.portal.call(jobs.dispatch_step)
                assert pops == []
                assert _job_state(client, job_id) == "queued"
        finally:
            _cleanup(client, worker_id)


def test_the_pause_survives_a_reconnect(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Reconnect")
        with _connect(client, credential) as worker:
            _register(client, worker, 5, worker_id, [MANIFEST])
            assert _set_pause(client, member_id, True).status_code == 204
        _wait_for(lambda: worker_id not in realtime.workers,
                  "the paused member's socket to close")
        # Closing a socket mid-query can leave a broken connection in the pool.
        client.portal.call(fresh_pool)
        with _connect(client, credential) as worker:
            _register(client, worker, 5, worker_id, [MANIFEST])
            assert realtime.workers[worker_id].paused is True
            assert realtime.pick_worker(MANIFEST["id"]) is None
            assert jobs.pick_job_worker(MANIFEST["id"]) is None


def test_registration_reads_the_pause_from_the_database(none_mode):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Stored")
        client.portal.call(_set_column, member_id, True)
        with _connect(client, credential) as worker:
            _register(client, worker, 5, worker_id, [MANIFEST])
            assert realtime.workers[worker_id].paused is True
            assert jobs.pick_job_worker(MANIFEST["id"]) is None
        _wait_for(lambda: worker_id not in realtime.workers,
                  "the member's socket to close")
        client.portal.call(fresh_pool)
        client.portal.call(_set_column, member_id, False)
        with _connect(client, credential) as worker:
            _register(client, worker, 5, worker_id, [MANIFEST])
            # A member is held paused until its row is read again after registration.
            _wait_for(lambda: realtime.workers[worker_id].paused is False,
                      "the registration to read the row again")
            assert jobs.pick_job_worker(MANIFEST["id"]) is realtime.workers[worker_id]


def test_a_pause_committed_during_the_handshake_is_read_again_at_registration(none_mode,
                                                                             monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Handshake")
        real_lookup = fleet_members.member_for_credential
        calls = []

        async def pause_after_the_first_lookup(credential):
            found = await real_lookup(credential)
            calls.append(credential)
            if len(calls) == 1:
                # The pause lands after the handshake's own read and before registration,
                # so the route finds no worker to set the flag on.
                await _set_column(member_id, True)
            return found

        monkeypatch.setattr(fleet_members, "member_for_credential",
                            pause_after_the_first_lookup)
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, 5, worker_id, [MANIFEST])
                _wait_for(lambda: worker_id in realtime.workers
                          and realtime.workers[worker_id].paused,
                          "the registration to read the pause again")
                assert jobs.pick_job_worker(MANIFEST["id"]) is None
        finally:
            _cleanup(client, worker_id)


def test_a_pause_committed_before_registration_admits_no_queued_session(none_mode, monkeypatch):
    model = _realtime_model()
    other_id = f"w-pause-other-{uuid.uuid4().hex[:8]}"
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Early")
        try:
            # Another worker makes the model known and takes nothing, so the
            # browser's session waits in the queue.
            with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as other:
                _register(client, other, 5, other_id, [model])
                realtime.workers[other_id].paused = True
                with client.websocket_connect("/api/v1/realtime",
                                              headers={"Origin": ORIGIN}) as browser:
                    browser.send_json({"type": "open", "model_id": model["id"]})
                    expect(browser, "queued")
                    waiting = next(s for s in realtime.sessions.values()
                                   if s.model_id == model["id"])
                    real_lookup = fleet_members.member_for_credential
                    calls = []

                    async def pause_before_the_second_lookup(credential):
                        calls.append(credential)
                        if len(calls) == 2:
                            # Committed between the handshake's read and this one.
                            await _set_column(member_id, True)
                        return await real_lookup(credential)

                    monkeypatch.setattr(fleet_members, "member_for_credential",
                                        pause_before_the_second_lookup)
                    with _connect(client, credential) as worker:
                        _register(client, worker, 5, worker_id, [model])
                        _wait_for(lambda: len(calls) == 2 and worker_id in realtime.workers
                                  and realtime.workers[worker_id].paused,
                                  "the registration to read the pause")
                        assert waiting.state == "queued"
                        assert waiting.worker is None
        finally:
            _cleanup(client, worker_id)
            client.portal.call(drop_worker_rows, other_id)


@pytest.mark.parametrize("protocol", [5, 6])
def test_a_pause_during_dispatch_preparation_leaves_the_job_queued(none_mode, monkeypatch,
                                                                   protocol):
    if protocol == 6:
        root_keys(monkeypatch)
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Preparing")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, protocol, worker_id, [MANIFEST])
                storage = get_storage()
                real_target = storage.upload_target

                async def pause_while_preparing(*args, **kwargs):
                    # The route's PATCH lands while dispatch awaits its upload address.
                    realtime.workers[worker_id].paused = True
                    return await real_target(*args, **kwargs)

                monkeypatch.setattr(storage, "upload_target", pause_while_preparing)
                job_id = _queue_job(client)
                client.portal.call(jobs.dispatch_step)
                assert job_id not in jobs.inflight
                assert _job_state(client, job_id) == "queued"
                assert realtime.workers[worker_id].jobs_in_flight == 0
        finally:
            _cleanup(client, worker_id)


def test_two_pause_requests_leave_the_live_flag_as_the_database_holds_it(none_mode,
                                                                         monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Racing")
        try:
            with _connect(client, credential) as worker:
                _register(client, worker, 5, worker_id, [MANIFEST])
                _wait_for(lambda: realtime.workers[worker_id].paused is False,
                          "the registration to read the row again")

                async def scenario():
                    async with db.session_factory() as session:
                        admin = await session.get(User, db.local_user_id)
                    contended = asyncio.Event()
                    first_in = asyncio.Event()

                    class Watched(asyncio.Lock):
                        async def acquire(self):
                            if self.locked():
                                contended.set()
                            return await super().acquire()

                    monkeypatch.setattr(fleet_members, "_PAUSE_LOCK", Watched(), raising=False)
                    real_factory = db.session_factory
                    opened = []

                    class Held:
                        """The first request's session, held open after its commit."""

                        def __init__(self, session, hold):
                            self.session, self.hold = session, hold

                        async def __aenter__(self):
                            return await self.session.__aenter__()

                        async def __aexit__(self, *exc):
                            result = await self.session.__aexit__(*exc)
                            if self.hold:
                                # Long enough for the second request to finish, unless
                                # it waits for this one.
                                try:
                                    await asyncio.wait_for(contended.wait(), 2)
                                except TimeoutError:
                                    pass
                            return result

                    def factory():
                        opened.append(1)
                        if len(opened) == 1:
                            first_in.set()
                        return Held(real_factory(), hold=len(opened) == 1)

                    monkeypatch.setattr(db, "session_factory", factory)
                    pause = asyncio.create_task(fleet_members.set_member_paused(
                        uuid.UUID(member_id), fleet_members.PauseRequest(paused=True), admin))
                    await first_in.wait()
                    resume = asyncio.create_task(fleet_members.set_member_paused(
                        uuid.UUID(member_id), fleet_members.PauseRequest(paused=False), admin))
                    await asyncio.gather(pause, resume)
                    monkeypatch.setattr(db, "session_factory", real_factory)
                    async with real_factory() as session:
                        stored = (await session.execute(
                            text("SELECT paused FROM fleet_members WHERE id = :id"),
                            {"id": uuid.UUID(member_id)})).scalar_one()
                    return stored

                stored = client.portal.call(scenario)
                assert stored is False
                assert realtime.workers[worker_id].paused is stored
        finally:
            _cleanup(client, worker_id)


def test_a_member_that_is_not_paused_takes_a_waiting_session_once_it_registers(none_mode):
    model = _realtime_model()
    other_id = f"w-pause-other-{uuid.uuid4().hex[:8]}"
    with TestClient(app, base_url=ORIGIN) as client:
        member_id, worker_id, credential = _member(client, "Ready")
        try:
            with client.websocket_connect("/api/v1/fleet", headers=FLEET_HEADERS) as other:
                _register(client, other, 5, other_id, [model])
                realtime.workers[other_id].paused = True
                with client.websocket_connect("/api/v1/realtime",
                                              headers={"Origin": ORIGIN}) as browser:
                    browser.send_json({"type": "open", "model_id": model["id"]})
                    expect(browser, "queued")
                    waiting = next(s for s in realtime.sessions.values()
                                   if s.model_id == model["id"])
                    with _connect(client, credential) as worker:
                        _register(client, worker, 5, worker_id, [model])
                        # Held until its row is read again, then admitted at once:
                        # the sweep that would also find it runs every 30 seconds.
                        opened = expect(worker, "open_session")
                        assert opened["session_id"] == str(waiting.id)
        finally:
            _cleanup(client, worker_id)
            client.portal.call(drop_worker_rows, other_id)
