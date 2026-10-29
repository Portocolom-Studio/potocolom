import asyncio
import json
import time
import uuid
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import command_store, command_subjects, db, jobs, realtime, sessions as account_sessions, worker_authority
from app.account_lock import hold_the_account
from app import main as main_module
from app.protocol6 import decode_control
from app.settings import get_settings
from app.tables import User
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    drop_worker_rows,
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
    seed_queued_job,
)
from test_s1_handoff_retry import _Browser, _HeldSocket
from test_totp_flow import accounts
from test_v6_sessions import command_ack, open_live_session, session_ready_message

__all__ = ["accounts"]


@pytest.fixture(autouse=True)
def park_dispatch_loop(monkeypatch):
    async def parked():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", parked)


async def _wait(event, seconds=8):
    await asyncio.wait_for(event.wait(), seconds)


async def _join(task, seconds=8):
    return await asyncio.wait_for(asyncio.shield(task), seconds)


def _authenticate_browser(client):
    async def local_user():
        async with db.session_factory() as session:
            return await session.get(User, db.local_user_id)

    user = client.portal.call(local_user)
    issued = client.portal.call(account_sessions.mint, user, False)
    cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
    client.cookies.set(cookie, issued.token)
    client.cookies.set(csrf, issued.csrf)


def _receive_json(websocket, seconds=8):
    message = websocket.portal.call(
        asyncio.wait_for, websocket._send_rx.receive(), seconds
    )
    websocket._raise_on_close(message)
    return json.loads(message["text"])


def _receive_text(websocket, seconds=8):
    message = websocket.portal.call(
        asyncio.wait_for, websocket._send_rx.receive(), seconds
    )
    websocket._raise_on_close(message)
    return message["text"]


async def _drain(seconds=8):
    await asyncio.wait_for(realtime.drain_worker_deliveries(), seconds)


async def _force_worker_socket_close(worker, seconds=3):
    await asyncio.wait_for(worker.ws.close(), seconds)


async def _worker_state(worker_id, incarnation):
    async with db.session_factory() as session:
        return await session.scalar(
            text("SELECT lifecycle FROM worker_connections WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )


async def _wait_worker_ended(worker_id, incarnation, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await _worker_state(worker_id, incarnation) == "ended":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("durable worker connection did not end")


async def _wait_worker_removed(worker_id, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if worker_id not in realtime.workers:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("worker remained in realtime after disconnect")


async def _wait_job_queued(job_id, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        async with db.session_factory() as session:
            state = await session.scalar(
                text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id}
            )
        if state == "queued":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("worker-owned job was not requeued")


def test_late_protocol6_finalizer_preserves_same_id_replacement(monkeypatch):
    async def scenario():
        worker_id = f"s1-finalizer-replacement-{uuid.uuid4()}"
        old = realtime.Worker(
            worker_id, _HeldSocket(), [], 1,
            protocol_version=6, incarnation=uuid.uuid4(), grant_nonce=uuid.uuid4(),
        )
        new = realtime.Worker(
            worker_id, _HeldSocket(auto_release=True), [], 1,
            protocol_version=6, incarnation=uuid.uuid4(), grant_nonce=uuid.uuid4(),
        )
        assert old.incarnation != new.incarnation
        old_session = realtime.Session(uuid.uuid4(), "model", _Browser(), worker=old,
                                       state="assigning", attempt_ok=True)
        new_session = realtime.Session(uuid.uuid4(), "model", _Browser(), worker=new,
                                       state="live", attempt_ok=True)
        old_job, new_job, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        old_entry = jobs.InFlight(old, "old", "old-thumb", user_id, "old-token")
        new_entry = jobs.InFlight(new, "new", "new-thumb", user_id, "new-token")
        old_key = (old.id, old.incarnation)
        new_key = (new.id, new.incarnation)
        sequences = {old_key: (1, 1.0), new_key: (2, 2.0)}
        closing = {
            (old_session.id, 1): (user_id, "model", old),
            (new_session.id, 1): (user_id, "model", new),
        }
        closed = []

        async def close_worker(target_id, incarnation):
            closed.append((target_id, incarnation))

        monkeypatch.setattr(realtime, "workers", {worker_id: old})
        monkeypatch.setattr(realtime, "sessions", {old_session.id: old_session})
        monkeypatch.setattr(realtime, "closing_sessions", closing)
        monkeypatch.setattr(realtime, "_sent_command_sequences", sequences)
        monkeypatch.setattr(realtime, "_worker_delivery_owners", {})
        monkeypatch.setattr(realtime, "_reassign_tasks", set())
        monkeypatch.setattr(jobs, "inflight", {old_job: old_entry, new_job: new_entry})
        monkeypatch.setattr(jobs, "lost_jobs", [])
        monkeypatch.setattr(jobs, "live_progress", {})
        monkeypatch.setattr(jobs, "last_progress_at", {})
        monkeypatch.setattr(worker_authority, "close_worker", close_worker)
        cleanup_waiting = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def delayed_cleanup():
            cleanup_waiting.set()
            await release_cleanup.wait()
            await realtime._finalize_worker(old)

        finalizer = asyncio.create_task(delayed_cleanup())
        try:
            await asyncio.wait_for(cleanup_waiting.wait(), 8)
            assert not finalizer.done()
            assert realtime.workers.pop(worker_id) is old
            realtime.workers[worker_id] = new
            realtime.sessions[new_session.id] = new_session
            release_cleanup.set()
            await asyncio.wait_for(finalizer, 8)

            assert realtime.workers[worker_id] is new
            assert old_session.ready.is_set()
            assert old_session.attempt_ok is False
            assert realtime.sessions[new_session.id] is new_session
            assert new_session.worker is new
            assert new_session.state == "live"
            assert new_session.attempt_ok is True
            assert old_job not in jobs.inflight
            assert jobs.inflight[new_job] is new_entry
            assert jobs.lost_jobs == [old_job]
            assert old_key not in sequences
            assert sequences[new_key] == (2, 2.0)
            assert (old_session.id, 1) not in closing
            assert closing[(new_session.id, 1)][2] is new
            assert closed == [(worker_id, old.incarnation)]
        finally:
            release_cleanup.set()
            await asyncio.wait_for(finalizer, 8)
            tasks = list(realtime._reassign_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.db
def test_disconnect_cleanup_survives_a_failing_delivery_owner(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-finalizer-error-{uuid.uuid4()}"
    model_id = f"s1-finalizer-error-model-{uuid.uuid4()}"
    prep_started = asyncio.Event()
    release_prep = asyncio.Event()
    owner_join_entered = asyncio.Event()
    lost_workers = []
    original_lost = jobs.on_worker_lost
    original_close = worker_authority.close_worker
    original_run_to_completion = getattr(db, "run_to_completion", None)

    with protocol6_client(worker_id) as client:
        _authenticate_browser(client)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        session_id = uuid.UUID(open_command["session_id"])
        job_id = None
        delivery = None
        closing = None
        try:
            job_id = client.portal.call(seed_queued_job, model_id)
            async def dispatch_job():
                return await asyncio.wait_for(jobs.dispatch(job_id), 8)

            assert client.portal.call(dispatch_job) is True
            assert jobs.inflight[job_id].worker is worker

            async def failing_preparation():
                prep_started.set()
                await asyncio.wait_for(release_prep.wait(), 8)
                raise command_store.CommandRefused("test owner preparation refusal")

            async def observed_run_to_completion(task):
                if (worker_id not in realtime.workers
                        and any(owner is worker and owned is not asyncio.current_task()
                                for owned, owner in realtime._worker_delivery_owners.items())):
                    owner_join_entered.set()
                return await original_run_to_completion(task)

            def observed_lost(target):
                if target is worker:
                    lost_workers.append(target)
                return original_lost(target)

            monkeypatch.setattr(jobs, "on_worker_lost", observed_lost)
            if original_run_to_completion is not None:
                monkeypatch.setattr(db, "run_to_completion", observed_run_to_completion)

            async def start_failing_delivery():
                return await realtime._deliver_pending_worker_command(
                    worker, prepare=failing_preparation
                )

            delivery = client.portal.start_task_soon(start_failing_delivery)
            client.portal.call(_wait, prep_started)
            closing = client.portal.start_task_soon(
                asyncio.to_thread, fleet.__exit__, None, None, None
            )
            if original_run_to_completion is not None:
                client.portal.call(_wait, owner_join_entered)
            client.portal.call(_wait_worker_removed, worker_id)
            client.portal.call(release_prep.set)
            try:
                delivery.result(timeout=8)
            except command_store.CommandRefused:
                pass
            client.portal.call(_wait_worker_ended, worker_id, worker.incarnation)
            assert lost_workers == [worker]
            assert job_id not in jobs.inflight
            client.portal.call(jobs._dispatch_step_body)
            client.portal.call(_wait_job_queued, job_id, 8)
            state = client.portal.call(
                lambda: asyncio.sleep(0, result=realtime.sessions.get(session_id))
            )
            assert state is None or state.state != "live"
        finally:
            client.portal.call(release_prep.set)
            try:
                if delivery is not None:
                    try:
                        delivery.result(timeout=8)
                    except command_store.CommandRefused:
                        pass
            finally:
                try:
                    client.portal.call(_wait_worker_ended, worker_id, worker.incarnation, 8)
                except AssertionError:
                    try:
                        client.portal.call(asyncio.wait_for, original_close(worker_id, worker.incarnation), 8)
                    except BaseException:
                        pass
                try:
                    client.portal.call(_drain, 8)
                except BaseException:
                    pass
                finally:
                    try:
                        client.portal.call(_force_worker_socket_close, worker, 3)
                    except BaseException:
                        pass
                    finally:
                        try:
                            if closing is not None:
                                closing.result(timeout=8)
                            else:
                                fleet.__exit__(None, None, None)
                        finally:
                            browser.__exit__(None, None, None)


@pytest.mark.db
def test_cancelled_disconnect_still_closes_durably(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-finalizer-cancel-{uuid.uuid4()}"
    model_id = f"s1-finalizer-cancel-model-{uuid.uuid4()}"
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    lost_workers = []
    closing_task = []
    original_close = worker_authority.close_worker
    original_lost = jobs.on_worker_lost

    with protocol6_client(worker_id) as client:
        _authenticate_browser(client)
        worker_ws, worker, _grant, _open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )

        async def held_close(target_id, incarnation):
            closing_task.append(asyncio.current_task())
            close_started.set()
            await asyncio.wait_for(release_close.wait(), 8)
            return await original_close(target_id, incarnation)

        def observed_lost(target):
            if target is worker:
                lost_workers.append(target)
            return original_lost(target)

        monkeypatch.setattr(worker_authority, "close_worker", held_close)
        monkeypatch.setattr(jobs, "on_worker_lost", observed_lost)
        original_run_to_completion = getattr(db, "run_to_completion", None)
        fleet_task = []
        if original_run_to_completion is not None:
            async def observed_run_to_completion(task):
                coroutine = getattr(task, "get_coro", lambda: None)()
                if getattr(coroutine, "__qualname__", None) == "_finalize_worker":
                    fleet_task.append(asyncio.current_task())
                return await original_run_to_completion(task)

            monkeypatch.setattr(db, "run_to_completion", observed_run_to_completion)
        closing = None
        try:
            closing = client.portal.start_task_soon(
                asyncio.to_thread, fleet.__exit__, None, None, None
            )
            client.portal.call(_wait, close_started)
            target = fleet_task[0] if fleet_task else closing_task[0]
            target.cancel()
            target.cancel()
            client.portal.call(release_close.set)
            client.portal.call(_wait_worker_ended, worker_id, worker.incarnation)
            assert lost_workers == [worker]
        finally:
            client.portal.call(release_close.set)
            try:
                client.portal.call(_wait_worker_ended, worker_id, worker.incarnation, 8)
            except AssertionError:
                try:
                    client.portal.call(asyncio.wait_for, original_close(worker_id, worker.incarnation), 8)
                except BaseException:
                    pass
            try:
                client.portal.call(_drain, 8)
            except BaseException:
                pass
            try:
                client.portal.call(_force_worker_socket_close, worker, 3)
            except BaseException:
                pass
            try:
                if closing is not None:
                    closing.result(timeout=8)
                else:
                    fleet.__exit__(None, None, None)
            finally:
                browser.__exit__(None, None, None)


@pytest.mark.db
def test_shutdown_waits_for_the_disconnect_finalizer(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-finalizer-shutdown-{uuid.uuid4()}"
    model_id = f"s1-finalizer-shutdown-model-{uuid.uuid4()}"
    close_started = asyncio.Event()
    release_close = Event()
    events = []
    original_close = worker_authority.close_worker
    original_dispose = db.dispose
    original_drain = main_module.drain_worker_deliveries

    async def held_close(target_id, incarnation):
        close_started.set()
        released = await asyncio.to_thread(release_close.wait, 8)
        assert released, "shutdown did not release the held worker close"
        result = await original_close(target_id, incarnation)
        events.append("close-returned")
        return result

    async def observed_dispose():
        events.append("dispose")
        return await original_dispose()

    async def remove_worker_rows_if_present():
        assert db.session_factory is not None
        async with db.session_factory() as session:
            present = await session.scalar(
                text("SELECT count(*) FROM workers WHERE worker_id = :worker_id"),
                {"worker_id": worker_id},
            )
        if present:
            await drop_worker_rows(worker_id)

    async def observed_drain():
        pending = [
            task
            for task, owner in realtime._worker_delivery_owners.items()
            if owner is worker and not task.done()
        ]
        events.append("finalizer-pending-at-drain" if pending else "finalizer-not-pending-at-drain")
        drain = asyncio.create_task(original_drain())
        await asyncio.sleep(0)
        events.append("drain-waited-for-finalizer" if not drain.done() else "drain-returned-early")
        release_close.set()
        return await drain

    monkeypatch.setattr(worker_authority, "close_worker", held_close)
    monkeypatch.setattr(db, "dispose", observed_dispose)
    monkeypatch.setattr(main_module, "drain_worker_deliveries", observed_drain)
    client = TestClient(main_module.app, headers=FLEET_HEADERS)
    entered = False
    exited = False
    browser_exited = False
    browser = None
    fleet = None
    worker = None
    closing = None
    try:
        client.__enter__()
        entered = True
        _authenticate_browser(client)
        worker_ws, worker, _grant, _open_command, browser_ws, browser, fleet = open_live_session(
            client, worker_id, model_id
        )
        closing = client.portal.start_task_soon(asyncio.to_thread, fleet.__exit__, None, None, None)
        client.portal.call(_wait, close_started)
        browser.__exit__(None, None, None)
        browser_exited = True
        try:
            client.__exit__(None, None, None)
        finally:
            exited = True
        closing.result(timeout=8)
        assert "finalizer-pending-at-drain" in events
        assert "drain-waited-for-finalizer" in events
        assert "drain-returned-early" not in events
        assert events.index("close-returned") < events.index("dispose")
    finally:
        release_close.set()
        if browser is not None and not browser_exited:
            try:
                browser.__exit__(None, None, None)
            except BaseException:
                pass
        if fleet is not None and closing is None:
            try:
                fleet.__exit__(None, None, None)
            except BaseException:
                pass
        if entered and not exited:
            try:
                client.__exit__(None, None, None)
            except BaseException:
                pass
        if closing is not None:
            try:
                closing.result(timeout=8)
            except BaseException:
                pass
        if entered:
            if db.session_factory is None:
                accounts(db.connect())
            accounts(remove_worker_rows_if_present())
            accounts(db.dispose())


@pytest.mark.db
def test_held_send_does_not_delay_browser_reassignment(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-finalizer-held-send-{uuid.uuid4()}"
    model_id = f"s1-finalizer-held-send-model-{uuid.uuid4()}"
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    with protocol6_client(worker_id) as client:
        _authenticate_browser(client)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        session_one_id = uuid.UUID(open_command["session_id"])
        browser_two = None
        browser_two_ws = None
        delivery = None
        closing = None
        try:
            worker.realtime_slots = 2
            browser_two = client.websocket_connect("/api/v1/realtime")
            browser_two_ws = browser_two.__enter__()
            browser_two_ws.send_json({
                "type": "open", "model_id": model_id,
                "params": {"prompt": "second"}, "frame_header": 2,
            })
            second_open = decode_control(_receive_text(worker_ws).encode("utf-8"))
            assert second_open["type"] == "open_session"
            worker_ws.send_json(command_ack(second_open))
            worker_ws.send_json(session_ready_message(worker, second_open, worker.grant_nonce))
            session_two_id = uuid.UUID(second_open["session_id"])
            second_session = realtime.sessions[session_two_id]
            _receive_json(browser_two_ws)
            original_send = worker.ws.send_text
            blocked = False

            async def held_send(data):
                nonlocal blocked
                if not blocked:
                    blocked = True
                    send_started.set()
                    await asyncio.wait_for(release_send.wait(), 8)
                return await original_send(data)

            worker.ws.send_text = held_send

            async def send_close_command():
                return await realtime.send_v6_command(
                    worker, "close_session",
                    {"session_id": str(session_one_id),
                     "attempt_id": open_command["attempt_id"],
                     "control_generation": open_command["control_generation"]},
                    account_user_id=realtime.sessions[session_one_id].user_id,
                )

            delivery = client.portal.start_task_soon(send_close_command)
            client.portal.call(_wait, send_started)
            closing = client.portal.start_task_soon(
                asyncio.to_thread, fleet.__exit__, None, None, None
            )
            deadline = time.monotonic() + 3
            reassigned = False
            while time.monotonic() < deadline:
                if second_session.state != "live":
                    reassigned = True
                    break
                client.portal.call(asyncio.sleep, 0.02)
            assert reassigned, "live browser session was not reassigned while send was held"
        finally:
            client.portal.call(release_send.set)
            try:
                if delivery is not None:
                    try:
                        delivery.result(timeout=8)
                    except Exception:
                        pass
            finally:
                try:
                    if closing is not None:
                        closing.result(timeout=8)
                    else:
                        fleet.__exit__(None, None, None)
                finally:
                    try:
                        if browser_two is not None:
                            browser_two.__exit__(None, None, None)
                    finally:
                        browser.__exit__(None, None, None)


@pytest.mark.db
def test_reassignment_keeps_an_update_committed_during_disconnect(accounts, monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-finalizer-update-{uuid.uuid4()}"
    replacement_id = f"s1-finalizer-replacement-{uuid.uuid4()}"
    model_id = f"s1-finalizer-update-model-{uuid.uuid4()}"
    account_locked = asyncio.Event()
    release_account = asyncio.Event()
    update_at_gate = asyncio.Event()
    allow_update_claim = asyncio.Event()
    update_calling_gate = asyncio.Event()
    update_committed = asyncio.Event()
    desired_read_started = asyncio.Event()
    desired_read_captured = asyncio.Event()
    release_desired_read = asyncio.Event()
    pause_desired_read = asyncio.Event()
    second_account_locked = asyncio.Event()
    release_second_account = asyncio.Event()
    pause_placement_enabled = asyncio.Event()
    placement_started = asyncio.Event()
    release_placement = asyncio.Event()
    update_patch = {"prompt": "updated during disconnect"}

    with protocol6_client(worker_id) as client:
        _authenticate_browser(client)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        session_id = uuid.UUID(open_command["session_id"])
        updated_params = {**open_command["params"], **update_patch}
        replacement_context = client.websocket_connect("/api/v1/fleet")
        replacement_ws = replacement_context.__enter__()
        replacement_incarnation = uuid.uuid4()
        replacement_ws.send_json(hello(
            replacement_id, [manifest(model_id)], incarnation=replacement_incarnation,
        ))
        assert replacement_ws.receive_json()["type"] == "registered"
        replacement_grant = request_work_grant(
            replacement_ws, replacement_id, replacement_incarnation,
        )

        async def hold_account(acquired, release):
            async with db.session_factory() as session:
                async with session.begin():
                    await hold_the_account(
                        session, db.local_user_id, wait=True,
                    )
                    acquired.set()
                    await asyncio.wait_for(release.wait(), 20)

        original_hold = command_store.hold_the_account
        original_desired_read = command_subjects.read_committed_desired
        original_commit = command_store.commit_command
        original_place = realtime.place_session

        async def observe_update_hold(session, user_id, busy_detail="too many changes to this account at once", *, wait=False):
            if user_id == db.local_user_id and not update_at_gate.is_set():
                update_at_gate.set()
                await asyncio.wait_for(allow_update_claim.wait(), 8)
                update_calling_gate.set()
            await original_hold(session, user_id, busy_detail, wait=wait)

        async def observe_desired_read(*args, **kwargs):
            desired_read_started.set()
            result = await original_desired_read(*args, **kwargs)
            desired_read_captured.set()
            if pause_desired_read.is_set():
                await asyncio.wait_for(release_desired_read.wait(), 8)
            return result

        async def observe_commit(*args, **kwargs):
            command_type = kwargs.get("command_type", args[2] if len(args) > 2 else None)
            result = await original_commit(*args, **kwargs)
            if command_type == "update_session":
                update_committed.set()
            return result

        async def pause_placement(session, **kwargs):
            if session.id == session_id and pause_placement_enabled.is_set():
                placement_started.set()
                await asyncio.wait_for(release_placement.wait(), 8)
            return await original_place(session, **kwargs)

        monkeypatch.setattr(command_store, "hold_the_account", observe_update_hold)
        monkeypatch.setattr(command_subjects, "read_committed_desired", observe_desired_read)
        monkeypatch.setattr(command_store, "commit_command", observe_commit)
        monkeypatch.setattr(realtime, "place_session", pause_placement)
        holder = None
        second_holder = None
        closing = None
        try:
            holder = client.portal.start_task_soon(
                hold_account, account_locked, release_account,
            )
            client.portal.call(_wait, account_locked)
            browser_ws.send_json({"type": "update_params", "params": update_patch})
            client.portal.call(_wait, update_at_gate)

            closing = client.portal.start_task_soon(
                asyncio.to_thread, fleet.__exit__, None, None, None
            )

            async def wait_disconnect_progress():
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    preparation = getattr(worker, "delivery_prepared", None)
                    if desired_read_started.is_set() or (
                        worker_id not in realtime.workers
                        and preparation is not None
                        and not preparation.done()
                    ):
                        return
                    await asyncio.sleep(0.02)
                raise AssertionError("disconnect did not reach preparation or reassignment")

            client.portal.call(wait_disconnect_progress)
            if desired_read_started.is_set():
                client.portal.call(pause_placement_enabled.set)
                client.portal.call(pause_desired_read.set)
                client.portal.call(release_account.set)
                client.portal.call(_wait, desired_read_captured)
                second_holder = client.portal.start_task_soon(
                    hold_account, second_account_locked, release_second_account,
                )
                client.portal.call(_wait, second_account_locked)
                client.portal.call(release_desired_read.set)
                client.portal.call(_wait, placement_started)
                client.portal.call(allow_update_claim.set)
                client.portal.call(_wait, update_calling_gate)
                client.portal.call(release_second_account.set)
                client.portal.call(_wait, update_committed)
                client.portal.call(release_placement.set)
            else:
                client.portal.call(allow_update_claim.set)
                client.portal.call(_wait, update_calling_gate)
                client.portal.call(release_account.set)
                client.portal.call(_wait, update_committed)

            replacement_open = decode_control(replacement_ws.receive_text().encode("utf-8"))
            assert replacement_open["type"] == "open_session"
            assert replacement_open["params"] == updated_params
            replacement_ws.send_json(command_ack(replacement_open))
            replacement_ws.send_json(session_ready_message(
                realtime.workers[replacement_id], replacement_open,
                replacement_grant["grant_nonce"],
            ))

            async def read_desired_params():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT desired_params FROM realtime_sessions WHERE id = :id"),
                        {"id": session_id},
                    )

            assert client.portal.call(read_desired_params) == updated_params
        finally:
            client.portal.call(allow_update_claim.set)
            client.portal.call(release_account.set)
            client.portal.call(release_second_account.set)
            client.portal.call(release_desired_read.set)
            client.portal.call(release_placement.set)
            try:
                if holder is not None:
                    holder.result(timeout=8)
            finally:
                try:
                    if second_holder is not None:
                        second_holder.result(timeout=8)
                finally:
                    try:
                        if closing is not None:
                            closing.result(timeout=8)
                        else:
                            fleet.__exit__(None, None, None)
                    finally:
                        try:
                            replacement_context.__exit__(None, None, None)
                        finally:
                            browser.__exit__(None, None, None)
