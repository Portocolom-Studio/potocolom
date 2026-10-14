"""Application shutdown joins tracked command delivery before disposing dependencies."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import app.main as main_module
from app.main import app
from app.settings import Settings


def test_lifespan_joins_delivery_before_disposal_through_repeated_cancel(monkeypatch):
    from app import command_store, db, realtime
    from test_s1_delivery import _worker

    disposed = []
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(
        auth_mode="none", fleet_token_key="test-fleet-token", telemetry=False,
    ))
    monkeypatch.setattr(db, "session_factory", object())
    monkeypatch.setattr(realtime, "_worker_delivery_admission_open", True)

    async def unavailable():
        return False

    async def dispose():
        disposed.append(True)

    async def drain_blobs():
        pass

    async def idle():
        await asyncio.Future()

    async def read(*args, **kwargs):
        return SimpleNamespace(sequence=1, body=b'{"type":"open_session"}')

    async def register(*args, register, **kwargs):
        return register()

    monkeypatch.setattr(main_module.db, "connect", unavailable)
    monkeypatch.setattr(main_module.db, "dispose", dispose)
    monkeypatch.setattr(main_module.jobs, "drain_blob_cleanup", drain_blobs)
    for name in (
        "reap_dead_workers", "sweep_dead_sessions", "maintain_loop",
        "maintain_deletes_loop", "telemetry_loop", "mail_loop", "purge_loop",
        "maintain_scheduler_lease",
    ):
        monkeypatch.setattr(main_module, name, idle)
    monkeypatch.setattr(main_module.jobs, "dispatch_loop", idle)
    monkeypatch.setattr(command_store, "read_exact_pending", read)
    monkeypatch.setattr(command_store, "register_delivery", register)

    async def exercise():
        worker = _worker(f"main-held-{uuid.uuid4()}")
        monkeypatch.setitem(realtime.workers, worker.id, worker)
        lifespan = main_module.lifespan(app)
        await lifespan.__aenter__()
        caller = asyncio.create_task(realtime._deliver_pending_worker_command(worker))
        await asyncio.wait_for(worker.ws.started.wait(), 1)
        owner = worker.delivery_task
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        for _ in range(100):
            if not realtime._worker_delivery_admission_open:
                break
            await asyncio.sleep(0)
        assert not realtime._worker_delivery_admission_open
        assert not worker.delivery_admission_open
        for _ in range(2):
            shutdown.cancel()
            await asyncio.sleep(0)
        assert not shutdown.done()
        assert disposed == []
        assert worker.delivery_task is owner
        assert worker.delivery_send_task is not None
        worker.ws.release.set()
        await asyncio.wait_for(caller, 1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 1)
        assert worker.delivery_task is None
        assert disposed == [True]

        async with main_module.lifespan(app):
            assert realtime._worker_delivery_admission_open
            assert not worker.delivery_admission_open
            await realtime._deliver_pending_worker_command(worker)
            assert len(worker.ws.messages) == 1
            fresh = _worker(f"main-fresh-{uuid.uuid4()}")
            monkeypatch.setitem(realtime.workers, fresh.id, fresh)
            fresh.ws.release.set()
            await asyncio.wait_for(realtime._deliver_pending_worker_command(fresh), 1)
            assert len(fresh.ws.messages) == 1
        assert disposed == [True, True]

    asyncio.run(exercise())
