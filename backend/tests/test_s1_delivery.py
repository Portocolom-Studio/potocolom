import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import command_store, db, realtime


@pytest.fixture
def anyio_backend():
    return "asyncio"


class HeldSocket:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.messages = []

    async def send_text(self, message):
        self.messages.append(message)
        self.started.set()
        await self.release.wait()

    async def close(self, *args, **kwargs):
        self.close_started.set()
        await self.close_release.wait()


def _worker(worker_id):
    return realtime.Worker(
        worker_id,
        HeldSocket(),
        [],
        1,
        protocol_version=6,
        incarnation=uuid.uuid4(),
        transport_owner_id=uuid.uuid4(),
    )


def _command():
    body = json.dumps({"type": "open_session", "session_id": str(uuid.uuid4())}).encode()
    return SimpleNamespace(sequence=1, body=body)


@pytest.mark.anyio
async def test_delivery_waits_for_known_release_before_socket_io(monkeypatch):
    worker = _worker(f"s1-delivery-release-{uuid.uuid4()}")
    command = _command()
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(db, "session_factory", object())
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setitem(realtime.workers, worker.id, worker)

    async def read_exact_pending(*args, **kwargs):
        return command

    async def register_delivery(*args, register, **kwargs):
        assert register()
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)
    monkeypatch.setattr(command_store, "register_delivery", register_delivery)
    caller = asyncio.create_task(realtime._deliver_pending_worker_command(worker))
    await entered.wait()
    assert worker.ws.messages == []
    release.set()
    await worker.ws.started.wait()
    worker.ws.release.set()
    await caller


@pytest.mark.anyio
async def test_unknown_release_never_activates_registered_delivery(monkeypatch):
    worker = _worker(f"s1-delivery-unknown-{uuid.uuid4()}")
    command = _command()
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(db, "session_factory", object())
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setitem(realtime.workers, worker.id, worker)

    async def read_exact_pending(*args, **kwargs):
        return command

    async def register_delivery(*args, register, **kwargs):
        assert register()
        entered.set()
        await release.wait()
        raise command_store.CommandRefused("session release is uncertain")

    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)
    monkeypatch.setattr(command_store, "register_delivery", register_delivery)
    owner = asyncio.create_task(realtime._deliver_pending_worker_command(worker))
    await entered.wait()
    assert worker.ws.messages == []
    release.set()
    await owner
    assert worker.ws.messages == []
    assert worker.delivery_send_task is None


@pytest.mark.anyio
async def test_repeated_outer_cancel_and_shutdown_join_held_send_after_worker_removal(
    monkeypatch,
):
    worker = _worker(f"s1-delivery-held-{uuid.uuid4()}")
    command = _command()
    monkeypatch.setattr(db, "session_factory", object())
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setattr(realtime, "CLOSE_TIMEOUT", 0.01)
    monkeypatch.setitem(realtime.workers, worker.id, worker)

    async def read_exact_pending(*args, **kwargs):
        return command

    async def register_delivery(*args, register, **kwargs):
        return register()

    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)
    monkeypatch.setattr(command_store, "register_delivery", register_delivery)
    caller = asyncio.create_task(realtime._deliver_pending_worker_command(worker))
    await worker.ws.started.wait()
    for _ in range(100):
        if worker.id not in realtime.workers:
            break
        await asyncio.sleep(0.01)
    assert worker.id not in realtime.workers
    caller.cancel()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    owner = worker.delivery_task
    assert owner is not None and not owner.done()
    assert worker.delivery_send_task is not None
    drain = asyncio.create_task(realtime.drain_worker_deliveries())
    await asyncio.sleep(0)
    assert not drain.done()
    assert worker.delivery_task is owner
    worker.ws.release.set()
    await drain
    assert owner.done()
    assert worker.delivery_task is None
    assert len(worker.ws.messages) == 1


@pytest.mark.anyio
async def test_shutdown_closes_idle_worker_delivery_admission(monkeypatch):
    worker = _worker(f"s1-delivery-idle-{uuid.uuid4()}")
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setitem(realtime.workers, worker.id, worker)
    await realtime.drain_worker_deliveries()
    assert worker.delivery_admission_open is False
    assert worker.delivery_task is None
    assert realtime._worker_delivery_admission_open is False


@pytest.mark.anyio
async def test_delivery_owner_holds_actual_session_close_and_serializes_builders(monkeypatch):
    worker = _worker(f"s1-delivery-prep-{uuid.uuid4()}")
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    second_started = asyncio.Event()
    original_close = db.OwnedSession.close
    monkeypatch.setattr(
        db, "session_factory", async_sessionmaker(class_=db.OwnedSession)
    )
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setitem(realtime.workers, worker.id, worker)

    async def read_exact_pending(*args, **kwargs):
        return None

    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)

    async def held_close(session):
        if session.info.pop("hold_delivery_close", False):
            close_started.set()
            await close_release.wait()
        await original_close(session)

    monkeypatch.setattr(db.OwnedSession, "close", held_close)

    async def first_builder():
        async with db.session_factory() as session:
            session.info["hold_delivery_close"] = True
        return "first"

    async def second_builder():
        async with db.session_factory() as session:
            assert session is not None
        second_started.set()
        return "second"

    first = asyncio.create_task(
        realtime._deliver_pending_worker_command(worker, prepare=first_builder)
    )
    await close_started.wait()
    owner = worker.delivery_task
    assert owner is not None and owner in realtime._worker_delivery_owners
    second = asyncio.create_task(
        realtime._deliver_pending_worker_command(worker, prepare=second_builder)
    )
    await asyncio.sleep(0)
    assert not second_started.is_set()
    assert not second.done()
    close_release.set()
    assert await first == "first"
    assert await second == "second"
    assert second_started.is_set()
    assert worker.delivery_task is None


@pytest.mark.anyio
@pytest.mark.parametrize("fail_after_close", [False, True])
async def test_shutdown_joins_actual_prep_close_after_cancel_and_worker_removal(
        monkeypatch, fail_after_close):
    worker = _worker(f"s1-delivery-prep-shutdown-{uuid.uuid4()}")
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    opposing_started = asyncio.Event()
    close_error = RuntimeError("session close failed after releasing its connection")
    original_close = db.OwnedSession.close
    monkeypatch.setattr(
        db, "session_factory", async_sessionmaker(class_=db.OwnedSession)
    )
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setitem(realtime.workers, worker.id, worker)

    async def read_exact_pending(*args, **kwargs):
        return None

    async def held_close(session):
        if session.info.pop("hold_delivery_close", False):
            close_started.set()
            await close_release.wait()
        await original_close(session)
        if fail_after_close:
            raise close_error

    monkeypatch.setattr(db.OwnedSession, "close", held_close)
    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)

    async def prepare():
        async with db.session_factory() as session:
            session.info["hold_delivery_close"] = True
        return "committed"

    async def opposing_prepare():
        opposing_started.set()
        return "opposing"

    caller = asyncio.create_task(
        realtime._deliver_pending_worker_command(worker, prepare=prepare)
    )
    await close_started.wait()
    owner = worker.delivery_task
    assert owner is not None and owner in realtime._worker_delivery_owners
    opposing = asyncio.create_task(
        realtime._deliver_pending_worker_command(worker, prepare=opposing_prepare)
    )
    await asyncio.sleep(0)
    assert not opposing_started.is_set()

    realtime.workers.pop(worker.id)
    realtime.stop_worker_deliveries()
    caller.cancel()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    drain = asyncio.create_task(realtime.drain_worker_deliveries())
    await asyncio.sleep(0)
    assert not drain.done()
    assert not owner.done()
    assert not opposing.done()
    close_release.set()
    if fail_after_close:
        with pytest.raises(RuntimeError, match="session close failed") as raised:
            await drain
        assert raised.value is close_error
    else:
        await drain
    assert not opposing_started.is_set()
    assert await opposing is None
    assert owner.done()
    assert worker.delivery_task is None
    assert owner not in realtime._worker_delivery_owners


@pytest.mark.anyio
async def test_fenced_close_is_tracked_concurrently_and_joined_after_repeated_cancel(
    monkeypatch,
):
    worker = _worker(f"s1-delivery-close-{uuid.uuid4()}")
    command = _command()
    monkeypatch.setattr(db, "session_factory", object())
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)
    monkeypatch.setattr(realtime, "CLOSE_TIMEOUT", 100)
    monkeypatch.setitem(realtime.workers, worker.id, worker)
    monkeypatch.setattr("app.jobs.on_worker_lost", lambda _worker: None)

    async def read_exact_pending(*args, **kwargs):
        return command

    async def register_delivery(*args, register, **kwargs):
        return register()

    monkeypatch.setattr(command_store, "read_exact_pending", read_exact_pending)
    monkeypatch.setattr(command_store, "register_delivery", register_delivery)
    delivery = asyncio.create_task(realtime._deliver_pending_worker_command(worker))
    await worker.ws.started.wait()
    closing = asyncio.create_task(
        realtime.close_fenced_worker(worker.id, worker.incarnation)
    )
    await worker.ws.close_started.wait()
    assert worker.delivery_send_task is not None
    close_task = worker.delivery_close_task
    assert close_task is not None and close_task in realtime._worker_delivery_owners
    assert not worker.delivery_send_task.done()

    closing.cancel()
    closing.cancel()
    drain = asyncio.create_task(realtime.drain_worker_deliveries())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not drain.done()
    worker.ws.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not drain.done()
    worker.ws.release.set()
    await drain
    await delivery
    assert close_task.done()
    assert close_task not in realtime._worker_delivery_owners
