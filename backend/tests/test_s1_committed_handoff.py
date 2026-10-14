"""Protocol 6 desired-state handoff across a committed update."""

import asyncio
import uuid
from concurrent.futures import CancelledError, TimeoutError as FutureTimeout
from functools import partial

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSessionTransaction

from app import db, realtime, worker_authority
from app.protocol6 import decode_control
from test_fleet_protocol6 import (
    drop_worker_rows,
    fresh_pool,
    hello,
    manifest,
    protocol6_client,
    request_work_grant,
    root_keys,
)
from test_v6_sessions import command_ack, open_live_session, session_ready_message


_UPDATE_MARKER = "s1_committed_update"
_WAIT_TIMEOUT = 10


class UpdateCloseGate:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_task: asyncio.Task | None = None
        self.committed_params: dict | None = None
        self.committed_revision: int | None = None


class UpdateCommitGate:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task | None = None


def hold_committed_update_close(monkeypatch, session_id: uuid.UUID,
                                generation: int, revision: int,
                                params: dict) -> UpdateCloseGate:
    factory = db.session_factory
    assert factory is not None
    session_class = factory.class_
    real_execute = session_class.execute
    real_close = session_class.close
    gate = UpdateCloseGate()

    async def track_execute(session, statement, *args, **kwargs):
        bound = args[0] if args else kwargs.get("params")
        if ("UPDATE realtime_sessions SET desired_params" in str(statement)
                and isinstance(bound, dict)
                and bound.get("session_id") == session_id
                and bound.get("revision") == revision):
            session.info[_UPDATE_MARKER] = (generation, revision, params)
        return await real_execute(session, statement, *args, **kwargs)

    async def close_after_commit(session, *args, **kwargs):
        marker = session.info.get(_UPDATE_MARKER)
        if marker is not None and gate.close_task is None:
            marked_generation, marked_revision, marked_params = marker
            assert not session.in_transaction()
            gate.close_task = asyncio.current_task()
            async with db.session_factory() as observer:
                row = (await observer.execute(
                    text(
                        "SELECT desired_params, desired_revision, control_generation "
                        "FROM realtime_sessions WHERE id = :session_id"
                    ),
                    {"session_id": session_id},
                )).mappings().one()
                command = await observer.scalar(
                    text(
                        "SELECT count(*) FROM worker_commands WHERE kind = 'update_session' "
                        "AND subject_id = :session_id AND subject_sequence = :generation"
                    ),
                    {"session_id": session_id, "generation": marked_generation},
                )
            assert row["desired_params"] == marked_params
            assert row["desired_revision"] == marked_revision
            assert row["control_generation"] == marked_generation
            assert command == 1
            gate.committed_params = row["desired_params"]
            gate.committed_revision = row["desired_revision"]
            gate.started.set()
            await gate.release.wait()
        return await real_close(session, *args, **kwargs)

    monkeypatch.setattr(session_class, "execute", track_execute)
    monkeypatch.setattr(session_class, "close", close_after_commit)
    return gate


def hold_update_before_commit(monkeypatch, gate: UpdateCommitGate) -> None:
    real_exit = AsyncSessionTransaction.__aexit__

    async def hold_outer_update_exit(transaction, error_type, error, traceback):
        marker = transaction.session.info.get(_UPDATE_MARKER)
        if (marker is not None and not transaction.nested and error_type is None
                and error is None and traceback is None and gate.task is None):
            assert transaction.session.in_transaction()
            gate.task = asyncio.current_task()
            gate.started.set()
            await gate.release.wait()
        return await real_exit(transaction, error_type, error, traceback)

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", hold_outer_update_exit)


async def wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=_WAIT_TIMEOUT)


async def wait_for_task(task: asyncio.Task) -> None:
    done, pending = await asyncio.wait({task}, timeout=_WAIT_TIMEOUT)
    assert not pending, "owned task did not finish after its gate was released"
    if task in done and not task.cancelled():
        task.result()


def add_protocol6_worker(client, worker_id: str, model_id: str):
    incarnation = uuid.uuid4()
    client.portal.call(worker_authority.acquire_scheduler_lease)
    fleet_context = client.websocket_connect("/api/v1/fleet")
    fleet = fleet_context.__enter__()
    fleet.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
    assert fleet.receive_json()["type"] == "registered"
    grant = request_work_grant(fleet, worker_id, incarnation)
    return fleet, fleet_context, realtime.workers[worker_id], grant


def receive_type(browser, kind: str) -> dict:
    for _ in range(8):
        message = browser.receive_json()
        if message.get("type") == kind:
            return message
    raise AssertionError(f"browser did not receive {kind}")


async def read_handoff_rows(session_id: uuid.UUID) -> tuple[dict, dict | None]:
    async with db.session_factory() as session:
        current = (await session.execute(
            text(
                "SELECT desired_params, desired_revision, control_generation "
                "FROM realtime_sessions WHERE id = :session_id"
            ),
            {"session_id": session_id},
        )).mappings().one()
        attempt = (await session.execute(
            text(
                "SELECT worker_id, attempt_id, control_generation FROM realtime_session_attempts "
                "WHERE session_id = :session_id ORDER BY control_generation DESC LIMIT 1"
            ),
            {"session_id": session_id},
        )).mappings().one_or_none()
        return current, attempt


def replacement_open(fleet, worker, grant, browser, session_id: uuid.UUID) -> dict:
    opened = decode_control(fleet.receive_text().encode("utf-8"))
    assert opened["type"] == "open_session"
    assert opened["session_id"] == str(session_id)
    fleet.send_json(command_ack(opened))
    fleet.send_json(session_ready_message(worker, opened, grant["grant_nonce"]))
    assert browser.receive_json()["type"] == "interrupted"
    assert browser.receive_json()["type"] == "resumed"
    return opened


def direct_update(client, session: realtime.Session, params: dict, caller_tasks: list,
                  *, caller_name=None):
    worker = session.worker
    assert worker is not None
    assert session.attempt_id is not None
    revision = session.desired_revision + 1
    fields = {
        "session_id": str(session.id),
        "attempt_id": str(session.attempt_id),
        "control_generation": session.control_generation,
        "model_id": session.model_id,
        "params": params,
        "params_revision": revision,
    }
    send = partial(
        realtime.send_v6_command,
        worker,
        "update_session",
        fields,
        account_user_id=session.user_id,
        session_context={
            "auth_session_id": session.auth_session_id,
            "browser_owner_id": worker_authority.TRANSPORT_OWNER_ID,
            "attempt_id": session.attempt_id,
            "control_generation": session.control_generation,
            "model_id": session.model_id,
            "base_desired_revision": session.desired_revision,
            "desired_revision": revision,
            "params": params,
        },
    )

    async def caller():
        task = asyncio.current_task()
        assert task is not None
        caller_tasks.append(task)
        return await send()

    return client.portal.start_task_soon(caller, name=caller_name)


def start_reassign(client, session: realtime.Session, tasks: list):
    async def run():
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        return await realtime.reassign(session)

    return client.portal.start_task_soon(run, name="s1-reassign")


def finish_test(client, gate, futures, portal_tasks, contexts, worker_id: str | None,
                release_events) -> BaseException | None:
    cleanup_error = None

    def attempt(action) -> None:
        nonlocal cleanup_error
        try:
            action()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

    def join_future(future) -> None:
        try:
            future.result(timeout=_WAIT_TIMEOUT)
        except CancelledError:
            return
        except FutureTimeout:
            raise AssertionError("owned caller did not finish after its task was joined")

    if gate is not None:
        attempt(lambda: client.portal.call(gate.release.set))
    for event in release_events:
        if event is not None:
            attempt(lambda event=event: client.portal.call(event.set))
    if gate is not None:
        if gate.close_task is not None:
            attempt(lambda: client.portal.call(wait_for_task, gate.close_task))
    for task in portal_tasks:
        if task is not None:
            attempt(lambda task=task: client.portal.call(wait_for_task, task))
    if gate is not None and gate.close_task is not None:
        attempt(lambda: client.portal.call(wait_for_task, gate.close_task))
    for future in futures:
        if future is not None:
            attempt(lambda future=future: join_future(future))
    for context in contexts:
        if context is not None:
            attempt(lambda context=context: context.__exit__(None, None, None))
    if worker_id is not None:
        attempt(lambda: client.portal.call(fresh_pool))
        attempt(lambda: client.portal.call(drop_worker_rows, worker_id))
    return cleanup_error


@pytest.mark.db
def test_committed_update_handoff_uses_the_database_snapshot(monkeypatch):
    root_keys(monkeypatch)
    first_worker_id = f"s1-committed-{uuid.uuid4()}"
    second_worker_id = f"s1-replacement-{uuid.uuid4()}"
    model_id = f"s1-model-{uuid.uuid4()}"
    gate = None
    update_future = None
    delivery_owner = None
    reassign_future = None
    caller_tasks = []
    reassign_tasks = []
    primary = None

    with protocol6_client(first_worker_id) as client:
        browser_context = fleet_context = second_fleet_context = None
        try:
            _first_fleet, first_worker, _grant, open_cmd, browser, browser_context, fleet_context = (
                open_live_session(client, first_worker_id, model_id)
            )
            second_fleet, second_fleet_context, second_worker, second_grant = add_protocol6_worker(
                client, second_worker_id, model_id
            )
            session_id = uuid.UUID(open_cmd["session_id"])
            session = realtime.sessions[session_id]
            committed_params = {**session.desired_params, "prompt": "committed"}
            committed_revision = 2
            gate = hold_committed_update_close(
                monkeypatch, session_id, session.control_generation,
                committed_revision, committed_params,
            )
            caller_started = asyncio.Event()
            caller_task = {}
            real_delivery = realtime._deliver_pending_worker_command

            async def observe_update_caller(worker, *, prepare=None):
                task = asyncio.current_task()
                if task is not None and task.get_name() == "s1-outer-update-caller":
                    caller_task["task"] = task
                    caller_started.set()
                return await real_delivery(worker, prepare=prepare)

            monkeypatch.setattr(
                realtime, "_deliver_pending_worker_command", observe_update_caller
            )
            browser.send_json({"type": "update_params", "params": {"prompt": "committed"}})
            client.portal.call(wait_for_event, gate.started)
            update_future = direct_update(
                client, session, committed_params, caller_tasks,
                caller_name="s1-outer-update-caller",
            )
            client.portal.call(wait_for_event, caller_started)
            assert caller_tasks[0] is caller_task["task"]
            delivery_owner = first_worker.delivery_task
            assert delivery_owner is not None
            assert gate.close_task is not None
            assert not gate.close_task.done()
            assert not delivery_owner.done()
            assert gate.committed_params == committed_params
            assert gate.committed_revision == committed_revision
            before_rows, before_attempt = client.portal.call(read_handoff_rows, session_id)
            assert before_rows["desired_params"] == committed_params
            assert before_rows["desired_revision"] == committed_revision
            assert before_rows["control_generation"] == session.control_generation
            assert before_attempt["worker_id"] == first_worker_id

            client.portal.call(caller_task["task"].cancel)
            with pytest.raises(CancelledError):
                update_future.result(timeout=_WAIT_TIMEOUT)
            update_future = None
            client.portal.call(wait_for_task, caller_tasks[0])
            assert client.portal.call(lambda: not gate.close_task.done())
            assert client.portal.call(lambda: not delivery_owner.done())
            assert client.portal.call(gate.release.is_set) is False

            original_generation = session.control_generation
            original_attempt = session.attempt_id
            reassign_future = start_reassign(client, session, reassign_tasks)
            opened = replacement_open(
                second_fleet, second_worker, second_grant, browser, session_id
            )
            assert opened["params"] == committed_params, opened
            assert session.control_generation == original_generation + 1
            assert session.worker is second_worker
            assert session.attempt_id != original_attempt

            replacement_state = (
                session.desired_params,
                session.desired_revision,
                session.worker,
                session.control_generation,
                session.attempt_id,
            )
            after_rows, after_attempt = client.portal.call(read_handoff_rows, session_id)
            assert after_rows["desired_params"] == committed_params
            assert after_rows["desired_revision"] == replacement_state[1] == 1
            assert after_attempt["worker_id"] == second_worker_id
            assert after_attempt["attempt_id"] == session.attempt_id

            assert client.portal.call(lambda: not gate.close_task.done())
            assert client.portal.call(lambda: not delivery_owner.done())

            client.portal.call(gate.release.set)
            client.portal.call(wait_for_task, gate.close_task)
            client.portal.call(wait_for_task, delivery_owner)
            reassign_future.result(timeout=_WAIT_TIMEOUT)
            assert session.desired_params is replacement_state[0]
            assert (
                session.desired_revision,
                session.worker,
                session.control_generation,
                session.attempt_id,
            ) == replacement_state[1:]
            final_rows, final_attempt = client.portal.call(read_handoff_rows, session_id)
            assert final_rows["desired_params"] == committed_params
            assert final_rows["desired_revision"] == replacement_state[1]
            assert final_rows["control_generation"] == replacement_state[3]
            assert final_attempt["worker_id"] == second_worker_id
            assert final_attempt["attempt_id"] == replacement_state[4]

            browser.send_json({"type": "update_params", "params": {"prompt": "next"}})
            next_update = decode_control(second_fleet.receive_text().encode("utf-8"))
            assert next_update["type"] == "update_session"
            assert next_update["params_revision"] == replacement_state[1] + 1
            second_fleet.send_json(command_ack(next_update))
            response = receive_type(browser, "params_updated")
            assert response["params"]["prompt"] == "next"
        except BaseException as error:
            primary = (error, error.__traceback__)
        finally:
            cleanup_error = finish_test(
                client,
                gate,
                [update_future, reassign_future],
                [*caller_tasks, delivery_owner, *reassign_tasks],
                [browser_context, second_fleet_context, fleet_context],
                second_worker_id if second_fleet_context is not None else None,
                [],
            )
        if primary is not None:
            if cleanup_error is not None:
                raise primary[0].with_traceback(primary[1]) from cleanup_error
            raise primary[0].with_traceback(primary[1])
        if cleanup_error is not None:
            raise cleanup_error


@pytest.mark.db
def test_workerless_update_wins_over_a_delayed_committed_snapshot(monkeypatch):
    root_keys(monkeypatch)
    first_worker_id = f"s1-workerless-{uuid.uuid4()}"
    second_worker_id = f"s1-workerless-replacement-{uuid.uuid4()}"
    model_id = f"s1-workerless-model-{uuid.uuid4()}"
    gate = None
    old_update = reassign_future = None
    release_detach = None
    old_update_tasks = []
    reassign_tasks = []
    primary = None

    with protocol6_client(first_worker_id) as client:
        browser_context = fleet_context = second_fleet_context = None
        try:
            _first_fleet, first_worker, _grant, open_cmd, browser, browser_context, fleet_context = (
                open_live_session(client, first_worker_id, model_id)
            )
            second_fleet, second_fleet_context, second_worker, second_grant = add_protocol6_worker(
                client, second_worker_id, model_id
            )
            session_id = uuid.UUID(open_cmd["session_id"])
            session = realtime.sessions[session_id]
            assert session.worker is first_worker
            original_generation = session.control_generation
            original_attempt = session.attempt_id
            committed_params = {**session.desired_params, "prompt": "older commit"}
            gate = hold_committed_update_close(
                monkeypatch, session_id, session.control_generation, 2, committed_params
            )
            old_update = direct_update(client, session, committed_params, old_update_tasks)
            client.portal.call(wait_for_event, gate.started)
            committed_rows, _ = client.portal.call(read_handoff_rows, session_id)
            assert committed_rows["desired_params"] == committed_params
            assert committed_rows["desired_revision"] == 2

            detach_started = asyncio.Event()
            release_detach = asyncio.Event()
            real_abandon = worker_authority.abandon_realtime_attempt

            async def hold_abandon(session_id_arg, generation, attempt_id):
                if session_id_arg == session.id:
                    assert session.worker is None
                    assert generation == original_generation
                    assert attempt_id == original_attempt
                    assert session.control_generation == original_generation
                    assert session.attempt_id == original_attempt
                    detach_started.set()
                    await release_detach.wait()
                return await real_abandon(session_id_arg, generation, attempt_id)

            monkeypatch.setattr(worker_authority, "abandon_realtime_attempt", hold_abandon)
            reassign_future = start_reassign(client, session, reassign_tasks)
            client.portal.call(wait_for_event, detach_started)
            assert session.worker is None
            browser.send_json({"type": "update_params", "params": {"prompt": "newer"}})
            response = receive_type(browser, "params_updated")
            assert response["params"]["prompt"] == "newer"
            newer_object = session.desired_params
            newer_revision = session.desired_revision
            assert newer_object is not committed_params
            assert newer_object["prompt"] == "newer"

            client.portal.call(gate.release.set)
            client.portal.call(wait_for_task, gate.close_task)
            old_update.result(timeout=_WAIT_TIMEOUT)
            client.portal.call(wait_for_task, old_update_tasks[0])
            assert session.worker is None
            assert session.control_generation == original_generation
            assert session.attempt_id == original_attempt
            assert session.desired_params is newer_object
            assert session.desired_revision == newer_revision

            client.portal.call(release_detach.set)
            opened = replacement_open(
                second_fleet, second_worker, second_grant, browser, session_id
            )
            assert opened["params"] == newer_object
            assert session.desired_params is newer_object
            assert session.desired_revision == 1
            replacement_state = (
                session.desired_params,
                session.desired_revision,
                session.worker,
                session.control_generation,
                session.attempt_id,
            )
            after_rows, after_attempt = client.portal.call(read_handoff_rows, session_id)
            assert after_rows["desired_params"] == newer_object
            assert after_rows["desired_revision"] == replacement_state[1] == 1
            assert after_attempt["worker_id"] == second_worker_id
            assert after_attempt["attempt_id"] == session.attempt_id

            reassign_future.result(timeout=_WAIT_TIMEOUT)
            assert session.desired_params is replacement_state[0]
            assert (
                session.desired_revision,
                session.worker,
                session.control_generation,
                session.attempt_id,
            ) == replacement_state[1:]
            final_rows, final_attempt = client.portal.call(read_handoff_rows, session_id)
            assert final_rows["desired_params"] == newer_object
            assert final_rows["desired_revision"] == after_rows["desired_revision"]
            assert final_attempt["worker_id"] == second_worker_id
        except BaseException as error:
            primary = (error, error.__traceback__)
        finally:
            cleanup_error = finish_test(
                client,
                gate,
                [old_update, reassign_future],
                [*old_update_tasks, *reassign_tasks],
                [browser_context, second_fleet_context, fleet_context],
                second_worker_id if second_fleet_context is not None else None,
                [release_detach],
            )
        if primary is not None:
            if cleanup_error is not None:
                raise primary[0].with_traceback(primary[1]) from cleanup_error
            raise primary[0].with_traceback(primary[1])
        if cleanup_error is not None:
            raise cleanup_error


@pytest.mark.db
def test_workerless_update_survives_an_old_committed_update(monkeypatch):
    root_keys(monkeypatch)
    first_worker_id = f"s1-identity-{uuid.uuid4()}"
    second_worker_id = f"s1-identity-replacement-{uuid.uuid4()}"
    model_id = f"s1-identity-model-{uuid.uuid4()}"
    close_gate = commit_gate = None
    old_update = reassign_future = None
    release_detach = None
    old_update_tasks = []
    reassign_tasks = []
    owned_tasks = []
    primary = None

    with protocol6_client(first_worker_id) as client:
        browser_context = fleet_context = second_fleet_context = None
        try:
            _first_fleet, first_worker, _grant, open_cmd, browser, browser_context, fleet_context = (
                open_live_session(client, first_worker_id, model_id)
            )
            second_fleet, second_fleet_context, second_worker, second_grant = add_protocol6_worker(
                client, second_worker_id, model_id
            )
            session_id = uuid.UUID(open_cmd["session_id"])
            session = realtime.sessions[session_id]
            original_object = session.desired_params
            original_generation = session.control_generation
            original_attempt = session.attempt_id
            old_params = {**original_object, "prompt": "old commit"}
            close_gate = hold_committed_update_close(
                monkeypatch, session_id, original_generation, 2, old_params
            )
            commit_gate = UpdateCommitGate()
            hold_update_before_commit(monkeypatch, commit_gate)

            old_update = direct_update(client, session, old_params, old_update_tasks)
            client.portal.call(wait_for_event, commit_gate.started)
            assert session.desired_params is original_object
            transaction_task = commit_gate.task
            assert transaction_task is not None
            delivery_owner = first_worker.delivery_task
            assert delivery_owner is not None
            owned_tasks.extend((transaction_task, delivery_owner))

            detach_started = asyncio.Event()
            release_detach = asyncio.Event()
            real_abandon = worker_authority.abandon_realtime_attempt

            async def hold_abandon(session_id_arg, generation, attempt_id):
                if session_id_arg == session.id:
                    assert session.worker is None
                    assert generation == original_generation
                    assert attempt_id == original_attempt
                    assert session.control_generation == original_generation
                    assert session.attempt_id == original_attempt
                    detach_started.set()
                    await release_detach.wait()
                return await real_abandon(session_id_arg, generation, attempt_id)

            monkeypatch.setattr(worker_authority, "abandon_realtime_attempt", hold_abandon)
            reassign_future = start_reassign(client, session, reassign_tasks)
            client.portal.call(wait_for_event, detach_started)
            assert session.worker is None

            browser.send_json({"type": "update_params", "params": {"prompt": "newer"}})
            response = receive_type(browser, "params_updated")
            assert response["params"]["prompt"] == "newer"
            newer_object = session.desired_params
            newer_revision = session.desired_revision
            assert newer_object is not original_object
            assert newer_object["prompt"] == "newer"
            assert session.control_generation == original_generation
            assert session.attempt_id == original_attempt

            client.portal.call(commit_gate.release.set)
            client.portal.call(wait_for_event, close_gate.started)
            assert close_gate.committed_params == old_params
            assert close_gate.committed_revision == 2
            assert close_gate.close_task is not None
            assert not close_gate.close_task.done()
            assert not delivery_owner.done()
            assert not transaction_task.done()
            assert session.desired_params is newer_object
            assert session.desired_revision == newer_revision
            assert session.control_generation == original_generation
            assert session.attempt_id == original_attempt
            assert session.worker is None

            client.portal.call(close_gate.release.set)
            client.portal.call(wait_for_task, close_gate.close_task)
            client.portal.call(wait_for_task, delivery_owner)
            client.portal.call(wait_for_task, transaction_task)
            old_update.result(timeout=_WAIT_TIMEOUT)
            client.portal.call(wait_for_task, old_update_tasks[0])
            assert session.desired_params is newer_object
            assert session.desired_revision == newer_revision
            assert session.control_generation == original_generation
            assert session.attempt_id == original_attempt
            assert session.worker is None

            client.portal.call(release_detach.set)
            opened = replacement_open(
                second_fleet, second_worker, second_grant, browser, session_id
            )
            assert opened["params"] == newer_object
            assert session.desired_params is newer_object
            assert session.desired_revision == 1
            assert session.worker is second_worker
            assert session.control_generation == original_generation + 1
            assert session.attempt_id != original_attempt
            replacement_attempt = session.attempt_id
            rows, attempt = client.portal.call(read_handoff_rows, session_id)
            assert rows["desired_params"] == newer_object
            assert rows["desired_revision"] == 1
            assert rows["control_generation"] == original_generation + 1
            assert attempt["worker_id"] == second_worker_id
            assert attempt["attempt_id"] == replacement_attempt
            assert attempt["control_generation"] == original_generation + 1

            reassign_future.result(timeout=_WAIT_TIMEOUT)
            client.portal.call(wait_for_task, reassign_tasks[0])
            browser.send_json({"type": "update_params", "params": {"prompt": "current"}})
            current_update = decode_control(second_fleet.receive_text().encode("utf-8"))
            assert current_update["type"] == "update_session"
            assert current_update["params_revision"] == 2
            second_fleet.send_json(command_ack(current_update))
            response = receive_type(browser, "params_updated")
            assert response["params"]["prompt"] == "current"
            rows, attempt = client.portal.call(read_handoff_rows, session_id)
            assert rows["desired_params"]["prompt"] == "current"
            assert rows["desired_revision"] == current_update["params_revision"] == 2
            assert rows["control_generation"] == original_generation + 1
            assert attempt["worker_id"] == second_worker_id
            assert attempt["attempt_id"] == replacement_attempt
        except BaseException as error:
            primary = (error, error.__traceback__)
        finally:
            cleanup_error = finish_test(
                client,
                close_gate,
                [old_update, reassign_future],
                [*old_update_tasks, *owned_tasks, *reassign_tasks],
                [browser_context, second_fleet_context, fleet_context],
                second_worker_id if second_fleet_context is not None else None,
                [commit_gate.release if commit_gate is not None else None, release_detach],
            )
        if primary is not None:
            if cleanup_error is not None:
                raise primary[0].with_traceback(primary[1]) from cleanup_error
            raise primary[0].with_traceback(primary[1])
        if cleanup_error is not None:
            raise cleanup_error
