import asyncio
import json
import sys
import time
import uuid
from threading import Event, Thread

import pytest
from sqlalchemy import text

from app import command_store, db, jobs, realtime, sessions as account_sessions, worker_authority
from app import main as main_module
from app.protocol6 import decode_control
from app.settings import get_settings
from app.tables import User
from test_fleet_protocol6 import protocol6_client, root_keys, seed_queued_job
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
    shutdown_drain_started = Event()
    events = []
    original_close = worker_authority.close_worker
    original_dispose = db.dispose
    original_drain = main_module.drain_worker_deliveries

    async def held_close(target_id, incarnation):
        close_started.set()
        await asyncio.to_thread(release_close.wait, 8)
        result = await original_close(target_id, incarnation)
        events.append("close-returned")
        return result

    async def observed_dispose():
        events.append("dispose")
        return await original_dispose()

    async def observed_drain():
        shutdown_drain_started.set()
        return await original_drain()

    monkeypatch.setattr(worker_authority, "close_worker", held_close)
    monkeypatch.setattr(db, "dispose", observed_dispose)
    monkeypatch.setattr(main_module, "drain_worker_deliveries", observed_drain)
    with protocol6_client(worker_id) as client:
        _authenticate_browser(client)
        worker_ws, worker, _grant, _open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        closing = client.portal.start_task_soon(
            asyncio.to_thread, fleet.__exit__, None, None, None
        )
        releaser = Thread(target=lambda: (
            shutdown_drain_started.wait(8), release_close.set()
        ))
        try:
            client.portal.call(_wait, close_started)
            releaser.start()
            browser.__exit__(None, None, None)
        finally:
            if releaser.ident is None:
                release_close.set()
                shutdown_drain_started.set()
            if sys.exc_info()[0] is not None:
                release_close.set()
                shutdown_drain_started.set()
                if releaser.ident is not None:
                    releaser.join(8)
                if closing is not None:
                    closing.result(timeout=8)
    closing.result(timeout=8)
    if releaser.ident is not None:
        releaser.join(8)
    assert events.index("close-returned") < events.index("dispose")
    assert not releaser.is_alive()


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
