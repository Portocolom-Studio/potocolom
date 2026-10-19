import hashlib
import os
import uuid
from contextlib import ExitStack, contextmanager

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import command_store, db, jobs, operator, sessions as account_sessions
from app import worker_authority
from app.main import app
from app.settings import get_settings
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    drop_worker_rows,
    fresh_pool,
    root_keys,
    seed_queued_job,
)
from test_command_store import command_ack_fields
from test_totp_flow import _make as make_account_user, accounts
from test_v6_sessions import open_live_session

__all__ = ["accounts"]

_LEASE_LOCK = "SELECT region FROM scheduler_leases WHERE region = :region FOR UPDATE"
_JOURNAL_LOCK = (
    "SELECT command_sequence FROM worker_commands "
    "WHERE worker_id = $1 AND incarnation = $2 "
    "AND command_sequence = $3 AND account_user_id = $4 FOR UPDATE NOWAIT"
)


class _JournalBoundary:
    def __init__(self, observer, journals):
        self.observer = observer
        self.journals = journals
        self.outcomes: dict[str, str] = {}
        self.fired = False

    async def probe(self) -> dict[str, str]:
        outcomes = {}
        for journal in self.journals:
            found = None
            try:
                async with self.observer.transaction():
                    found = await self.observer.fetchval(
                        _JOURNAL_LOCK,
                        journal["worker_id"],
                        journal["incarnation"],
                        journal["sequence"],
                        journal["account_user_id"],
                    )
            except asyncpg.LockNotAvailableError:
                outcomes[journal["label"]] = "held"
                continue
            outcomes[journal["label"]] = "free" if found is not None else "missing"
        return outcomes

    async def guard(self, statement) -> None:
        if self.fired or " ".join(str(statement).split()) != _LEASE_LOCK:
            return
        self.fired = True
        self.outcomes = await self.probe()
        still_lockable = sorted(
            label for label, outcome in self.outcomes.items() if outcome != "held"
        )
        assert not still_lockable, (
            "operator.collapse locked its first regional lease while these command "
            f"journals were still lockable by another transaction: {still_lockable}"
        )


class _WatchedSession:
    def __init__(self, session, boundary):
        self._session = session
        self._boundary = boundary

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._session.__aexit__(*exc_info)

    async def execute(self, statement, *args, **kwargs):
        await self._boundary.guard(statement)
        return await self._session.execute(statement, *args, **kwargs)


@contextmanager
def _two_worker_client(*worker_ids):
    with TestClient(app, headers=FLEET_HEADERS) as client:
        try:
            yield client
        finally:
            client.portal.call(fresh_pool)
            for worker_id in worker_ids:
                client.portal.call(drop_worker_rows, worker_id)


async def _open_observer():
    return await asyncpg.connect(os.environ["DATABASE_URL"])


async def _pending_journals(first: uuid.UUID, later: uuid.UUID) -> list[dict]:
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT worker_id, incarnation, command_sequence, account_user_id, kind "
                 "FROM worker_commands WHERE state = 'pending' "
                 "AND account_user_id IN (:first, :later) "
                 "ORDER BY worker_id, command_sequence"),
            {"first": first, "later": later},
        )).mappings().all()
    return [
        {
            "label": f"{row['kind']}:"
                     f"{'first' if row['account_user_id'] == first else 'later'}",
            "worker_id": row["worker_id"],
            "incarnation": row["incarnation"],
            "sequence": row["command_sequence"],
            "account_user_id": row["account_user_id"],
        }
        for row in rows
    ]


async def _seed_thin_history(first: uuid.UUID, later: uuid.UUID,
                             worker_by_user: dict[uuid.UUID, str]) -> list[dict]:
    journals = []
    async with db.session_factory() as session:
        async with session.begin():
            for account_user_id, state, label in (
                (first, "acknowledged", "thin:acknowledged:first"),
                (later, "erased", "thin:erased:later"),
            ):
                worker_id = worker_by_user[account_user_id]
                worker = (await session.execute(
                    text("SELECT worker_id, incarnation, region, owner_epoch, lease_id, "
                         "lease_expires_at, grant_nonce, acknowledged_floor, "
                         "next_command_sequence FROM worker_connections "
                         "WHERE worker_id = :worker_id AND closed_at IS NULL"),
                    {"worker_id": worker_id},
                )).mappings().one()
                pending = (await session.execute(
                    text("SELECT command_sequence FROM worker_commands "
                         "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                         "AND account_user_id = :account_user_id AND state = 'pending' "
                         "ORDER BY command_sequence"),
                    {"worker_id": worker_id, "incarnation": worker["incarnation"],
                     "account_user_id": account_user_id},
                )).scalars().all()
                floor = worker["acknowledged_floor"]
                sequence = worker["next_command_sequence"]
                assert pending == [floor + 1, floor + 2], pending
                assert sequence == floor + 3, (floor, pending, sequence)
                assert sequence > floor

                command_id = uuid.uuid4()
                subject_id = uuid.uuid4()
                control = {
                    "type": "cancel_job",
                    "worker_id": worker["worker_id"],
                    "incarnation": str(worker["incarnation"]),
                    "region": worker["region"],
                    "owner_epoch": worker["owner_epoch"],
                    "lease_id": str(worker["lease_id"]),
                    "lease_expires_at": worker["lease_expires_at"].isoformat().replace(
                        "+00:00", "Z"),
                    "grant_nonce": str(worker["grant_nonce"]),
                    "command_sequence": sequence,
                    "command_id": str(command_id),
                    "job_id": str(subject_id),
                    "dispatch_sequence": 1,
                }
                body_hash = hashlib.sha256(
                    command_store.canonical_bytes(control)
                ).hexdigest()
                control["body_hash"] = body_hash
                ack_identity_hash = command_store._ack_identity_hash("cancel_job", control)
                ack_body = None
                ack_status = None
                if state == "acknowledged":
                    ack = {
                        "type": "command_ack",
                        **command_ack_fields(control),
                        "status": "accepted",
                    }
                    ack_body = command_store.canonical_bytes(ack)
                    ack_status = "accepted"

                await session.execute(
                    text("INSERT INTO worker_commands "
                         "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                         "account_user_id, subject_id, subject_sequence, ack_identity_hash, "
                         "key_version, encrypted_body, complete_bytes, state, acknowledged_at, "
                         "ack_status, ack_code, expected_sequence, ack_body) "
                         "VALUES (:worker_id, :incarnation, :sequence, :command_id, 'cancel_job', "
                         ":body_hash, :account_user_id, :subject_id, 1, :ack_identity_hash, "
                         "NULL, NULL, 0, :state, CASE WHEN :state = 'acknowledged' "
                         "THEN clock_timestamp() END, :ack_status, NULL, NULL, :ack_body)"),
                    {"worker_id": worker_id, "incarnation": worker["incarnation"],
                     "sequence": sequence, "command_id": command_id,
                     "body_hash": body_hash, "account_user_id": account_user_id,
                     "subject_id": subject_id, "ack_identity_hash": ack_identity_hash,
                     "state": state, "ack_status": ack_status, "ack_body": ack_body},
                )
                next_sequence = await session.scalar(
                    text("UPDATE worker_connections SET next_command_sequence = "
                         "next_command_sequence + 1 WHERE worker_id = :worker_id "
                         "AND incarnation = :incarnation RETURNING next_command_sequence"),
                    {"worker_id": worker_id, "incarnation": worker["incarnation"]},
                )
                assert next_sequence == sequence + 1
                journals.append({
                    "label": label,
                    "worker_id": worker_id,
                    "incarnation": worker["incarnation"],
                    "sequence": sequence,
                    "account_user_id": account_user_id,
                })
    return journals


async def _own(job_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
            {"user_id": user_id, "job_id": job_id},
        )
        await session.commit()


async def _counters(worker_ids: list[str]) -> dict:
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT worker_id, incarnation, next_command_sequence, "
                 "acknowledged_floor FROM worker_connections "
                 "WHERE worker_id IN (:first, :second)"),
            {"first": worker_ids[0], "second": worker_ids[1]},
        )).mappings().all()
    return {
        (row["worker_id"], row["incarnation"]):
            (row["next_command_sequence"], row["acknowledged_floor"])
        for row in rows
    }


async def _closed(worker_ids: list[str]) -> dict:
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT worker_id, closed_at FROM worker_connections "
                 "WHERE worker_id IN (:first, :second)"),
            {"first": worker_ids[0], "second": worker_ids[1]},
        )).mappings().all()
    return {row["worker_id"]: row["closed_at"] for row in rows}


async def _journal_states(journals: list[dict]) -> dict:
    worker_ids = sorted({journal["worker_id"] for journal in journals})
    assert len(worker_ids) == 2
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT * FROM worker_commands "
                 "WHERE worker_id IN (:first, :second)"),
            {"first": worker_ids[0], "second": worker_ids[1]},
        )).mappings().all()
    return {(row["worker_id"], row["command_sequence"]): row for row in rows}


async def _job_owners(job_ids: list[uuid.UUID]) -> dict:
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT id, user_id FROM jobs WHERE id IN (:first, :second)"),
            {"first": job_ids[0], "second": job_ids[1]},
        )).mappings().all()
    return {row["id"]: row["user_id"] for row in rows}


async def _restore_accounts_mode() -> None:
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE installation_auth_state SET auth_mode = 'accounts', "
                 "root_key_version = 1 WHERE id = 1"))
        await session.commit()


async def _regions(worker_ids: list[str]) -> list[str]:
    async with db.session_factory() as session:
        return list((await session.execute(
            text("SELECT region FROM worker_connections "
                 "WHERE worker_id IN (:first, :second) ORDER BY worker_id"),
            {"first": worker_ids[0], "second": worker_ids[1]},
        )).scalars().all())


@pytest.mark.db
def test_collapse_locks_every_affected_journal_before_any_regional_lease(
    accounts, monkeypatch,
):
    root_keys(monkeypatch)

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    first_worker_id = f"s1-order-first-{uuid.uuid4()}"
    later_worker_id = f"s1-order-later-{uuid.uuid4()}"
    first_model_id = f"s1-order-first-model-{uuid.uuid4()}"
    later_model_id = f"s1-order-later-model-{uuid.uuid4()}"

    with ExitStack() as stack:
        client = stack.enter_context(_two_worker_client(first_worker_id, later_worker_id))

        first_user = client.portal.call(
            make_account_user, f"s1-order-first-{uuid.uuid4()}@example.test")
        later_user = client.portal.call(
            make_account_user, f"s1-order-later-{uuid.uuid4()}@example.test")
        earlier_user, later_user = sorted(
            (first_user, later_user), key=lambda user: user.id)
        first_is_earlier = earlier_user.id == first_user.id
        earlier_worker = first_worker_id if first_is_earlier else later_worker_id
        later_worker = later_worker_id if first_is_earlier else first_worker_id
        earlier_model = first_model_id if first_is_earlier else later_model_id
        later_model = later_model_id if first_is_earlier else first_model_id
        worker_by_user = {earlier_user.id: earlier_worker, later_user.id: later_worker}

        job_ids = []
        for user, worker_id, model_id in (
            (earlier_user, earlier_worker, earlier_model),
            (later_user, later_worker, later_model),
        ):
            issued = client.portal.call(account_sessions.mint, user, False)
            cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
            client.cookies.set(cookie, issued.token)
            client.cookies.set(csrf, issued.csrf)
            worker_ws, _worker, _grant, _open, browser_ws, browser, fleet = (
                open_live_session(client, worker_id, model_id)
            )
            stack.callback(browser.__exit__, None, None, None)
            stack.callback(fleet.__exit__, None, None, None)
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            assert update_command["type"] == "update_session"
            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.call(_own, job_id, user.id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            job_ids.append(job_id)
            stack.callback(client.portal.call, _own, job_id, db.local_user_id)

        stack.callback(client.portal.call, _restore_accounts_mode)

        first_id, later_id = sorted((earlier_user.id, later_user.id))
        journals = client.portal.call(_pending_journals, first_id, later_id)
        assert {journal["label"] for journal in journals} == {
            "update_session:first", "dispatch_job:first",
            "update_session:later", "dispatch_job:later",
        }
        for journal in journals:
            assert journal["worker_id"] == worker_by_user[journal["account_user_id"]]
        assert len({journal["worker_id"] for journal in journals}) == 2
        regions = client.portal.call(_regions, [first_worker_id, later_worker_id])
        assert regions == [worker_authority.REGION, worker_authority.REGION]

        thin_journals = client.portal.call(
            _seed_thin_history, first_id, later_id, worker_by_user)
        journals.extend(thin_journals)
        before_states = client.portal.call(_journal_states, journals)
        thin_before = {
            (journal["worker_id"], journal["sequence"]): before_states[
                (journal["worker_id"], journal["sequence"])]
            for journal in thin_journals
        }

        observer = client.portal.call(_open_observer)
        stack.callback(client.portal.call, observer.close)
        boundary = _JournalBoundary(observer, journals)
        control = client.portal.call(boundary.probe)
        assert set(control) == {journal["label"] for journal in journals}
        assert all(outcome == "free" for outcome in control.values()), control

        counters_before = client.portal.call(
            _counters, [first_worker_id, later_worker_id])

        real_factory = db.session_factory

        def watched_factory():
            return _WatchedSession(real_factory(), boundary)

        db.session_factory = watched_factory
        try:
            counted = client.portal.call(operator.collapse, operator.COLLAPSE_PHRASE)
        finally:
            db.session_factory = real_factory

        assert boundary.fired
        assert counted == {"accounts": 2, "generations": 2}
        assert all(outcome == "held" for outcome in boundary.outcomes.values())

        states = client.portal.call(_journal_states, journals)
        for journal in journals:
            row = states[(journal["worker_id"], journal["sequence"])]
            if journal in thin_journals:
                assert row == thin_before[(journal["worker_id"], journal["sequence"])]
                continue
            assert row["state"] == "erased"
            assert row["encrypted_body"] is None
            assert row["key_version"] is None
            assert row["complete_bytes"] == 0

        closed = client.portal.call(_closed, [first_worker_id, later_worker_id])
        assert set(closed) == {first_worker_id, later_worker_id}
        assert all(value is not None for value in closed.values())

        counters_after = client.portal.call(
            _counters, [first_worker_id, later_worker_id])
        assert counters_after == counters_before

        owners = client.portal.call(_job_owners, job_ids)
        assert set(owners) == set(job_ids)
        assert all(owner == db.local_user_id for owner in owners.values())
