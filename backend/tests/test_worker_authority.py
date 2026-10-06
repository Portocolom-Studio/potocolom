"""Direct calls into the durable authority: lease, registration, activation,
grant renewal, heartbeat and close.

The socket-level protocol 6 flows (hello, registered, grant_request over the
wire) live in tests/test_fleet_protocol6.py.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app import db, worker_authority
from app.main import app


def _register_kwargs(worker_id: str, incarnation: uuid.UUID, **overrides) -> dict:
    kwargs = {
        "worker_id": worker_id,
        "incarnation": incarnation,
        "protocol_version": 6,
        "grant_nonce": uuid.uuid4(),
        "capabilities": ["authority_fence"],
        "manifests": [{"id": "authority-model", "parameters": {}}],
        "device": "cpu-test",
        "memory_mode": "offload",
    }
    kwargs.update(overrides)
    return kwargs


async def _read_row(worker_id: str, incarnation: uuid.UUID, columns: str) -> dict:
    async with db.session_factory() as session:
        return (await session.execute(
            text(f"SELECT {columns} FROM worker_connections "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one()


async def _register(kwargs: dict) -> "worker_authority.SchedulerLease":
    # BlockingPortal.call takes no keyword arguments, and register_worker is
    # keyword-only by design.
    return await worker_authority.register_worker(**kwargs)


@pytest.mark.db
def test_scheduler_lease_acquires_renews_and_takes_over_after_expiry(monkeypatch):
    real_acquire = worker_authority.acquire_scheduler_lease
    real_renew = worker_authority.renew_scheduler_lease

    async def blocked(*_args, **_kwargs):
        raise worker_authority.AuthorityUnavailable("the test drives the lease itself")

    # The lifespan's maintenance task renews on a three second timer; blocking
    # it before the lifespan starts keeps the expiry below the test's own.
    monkeypatch.setattr(worker_authority, "acquire_scheduler_lease", blocked)
    monkeypatch.setattr(worker_authority, "renew_scheduler_lease", blocked)

    with TestClient(app) as client:
        async def scenario():
            lease = await real_acquire()
            assert worker_authority.current_lease() == lease
            renewed = await real_renew()
            assert renewed.owner_epoch == lease.owner_epoch
            assert renewed.lease_id == lease.lease_id
            assert worker_authority.current_lease() == renewed
            async with db.session_factory() as session:
                await session.execute(
                    text(
                        "UPDATE scheduler_leases SET expires_at = clock_timestamp() "
                        "- interval '1 second' WHERE region = :region"
                    ),
                    {"region": worker_authority.REGION},
                )
                await session.commit()
            taken = await real_acquire()
            assert taken.owner_epoch == lease.owner_epoch + 1
            assert taken.lease_id != lease.lease_id
            assert worker_authority.current_lease() == taken
            # A renew under the lease the takeover displaced must find nothing.
            worker_authority._lease = lease
            with pytest.raises(worker_authority.AuthorityUnavailable):
                await real_renew()
            assert worker_authority.current_lease() is None
            worker_authority._lease = taken
            return lease, taken

        lease, taken = client.portal.call(scenario)
    assert lease.region == worker_authority.REGION
    assert taken.region == worker_authority.REGION


@pytest.mark.db
def test_a_live_scheduler_lease_refuses_another_owner(monkeypatch):
    real_acquire = worker_authority.acquire_scheduler_lease

    async def blocked(*_args, **_kwargs):
        raise worker_authority.AuthorityUnavailable("the test drives the lease itself")

    monkeypatch.setattr(worker_authority, "acquire_scheduler_lease", blocked)
    monkeypatch.setattr(worker_authority, "renew_scheduler_lease", blocked)

    with TestClient(app) as client:
        async def scenario():
            held = await real_acquire()
            monkeypatch.setattr(worker_authority, "SCHEDULER_OWNER_ID", uuid.uuid4())
            with pytest.raises(worker_authority.AuthorityUnavailable):
                await real_acquire()
            async with db.session_factory() as session:
                row = (await session.execute(
                    text("SELECT owner_epoch, lease_id FROM scheduler_leases "
                         "WHERE region = :region"),
                    {"region": worker_authority.REGION},
                )).mappings().one()
            assert (row["owner_epoch"], row["lease_id"]) == (held.owner_epoch, held.lease_id)

        client.portal.call(scenario)


@pytest.mark.db
def test_worker_registration_commits_a_fenced_connecting_incarnation():
    worker_id = f"authority-{uuid.uuid4()}"
    incarnation = uuid.uuid4()

    with TestClient(app) as client:
        lease = client.portal.call(worker_authority.acquire_scheduler_lease)
        committed = client.portal.call(
            _register, _register_kwargs(worker_id, incarnation))
        # The maintenance task may renew the lease between these two reads, so
        # the identity is what registration must agree on, not the expiry.
        assert committed.region == lease.region
        assert committed.owner_epoch == lease.owner_epoch
        assert committed.lease_id == lease.lease_id

        row = client.portal.call(_read_row, worker_id, incarnation,
                                 "transport_owner_id, owner_epoch, lease_id, lifecycle, "
                                 "protocol_version, grant_ready, closed_at, "
                                 "envelope_revision")
        assert row["transport_owner_id"] == worker_authority.TRANSPORT_OWNER_ID
        assert row["owner_epoch"] == lease.owner_epoch
        assert row["lease_id"] == lease.lease_id
        assert row["lifecycle"] == "connecting"
        assert row["protocol_version"] == 6
        assert row["grant_ready"] is False
        assert row["closed_at"] is None
        assert row["envelope_revision"] == 1

        # An incarnation is single use, however the connection ended before.
        with pytest.raises(worker_authority.AuthorityUnavailable):
            client.portal.call(_register, _register_kwargs(worker_id, incarnation))
        client.portal.call(worker_authority.close_worker, worker_id, incarnation)


async def _open_rows(worker_id: str) -> list[uuid.UUID]:
    async with db.session_factory() as session:
        return list((await session.scalars(
            text("SELECT incarnation FROM worker_connections "
                 "WHERE worker_id = :worker_id AND closed_at IS NULL"),
            {"worker_id": worker_id},
        )).all())


@pytest.mark.db
def test_a_fixed_worker_id_registers_again_after_its_row_was_left_open(monkeypatch):
    """A crash leaves the connection row open. The same worker id must still
    register; only a row another owner holds under the current lease refuses."""
    worker_id = f"authority-{uuid.uuid4()}"
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with TestClient(app) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        client.portal.call(_register, _register_kwargs(worker_id, first))
        client.portal.call(_register, _register_kwargs(worker_id, second))
        assert client.portal.call(_open_rows, worker_id) == [second]

        monkeypatch.setattr(worker_authority, "TRANSPORT_OWNER_ID", uuid.uuid4())
        with pytest.raises(worker_authority.AuthorityUnavailable):
            client.portal.call(_register, _register_kwargs(worker_id, third))
        assert client.portal.call(_open_rows, worker_id) == [second]

        # A lease takeover fences the old owner's row out, so the new owner
        # may register the same worker id.
        async def take_over() -> None:
            await _expire_lease()
            monkeypatch.setattr(worker_authority, "SCHEDULER_OWNER_ID", uuid.uuid4())
            await worker_authority.acquire_scheduler_lease()

        client.portal.call(take_over)
        try:
            client.portal.call(_register, _register_kwargs(worker_id, third))
            assert client.portal.call(_open_rows, worker_id) == [third]
        finally:
            # Release the borrowed owner's lease, or the next test's process
            # owner waits out its expiry.
            client.portal.call(_expire_lease)


async def _expire_lease() -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE scheduler_leases SET expires_at = clock_timestamp() "
                 "- interval '1 second' WHERE region = :region"),
            {"region": worker_authority.REGION},
        )
        await session.commit()


@pytest.mark.db
def test_activate_initial_worker_requires_a_ready_envelope_and_its_own_row():
    ready_worker = f"authority-activate-{uuid.uuid4()}"
    bare_worker = f"{ready_worker}-bare"
    ready_incarnation = uuid.uuid4()
    bare_incarnation = uuid.uuid4()

    with TestClient(app) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)

        async def scenario():
            await worker_authority.register_worker(
                **_register_kwargs(ready_worker, ready_incarnation))
            activated = await worker_authority.activate_initial_worker(
                ready_worker, ready_incarnation)
            # Second activation finds no connecting row left to promote.
            again = await worker_authority.activate_initial_worker(
                ready_worker, ready_incarnation)
            unknown = await worker_authority.activate_initial_worker(
                ready_worker, uuid.uuid4())
            # An envelope without a device or memory mode never became ready.
            await worker_authority.register_worker(
                **_register_kwargs(bare_worker, bare_incarnation,
                                   device=None, memory_mode=None))
            bare = await worker_authority.activate_initial_worker(
                bare_worker, bare_incarnation)
            return activated, again, unknown, bare

        assert client.portal.call(scenario) == (True, False, False, False)
        assert client.portal.call(
            _read_row, ready_worker, ready_incarnation, "lifecycle")["lifecycle"] == "ready"
        assert client.portal.call(
            _read_row, bare_worker, bare_incarnation, "lifecycle")["lifecycle"] == "connecting"
        client.portal.call(worker_authority.close_worker, ready_worker, ready_incarnation)
        client.portal.call(worker_authority.close_worker, bare_worker, bare_incarnation)


@pytest.mark.db
def test_renew_worker_grant_is_ready_only_for_an_activated_connection():
    activated_worker = f"authority-grant-{uuid.uuid4()}"
    connecting_worker = f"{activated_worker}-connecting"
    activated_incarnation = uuid.uuid4()
    connecting_incarnation = uuid.uuid4()

    with TestClient(app) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)

        async def scenario():
            await worker_authority.register_worker(
                **_register_kwargs(activated_worker, activated_incarnation))
            assert await worker_authority.activate_initial_worker(
                activated_worker, activated_incarnation)
            nonce = uuid.uuid4()
            ready = await worker_authority.renew_worker_grant(
                activated_worker, activated_incarnation, nonce)
            await worker_authority.register_worker(
                **_register_kwargs(connecting_worker, connecting_incarnation))
            pending = await worker_authority.renew_worker_grant(
                connecting_worker, connecting_incarnation, uuid.uuid4())
            return ready, pending, nonce

        ready, pending, nonce = client.portal.call(scenario)
        assert ready.worker_id == activated_worker
        assert ready.incarnation == activated_incarnation
        assert ready.region == worker_authority.REGION
        assert ready.grant_nonce == nonce
        assert ready.ready is True
        assert 0 < ready.remaining_ms <= 10_000
        assert ready.owner_epoch > 0
        assert ready.lease_id is not None
        assert pending.ready is False
        assert pending.remaining_ms == 0

        # A closed row is no longer the connection anybody may renew.
        client.portal.call(worker_authority.close_worker,
                           activated_worker, activated_incarnation)
        with pytest.raises(worker_authority.AuthorityUnavailable):
            client.portal.call(worker_authority.renew_worker_grant,
                               activated_worker, activated_incarnation, uuid.uuid4())
        client.portal.call(worker_authority.close_worker,
                           connecting_worker, connecting_incarnation)


@pytest.mark.db
def test_touch_worker_records_a_heartbeat_until_the_row_closes():
    worker_id = f"authority-touch-{uuid.uuid4()}"
    incarnation = uuid.uuid4()

    with TestClient(app) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        client.portal.call(_register, _register_kwargs(worker_id, incarnation))
        before = client.portal.call(_read_row, worker_id, incarnation,
                                    "last_seen_at, expires_at, observations")
        client.portal.call(worker_authority.touch_worker, worker_id, incarnation,
                           {"authority-model": 421})
        after = client.portal.call(_read_row, worker_id, incarnation,
                                   "last_seen_at, expires_at, observations")
        assert after["last_seen_at"] >= before["last_seen_at"]
        assert after["expires_at"] > before["expires_at"]
        assert before["observations"] == {}
        assert after["observations"]["frame_p95_ms"] == {"authority-model": 421}

        client.portal.call(worker_authority.close_worker, worker_id, incarnation)
        assert client.portal.call(_read_row, worker_id, incarnation,
                                  "lifecycle, closed_at")["lifecycle"] == "ended"
        with pytest.raises(worker_authority.AuthorityUnavailable):
            client.portal.call(worker_authority.touch_worker, worker_id, incarnation, None)


def test_lease_maintenance_survives_an_unexpected_database_error(monkeypatch):
    calls = 0
    second_call = asyncio.Event()

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("connection reset")
        second_call.set()

    monkeypatch.setattr(worker_authority, "acquire_scheduler_lease", flaky)
    monkeypatch.setattr(worker_authority, "_lease", None)
    monkeypatch.setattr(worker_authority.db, "session_factory", object())

    async def scenario() -> None:
        task = asyncio.create_task(worker_authority.maintain_scheduler_lease())
        try:
            # A failed call is retried after 1 s.
            await asyncio.wait_for(second_call.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert calls >= 2


def test_lease_maintenance_stops_when_cancelled_as_a_call_completes(monkeypatch):
    """Shutdown cancels the loop. If the cancel lands just as a renewal
    returns, the loop must still stop, or lifespan shutdown waits forever."""
    loop_task: asyncio.Task | None = None

    async def completes_while_cancelled() -> None:
        assert loop_task is not None
        loop_task.cancel()

    monkeypatch.setattr(worker_authority, "acquire_scheduler_lease", completes_while_cancelled)
    monkeypatch.setattr(worker_authority, "_lease", None)
    monkeypatch.setattr(worker_authority.db, "session_factory", object())

    async def scenario() -> None:
        nonlocal loop_task
        loop_task = asyncio.create_task(worker_authority.maintain_scheduler_lease())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(loop_task), 5)

    asyncio.run(scenario())
