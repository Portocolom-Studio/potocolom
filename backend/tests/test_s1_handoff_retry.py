"""S1 handoff and retry regressions from the repair round (issue #717).

A held update or close send must never overwrite the replacement generation's
desired state, and a grant renewal must retry the unresolved journal head with
the exact original bytes while another worker's delivery is held. The handoff
tests run the real realtime endpoint with held worker sockets; the retry tests
run the real fleet path, the real journal and the real registration.
"""

import asyncio
import json
import queue
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app import command_store, db, jobs, realtime, worker_authority
from app.settings import get_settings
from test_command_store import acknowledge_bound, command_ack_fields
from test_fleet_protocol6 import (
    drop_worker_rows,
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_s1_authority_races import (
    _bound_update_session,
    _hold_journal_row,
    _join_portal_tasks,
    _marker,
    _run_delivery,
    _wait_peer_lock_wait,
)
from test_totp_flow import _make as make_account_user, accounts

__all__ = ["accounts"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _HeldSocket:
    def __init__(self, auto_release: bool = False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = asyncio.Event()
        self.auto_release = auto_release
        self.messages = []

    async def send_text(self, message):
        self.messages.append(message)
        self.started.set()
        if not self.auto_release:
            await self.release.wait()
        self.completed.set()

    async def close(self, *args, **kwargs):
        pass


class _Browser:
    """A browser socket at the real ASGI boundary.

    Controls the test sends are queued as the `websocket.receive` envelopes the
    endpoint actually reads; anything else is silently ignored by the app's
    message loop, which is exactly the shape a real socket delivers.
    """

    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []

    async def accept(self):
        pass

    async def receive(self):
        return await self.queue.get()

    async def send_json(self, value):
        self.sent.append(value)

    async def send_bytes(self, value):
        self.sent.append(value)

    async def close(self, *args, **kwargs):
        pass

    def send_control(self, control: dict) -> None:
        """Queue a text control the way the ASGI server would deliver it."""
        self.queue.put_nowait(
            {"type": "websocket.receive", "text": json.dumps(control)})

    def send_disconnect(self) -> None:
        self.queue.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def controls(self, kind: str) -> list:
        return [message for message in self.sent if message.get("type") == kind]


async def _until(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition was not met within {timeout}s")


async def _wait_marker_changed(worker_id: str, incarnation: uuid.UUID,
                               before, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _marker(worker_id, incarnation) != before:
            return True
        await asyncio.sleep(0.02)
    return False


def _renew_collecting(ws, worker_id: str, incarnation: uuid.UUID) -> list:
    """Renew through the normal grant path and keep any message that raced
    ahead of the work_grant reply."""
    ws.send_json({
        "type": "grant_request",
        "worker_id": worker_id,
        "incarnation": str(incarnation),
        "region": worker_authority.REGION,
        "grant_nonce": str(uuid.uuid4()),
    })
    stashed = []
    while True:
        message_text = ws.receive_text()
        if json.loads(message_text).get("type") == "work_grant":
            return stashed
        stashed.append(message_text)


@asynccontextmanager
async def _probe():
    """The real realtime endpoint with held worker sockets and the delivery
    owner, database authority calls replaced by in-memory doubles."""
    old = realtime.Worker(
        f"s1-probe-old-{uuid.uuid4()}", _HeldSocket(), [], 1,
        protocol_version=6, incarnation=uuid.uuid4(), grant_nonce=uuid.uuid4(),
        admission_ready=True, transport_owner_id=uuid.uuid4(),
    )
    new = realtime.Worker(
        f"s1-probe-new-{uuid.uuid4()}", _HeldSocket(auto_release=True), [], 1,
        protocol_version=6, incarnation=uuid.uuid4(), grant_nonce=uuid.uuid4(),
        admission_ready=True, transport_owner_id=uuid.uuid4(),
    )
    browser = _Browser()
    opened = []
    commit_calls = []
    desired_committed_calls = []
    pending = {"command": None}

    async def handshake(ws):
        return realtime.Handshake(user_id=uuid.uuid4())

    async def live(principal):
        return True

    async def first(ws):
        return {"type": "open", "model_id": "s1-probe",
                "params": {"prompt": "old", "seed": 1}}

    async def place(session, **kwargs):
        worker = old if not opened else new
        session.worker = worker
        session.attempt_id = uuid.uuid4()
        session.state = "live"
        worker.slots_in_use += 1
        opened.append({
            "generation": session.control_generation,
            "revision": session.desired_revision,
            "params": dict(session.desired_params),
        })
        return True

    async def commit(worker_id, incarnation, kind, fields, **kwargs):
        context = kwargs.get("session_context") or {}
        commit_calls.append({
            "kind": kind,
            "params_revision": fields.get("params_revision"),
            "base": context.get("base_desired_revision"),
            "params": dict(fields.get("params") or {}),
        })
        pending["command"] = SimpleNamespace(
            sequence=1, body=json.dumps({"type": kind, **fields}).encode())
        desired_committed = kwargs.get("desired_committed")
        if desired_committed is not None:
            accepted_params = dict(fields.get("params") or {})
            revision = int(fields["params_revision"])
            desired_committed_calls.append({
                "params": dict(accepted_params), "revision": revision,
            })
            desired_committed(dict(accepted_params), revision)
        return pending["command"]

    async def read(*args, **kwargs):
        return pending["command"]

    async def register(*args, register, **kwargs):
        return register()

    async def end(*args, **kwargs):
        pass

    with ExitStack() as stack:
        for target, value in [
            ("app.realtime.origin_allowed", lambda ws: True),
            ("app.realtime._handshake_principal", handshake),
            ("app.realtime._still_live", live),
            ("app.realtime.first_control", first),
            ("app.realtime.model_known", lambda model: True),
            ("app.registry.available", lambda: {}),
            ("app.realtime.place_session", place),
            ("app.realtime.CLOSE_TIMEOUT", 100),
            ("app.realtime._worker_delivery_admission_open", True),
            ("app.db.session_factory", None),
            ("app.command_store.commit_command", commit),
            ("app.command_store.read_exact_pending", read),
            ("app.command_store.register_delivery", register),
            ("app.worker_authority.end_realtime_session", end),
        ]:
            stack.enter_context(patch(target, value))
        realtime.workers[old.id] = old
        realtime.workers[new.id] = new
        try:
            yield SimpleNamespace(
                old=old, new=new, browser=browser, opened=opened,
                commit_calls=commit_calls,
                desired_committed_calls=desired_committed_calls)
        finally:
            realtime.workers.pop(old.id, None)
            realtime.workers.pop(new.id, None)


@pytest.mark.anyio
async def test_held_update_send_hands_over_the_committed_desired_snapshot():
    async with _probe() as probe:
        endpoint = asyncio.create_task(realtime.realtime(probe.browser))
        try:
            await _until(lambda: any(
                session.state == "live" and session.attempt_id is not None
                for session in realtime.sessions.values()))
            session = next(
                session for session in realtime.sessions.values()
                if session.state == "live" and session.attempt_id is not None)
            registered_session = session
            registered_generation = session.control_generation
            registered_attempt = session.attempt_id
            assert registered_generation == 1 and registered_attempt is not None
            probe.browser.send_control(
                {"type": "update_params", "params": {"prompt": "new"}})
            await asyncio.wait_for(probe.old.ws.started.wait(), 5)
            committed = probe.commit_calls[0]
            assert committed["kind"] == "update_session"
            assert committed["base"] == 1 and committed["params_revision"] == 2
            assert committed["params"] == {"prompt": "new", "seed": 1}
            assert probe.desired_committed_calls == [{
                "params": {"prompt": "new", "seed": 1}, "revision": 2,
            }], "the commit fake must invoke the synchronous callback with accepted values"
            assert session is registered_session
            assert session.control_generation == registered_generation
            assert session.attempt_id == registered_attempt
            assert session.desired_params == {"prompt": "new", "seed": 1}
            assert session.desired_revision == 2
            assert not probe.old.ws.completed.is_set(), (
                "the callback must update the original session before the held send completes")

            realtime.workers.pop(probe.old.id)
            await realtime.reassign(session)
            replacement = probe.opened[-1]
            assert session.control_generation == 2
            assert replacement["generation"] == 2
            assert replacement["params"] == {"prompt": "new", "seed": 1}, (
                "the replacement must open with the accepted desired parameters, "
                f"got {replacement['params']!r}")
            assert replacement["revision"] == 1, (
                "the replacement generation restarts the revision counter at 1, "
                f"got {replacement['revision']!r}")
            assert session.desired_revision == 1

            snapshot = (session.desired_revision, dict(session.desired_params),
                        dict(session.params), session.worker, session.state)
            probe.old.ws.release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert session.control_generation == 2
            assert (session.desired_revision, dict(session.desired_params),
                    dict(session.params), session.worker, session.state) == snapshot, (
                        "the stale generation-1 completion overwrote the "
                        "replacement generation")
            assert session.desired_revision == replacement["revision"], (
                "the replacement's desired revision must stay consistent with "
                "its open command")

            probe.browser.send_control(
                {"type": "update_params", "params": {"prompt": "third"}})
            await _until(lambda: len(probe.commit_calls) >= 2)
            second = probe.commit_calls[-1]
            assert second["params"] == {"prompt": "third", "seed": 1}
            assert second["base"] == 1, (
                "the next update must base on the replacement's revision 1, "
                f"got base {second['base']!r}")
            assert second["params_revision"] == 2, (
                "the next update must advance to revision 2, "
                f"got {second['params_revision']!r}")
        finally:
            probe.old.ws.release.set()
            probe.browser.send_disconnect()
            await asyncio.wait_for(
                asyncio.gather(endpoint, return_exceptions=True), 5)


@pytest.mark.anyio
async def test_held_close_send_and_cancelled_caller_leave_the_replacement_alone():
    original_idle_release = realtime.IDLE_RELEASE_SECONDS
    realtime.IDLE_RELEASE_SECONDS = 0
    try:
        async with _probe() as probe:
            endpoint = asyncio.create_task(realtime.realtime(probe.browser))
            try:
                await _until(lambda: bool(realtime.sessions))
                session = next(iter(realtime.sessions.values()))
                assert session.control_generation == 1 and session.state == "live"
                # The sweep releases a session that stopped drawing; zero the
                # input clock so the comparison is not a race against the
                # monotonic tick that created the session.
                session.last_input = 0.0

                outer = asyncio.create_task(realtime.release_idle_sessions())
                await asyncio.wait_for(probe.old.ws.started.wait(), 5)
                close_text = probe.old.ws.messages[0]
                assert json.loads(close_text)["type"] == "close_session"
                assert session.state == "idle"

                outer.cancel()
                try:
                    await outer
                except asyncio.CancelledError:
                    pass
                assert session.state == "idle" and session.control_generation == 1

                assert realtime.transition(session, "idle", "assigning")
                await realtime.resume_idle(session)
                assert session.control_generation == 2
                replacement = probe.opened[-1]
                assert replacement["generation"] == 2
                assert replacement["revision"] == 1, (
                    "the resumed generation restarts the revision counter at 1, "
                    f"got {replacement['revision']!r}")
                snapshot = (session.desired_revision, dict(session.desired_params),
                            dict(session.params), session.worker, session.state)

                probe.old.ws.release.set()
                await _until(lambda: probe.old.delivery_task is None)
                assert (session.desired_revision, dict(session.desired_params),
                        dict(session.params), session.worker, session.state) == snapshot, (
                            "the stale close completion overwrote the replacement "
                            "generation")
                assert probe.old.ws.messages == [close_text], (
                    "the original close bytes must be sent exactly once")
                assert probe.new.ws.messages == [], (
                    "the replacement worker must receive no stale close")
            finally:
                probe.old.ws.release.set()
                probe.browser.send_disconnect()
                await asyncio.wait_for(
                    asyncio.gather(endpoint, return_exceptions=True), 5)
    finally:
        realtime.IDLE_RELEASE_SECONDS = original_idle_release


async def _own_job(job_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
            {"user_id": user_id, "job_id": job_id},
        )
        await session.commit()


async def _command_state(worker_id: str, incarnation: uuid.UUID, sequence: int) -> str:
    async with db.session_factory() as session:
        return await session.scalar(text(
            "SELECT state FROM worker_commands WHERE worker_id = :worker_id "
            "AND incarnation = :incarnation AND command_sequence = :sequence"
        ), {"worker_id": worker_id, "incarnation": incarnation, "sequence": sequence})


@pytest.mark.db
def test_grant_renewal_retries_the_pending_head_while_another_worker_is_held(monkeypatch):
    root_keys(monkeypatch)
    held_worker_id = f"s1-retry-held-{uuid.uuid4()}"
    live_worker_id = f"s1-retry-live-{uuid.uuid4()}"
    held_incarnation = uuid.uuid4()
    live_incarnation = uuid.uuid4()
    model_id = f"s1-retry-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)

    with protocol6_client(held_worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        held_fleet = client.websocket_connect("/api/v1/fleet")
        held_ws = held_fleet.__enter__()
        live_fleet = None
        held_release = asyncio.Event()
        held_held = asyncio.Event()
        held_done = asyncio.Event()
        held_outcome = {}
        held_tasks = []
        try:
            held_ws.send_json(hello(held_worker_id, [manifest(model_id)],
                                    incarnation=held_incarnation))
            held_ws.receive_json()
            request_work_grant(held_ws, held_worker_id, held_incarnation)
            held_job = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, held_job) is True
            held_first_text = held_ws.receive_text()

            live_fleet = client.websocket_connect("/api/v1/fleet")
            live_ws = live_fleet.__enter__()
            live_ws.send_json(hello(live_worker_id, [manifest(model_id)],
                                    incarnation=live_incarnation))
            live_ws.receive_json()
            request_work_grant(live_ws, live_worker_id, live_incarnation)
            owner = client.portal.call(
                make_account_user, f"s1-retry-owner-{uuid.uuid4()}@example.test")
            live_job = client.portal.call(seed_queued_job, model_id)
            client.portal.call(_own_job, live_job, owner.id)
            assert client.portal.call(jobs.dispatch, live_job) is True
            live_first_text = live_ws.receive_text()

            # A renewal inside the one-second throttle window must not resend.
            live_before = _marker(live_worker_id, live_incarnation)
            request_work_grant(live_ws, live_worker_id, live_incarnation)
            time.sleep(0.5)
            assert _marker(live_worker_id, live_incarnation) == live_before, (
                "the one-second retry throttle must suppress a renewal kick "
                "inside its window")

            time.sleep(1.0)  # both first sends are now older than the throttle
            holder_task = client.portal.start_task_soon(
                _hold_journal_row, held_worker_id, held_incarnation,
                json.loads(held_first_text)["command_sequence"],
                held_held, held_release)
            held_tasks.append(holder_task)
            client.portal.call(asyncio.wait_for, held_held.wait(), 10)
            delivery_task = client.portal.start_task_soon(
                _run_delivery, realtime.workers[held_worker_id],
                held_outcome, held_done)
            held_tasks.append(delivery_task)
            blocked = client.portal.call(_wait_peer_lock_wait, None, 1.5)
            assert blocked, (
                "the held worker's delivery never reached its journal wait; "
                f"outcome={held_outcome!r}")

            live_before = _marker(live_worker_id, live_incarnation)
            stashed = _renew_collecting(live_ws, live_worker_id, live_incarnation)
            retry_seen = client.portal.call(
                _wait_marker_changed, live_worker_id, live_incarnation,
                live_before, 2.5)
            assert retry_seen, (
                "grant renewal did not retry the unresolved head: no send was "
                "recorded after the renewal")
            assert not held_done.is_set(), (
                "the healthy worker's retry must proceed while the other "
                "worker's delivery is held")
            retry_text = stashed.pop(0) if stashed else live_ws.receive_text()
            assert not stashed, f"unexpected extra messages: {stashed!r}"
            assert retry_text == live_first_text, (
                "the retry must carry the exact original bytes")

            ack = {**command_ack_fields(json.loads(retry_text)),
                  "type": "command_ack", "status": "accepted"}
            recorded = client.portal.call(
                acknowledge_bound, ack, live_worker_id, live_incarnation)
            assert recorded.recorded is True, (
                "the original ACK projection must still record the retried head")

            started_at = time.monotonic()
            try:
                request_work_grant(held_ws, held_worker_id, held_incarnation)
            except AssertionError as error:
                raise AssertionError(
                    "the fleet reader awaited the held delivery instead of "
                    "answering the renewal") from error
            assert time.monotonic() - started_at < 5, (
                "the renewal answer must not wait on the held delivery")
            assert not held_done.is_set()

            client.portal.call(held_release.set)
            client.portal.call(asyncio.wait_for, held_done.wait(), 15)
            assert "error" not in held_outcome, held_outcome
            held_retry_text = held_ws.receive_text()
            assert held_retry_text == held_first_text, (
                "the held worker's own retry must carry the exact original bytes")
        finally:
            try:
                _join_portal_tasks(client, [held_release], held_tasks)
            finally:
                try:
                    held_fleet.__exit__(None, None, None)
                finally:
                    if live_fleet is not None:
                        live_fleet.__exit__(None, None, None)


@pytest.mark.db
def test_fleet_ack_retries_next_command_without_blocking_reader_or_other_worker(monkeypatch):
    root_keys(monkeypatch)
    monkeypatch.setattr(realtime, "CLOSE_TIMEOUT", 60)
    first_id = f"s1-ack-held-{uuid.uuid4()}"
    second_id = f"s1-ack-live-{uuid.uuid4()}"
    first_incarnation = uuid.uuid4()
    second_incarnation = uuid.uuid4()
    model_id = f"s1-ack-model-{uuid.uuid4()}"
    job_ids = []
    portal_tasks = []
    background_actors = []
    pool = ThreadPoolExecutor(max_workers=4)
    second_registered = False

    async def park_dispatch():
        return None

    async def command_row(worker_id, incarnation, sequence):
        async with db.session_factory() as session:
            return (await session.execute(
                text(
                    "SELECT state, body_hash, command_id, encrypted_body "
                    "FROM worker_commands WHERE worker_id = :worker_id "
                    "AND incarnation = :incarnation AND command_sequence = :sequence"
                ),
                {"worker_id": worker_id, "incarnation": incarnation,
                 "sequence": sequence},
            )).mappings().one_or_none()

    async def wait_ack_projection(worker_id, incarnation, sequence):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            async with db.session_factory() as session:
                row = (await session.execute(
                    text(
                        "SELECT acknowledged_floor, EXISTS ("
                        "SELECT 1 FROM worker_commands WHERE worker_id = :worker_id "
                        "AND incarnation = :incarnation AND command_sequence = :sequence"
                        ") AS command_present FROM worker_connections "
                        "WHERE worker_id = :worker_id AND incarnation = :incarnation"
                    ),
                    {"worker_id": worker_id, "incarnation": incarnation,
                     "sequence": sequence},
                )).mappings().one_or_none()
            if (row is not None and row["acknowledged_floor"] >= sequence
                    and not row["command_present"]):
                return row["acknowledged_floor"], row["command_present"]
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"worker {worker_id} did not project ACK for command {sequence}")

    async def delivery_state(worker_id):
        worker = realtime.workers.get(worker_id)
        owners = tuple(task for task, owner in realtime._worker_delivery_owners.items()
                       if owner.id == worker_id)
        return worker, owners

    async def wait_delivery_idle(worker_ids):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if all(
                (worker := realtime.workers.get(worker_id)) is None
                or (worker.delivery_task is None and worker.delivery_send_task is None
                    and not any(owner.id == worker_id
                                for owner in realtime._worker_delivery_owners.values()))
                for worker_id in worker_ids
            ):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("fleet delivery owners did not finish")

    async def wait_workers_gone(worker_ids):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if all(worker_id not in realtime.workers for worker_id in worker_ids):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("fleet socket handlers did not release their Workers")

    async def join_delivery_owners(worker_ids):
        owners = [task for task, worker in list(realtime._worker_delivery_owners.items())
                  if worker.id in worker_ids]
        return await asyncio.gather(*owners, return_exceptions=True)

    def portal_result(function, *args, timeout=10):
        future = client.portal.start_task_soon(function, *args)
        portal_tasks.append(future)
        return future.result(timeout=timeout)

    def actor_result(function, *args, timeout=10):
        future = pool.submit(function, *args)
        background_actors.append(future)
        return future.result(timeout=timeout)

    def receive_until_grant(ws, stashed):
        while True:
            message = ws.receive_text()
            decoded = json.loads(message)
            if decoded.get("type") == "work_grant":
                return decoded
            stashed.put(message)

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)

    with protocol6_client(first_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        first_fleet = client.websocket_connect("/api/v1/fleet")
        first_ws = first_fleet.__enter__()
        second_fleet = None
        gate_entered = client.portal.call(asyncio.Event)
        gate_release = client.portal.call(asyncio.Event)
        held_messages = []
        head_replays = []
        original_send = None
        held_worker = None
        stashed_messages = queue.Queue()
        try:
            first_ws.send_json(hello(
                first_id, [manifest(model_id)], incarnation=first_incarnation))
            assert actor_result(first_ws.receive_json)["type"] == "registered"
            actor_result(request_work_grant, first_ws, first_id, first_incarnation)
            held_worker = realtime.workers[first_id]

            job_one = client.portal.call(seed_queued_job, model_id)
            job_ids.append(job_one)
            dispatch_one = client.portal.start_task_soon(jobs.dispatch, job_one)
            portal_tasks.append(dispatch_one)
            first_text = actor_result(first_ws.receive_text)
            assert dispatch_one.result(timeout=10) is True
            first_command = json.loads(first_text)
            assert first_command["type"] == "dispatch_job"
            assert first_command["command_sequence"] == 1

            original_send = held_worker.ws.send_text

            async def hold_sequence_two(message):
                sequence = json.loads(message).get("command_sequence")
                if sequence == 1:
                    head_replays.append(message)
                elif sequence == 2:
                    held_messages.append(message)
                    gate_entered.set()
                    await gate_release.wait()
                return await original_send(message)

            held_worker.ws.send_text = hold_sequence_two
            job_two = client.portal.call(seed_queued_job, model_id)
            job_ids.append(job_two)
            dispatch_two = client.portal.start_task_soon(jobs.dispatch, job_two)
            portal_tasks.append(dispatch_two)
            dispatch_two_result = dispatch_two.result(timeout=10)
            second_row = portal_result(
                command_row, first_id, first_incarnation, 2)
            assert second_row is not None, (
                "dispatch two did not persist a command; "
                f"jobs.dispatch returned {dispatch_two_result!r}")
            assert second_row["state"] == "pending"
            assert second_row["encrypted_body"] is not None
            for _ in head_replays:
                assert actor_result(first_ws.receive_text) == first_text
            ack_one = {
                **command_ack_fields(first_command),
                "type": "command_ack",
                "status": "accepted",
            }
            first_ws.send_json(ack_one)
            client.portal.call(asyncio.wait_for, gate_entered.wait(), 10)
            assert len(held_messages) == 1

            pending_two = portal_result(
                command_store.read_exact_pending, first_id, first_incarnation)
            assert pending_two is not None and pending_two.sequence == 2
            assert pending_two.command_id == second_row["command_id"]
            assert pending_two.body_hash == second_row["body_hash"]
            assert json.loads(pending_two.body)["job_id"] == str(job_two)
            assert held_messages[0] == pending_two.body.decode("utf-8")
            tracked_worker, owners = portal_result(
                delivery_state, first_id)
            assert tracked_worker is held_worker
            assert tracked_worker.delivery_task is not None
            assert owners == (tracked_worker.delivery_task,), (
                "ACK-triggered send must have one tracked delivery owner")
            assert tracked_worker.delivery_registered

            grant_nonce = uuid.uuid4()
            first_ws.send_json({
                "type": "grant_request",
                "worker_id": first_id,
                "incarnation": str(first_incarnation),
                "region": worker_authority.REGION,
                "grant_nonce": str(grant_nonce),
            })
            grant_actor = pool.submit(
                receive_until_grant, first_ws, stashed_messages)
            background_actors.append(grant_actor)
            try:
                grant_reply = grant_actor.result(timeout=2)
            except FutureTimeoutError:
                grant_reply = None

            second_fleet = client.websocket_connect("/api/v1/fleet")
            second_ws = second_fleet.__enter__()
            second_ws.send_json(hello(
                second_id, [manifest(model_id)], incarnation=second_incarnation))
            assert actor_result(second_ws.receive_json)["type"] == "registered"
            second_registered = True
            actor_result(request_work_grant, second_ws, second_id, second_incarnation)

            job_three = client.portal.call(seed_queued_job, model_id)
            job_ids.append(job_three)
            dispatch_three = client.portal.start_task_soon(jobs.dispatch, job_three)
            portal_tasks.append(dispatch_three)
            third_text = actor_result(second_ws.receive_text)
            dispatch_three.result(timeout=10)
            third_command = json.loads(third_text)
            assert third_command["type"] == "dispatch_job"
            assert third_command["job_id"] == str(job_three)
            ack_three = {
                **command_ack_fields(third_command),
                "type": "command_ack",
                "status": "accepted",
            }
            second_ws.send_json(ack_three)
            third_floor, third_command_present = portal_result(
                wait_ack_projection, second_id, second_incarnation,
                third_command["command_sequence"])
            assert third_floor >= third_command["command_sequence"]
            assert not third_command_present
            assert not client.portal.call(gate_release.is_set), (
                "the first worker's send gate must remain held during other-worker progress")

            assert grant_reply is not None, (
                "the fleet reader blocked on the ACK-triggered send instead of "
                "answering a same-worker grant request")
            assert grant_reply["worker_id"] == first_id
            assert grant_reply["incarnation"] == str(first_incarnation)
            assert grant_reply["grant_nonce"] == str(grant_nonce)
            assert grant_reply["ready"] is True
            assert realtime.workers.get(first_id) is held_worker
            assert held_worker.incarnation == first_incarnation
            assert held_worker.delivery_admission_open

            client.portal.call(gate_release.set)
            retry_text = actor_result(first_ws.receive_text)
            assert retry_text == pending_two.body.decode("utf-8"), (
                "the retry must carry the exact original decrypted command bytes")
            assert held_messages == [retry_text], (
                "the ACK reader must send the pending command exactly once")
            retry_command = json.loads(retry_text)
            assert retry_command["command_sequence"] == pending_two.sequence
            ack_two = {
                **command_ack_fields(retry_command),
                "type": "command_ack",
                "status": "accepted",
            }
            first_ws.send_json(ack_two)
            second_floor, second_command_present = portal_result(
                wait_ack_projection, first_id, first_incarnation,
                pending_two.sequence)
            assert second_floor >= pending_two.sequence
            assert not second_command_present
            portal_result(wait_delivery_idle, (first_id, second_id), timeout=10)
            idle_worker, idle_owners = portal_result(delivery_state, first_id)
            assert idle_worker.delivery_task is None
            assert idle_worker.delivery_send_task is None
            assert idle_owners == ()
        finally:
            cleanup_errors = []
            preserve_primary = sys.exc_info()[0] is not None
            try:
                client.portal.call(gate_release.set)
            except BaseException as error:
                cleanup_errors.append(error)
            if original_send is not None and held_worker is not None:
                held_worker.ws.send_text = original_send
            for fleet in (first_fleet, second_fleet):
                if fleet is not None:
                    try:
                        fleet.__exit__(None, None, None)
                    except BaseException as error:
                        cleanup_errors.append(error)
            try:
                gone_future = client.portal.start_task_soon(
                    wait_workers_gone, (first_id, second_id))
                portal_tasks.append(gone_future)
                gone_future.result(timeout=10)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                owner_future = client.portal.start_task_soon(
                    join_delivery_owners, (first_id, second_id))
                portal_tasks.append(owner_future)
            except BaseException as error:
                owner_future = None
                cleanup_errors.append(error)
            try:
                _join_portal_tasks(client, [], portal_tasks, timeout=15)
            except BaseException as error:
                cleanup_errors.append(error)
            if owner_future is not None:
                try:
                    cleanup_errors.extend(
                        result for result in owner_future.result(timeout=15)
                        if isinstance(result, BaseException)
                    )
                except BaseException as error:
                    cleanup_errors.append(error)
            for actor in background_actors:
                try:
                    actor.result(timeout=10)
                except BaseException as error:
                    cleanup_errors.append(error)
            async def clean_test_rows():
                for job_id in job_ids:
                    jobs.inflight.pop(job_id, None)
                    jobs.live_progress.pop(job_id, None)
                    jobs.last_progress_at.pop(job_id, None)
                async with db.session_factory() as session:
                    for job_id in job_ids:
                        await session.execute(
                            text("DELETE FROM job_attempts WHERE job_id = :job_id"),
                            {"job_id": job_id},
                        )
                        await session.execute(
                            text("DELETE FROM jobs WHERE id = :job_id"),
                            {"job_id": job_id},
                        )
                    for worker_id, incarnation in (
                        (first_id, first_incarnation),
                        (second_id, second_incarnation),
                    ):
                        await session.execute(
                            text("DELETE FROM worker_commands WHERE worker_id = :worker_id "
                                 "AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                        await session.execute(
                            text("DELETE FROM worker_connections WHERE worker_id = :worker_id "
                                 "AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                    await session.commit()
            try:
                cleanup_future = client.portal.start_task_soon(clean_test_rows)
                portal_tasks.append(cleanup_future)
                cleanup_future.result(timeout=15)
            except BaseException as error:
                cleanup_errors.append(error)
            if second_registered:
                try:
                    client.portal.call(drop_worker_rows, second_id)
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except BaseException as error:
                cleanup_errors.append(error)
            if cleanup_errors and not preserve_primary:
                raise cleanup_errors[0]


@pytest.mark.db
def test_grant_renewal_after_erasure_registers_nothing_and_sends_nothing(
        accounts, monkeypatch):
    worker_id = f"s1-retry-erased-{uuid.uuid4()}"
    model_id = f"s1-retry-erased-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        issued, _auth, worker_ws, worker, update, _first_text, browser_ws, cleanup = (
            _bound_update_session(client, worker_id, model_id)
        )
        try:
            time.sleep(1.5)  # the one-second retry throttle on the pending head
            response = client.post(
                "/api/v1/auth/logout",
                headers={"origin": get_settings().public_url,
                         "x-csrf-token": issued.csrf},
            )
            assert response.status_code == 204
            state = client.portal.call(
                _command_state, worker_id, worker.incarnation,
                update["command_sequence"])
            assert state == "erased", "logout must erase the pending update body"
            assert worker_id not in realtime.workers, (
                "logout must fence the worker out of the live fleet")
            assert not worker.delivery_admission_open, (
                "logout must close the fenced worker's delivery admission")

            sends = []
            original_send = worker.ws.send_text

            async def track_send(message):
                sends.append(message)
                await original_send(message)

            monkeypatch.setattr(worker.ws, "send_text", track_send)
            try:
                request_work_grant(worker_ws, worker_id, worker.incarnation)
            except WebSocketDisconnect:
                # Expected: logout fenced this worker and closed its socket, so
                # the renewal is answered by the close rather than a grant.
                pass
            owners_before = set(realtime._worker_delivery_owners)
            refused = client.portal.call(
                asyncio.wait_for,
                realtime._deliver_pending_worker_command(worker), 10)
            assert refused is None, (
                "the erased head must not start a delivery: " + repr(refused))
            assert set(realtime._worker_delivery_owners) == owners_before, (
                "the erased head must register no delivery owner")
            time.sleep(1.5)
            assert sends == [], "renewal sent an erased command"
            state = client.portal.call(
                _command_state, worker_id, worker.incarnation,
                update["command_sequence"])
            assert state == "erased", (
                "the erased body must stay erased after the renewal attempt")
        finally:
            cleanup.close()


@pytest.mark.db
def test_grant_renewal_after_shutdown_admission_starts_no_delivery(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-retry-shutdown-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-retry-shutdown-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)

    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as ws:
            ws.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            ws.receive_json()
            request_work_grant(ws, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            ws.receive_text()
            time.sleep(1.5)  # the one-second retry throttle on the pending head
            realtime.stop_worker_deliveries()
            try:
                marker_before = _marker(worker_id, incarnation)
                request_work_grant(ws, worker_id, incarnation)
                time.sleep(1.5)
                assert _marker(worker_id, incarnation) == marker_before, (
                    "renewal started a delivery after shutdown closed admission")
                assert all(owner.id != worker_id
                           for owner in realtime._worker_delivery_owners.values()), (
                               "shutdown must leave no tracked delivery owner for "
                               "the worker")
            finally:
                realtime.start_worker_deliveries()
