"""Worker close locks its complete journal before the first regional lease."""

import asyncio
import hashlib
import os
import uuid
from contextlib import ExitStack, contextmanager

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import command_store, db, jobs, realtime, sessions as account_sessions, worker_authority
from app.main import app
from app.protocol6 import decode_control
from app.settings import get_settings
from test_fleet_protocol6 import (
    FLEET_HEADERS,
    drop_worker_rows,
    fresh_pool,
    hello,
    manifest,
    request_work_grant,
    root_keys,
    seed_queued_job,
    wait_for_identity,
)
from test_command_store import command_ack_fields
from test_totp_flow import _make as make_account_user, accounts
from test_v6_sessions import command_ack, session_ready_message

__all__ = ["accounts"]

_LEASE_LOCK = "SELECT region FROM scheduler_leases WHERE region = :region FOR UPDATE"
_JOURNAL_LOCK = (
    "SELECT command_sequence FROM worker_commands "
    "WHERE worker_id = $1 AND incarnation = $2 "
    "AND command_sequence = $3 AND account_user_id = $4 FOR UPDATE NOWAIT"
)
_RANGE_LOCK = (
    "SELECT range_id FROM worker_physical_ranges WHERE range_id = $1 "
    "AND worker_id = $2 AND incarnation = $3 FOR UPDATE NOWAIT"
)


@contextmanager
def _two_worker_client(*worker_ids):
    with TestClient(app, headers=FLEET_HEADERS) as client:
        try:
            yield client
        finally:
            client.portal.call(fresh_pool)
            client.portal.call(drop_worker_rows, worker_ids[0])
            for worker_id in worker_ids[1:]:
                client.portal.call(_drop_worker_rows_if_present, worker_id)


async def _drop_worker_rows_if_present(worker_id: str):
    async with db.session_factory() as session:
        await session.execute(
            text("DELETE FROM gpu_samples WHERE worker_id = :worker_id"),
            {"worker_id": worker_id},
        )
        await session.execute(
            text("DELETE FROM workers WHERE worker_id = :worker_id"),
            {"worker_id": worker_id},
        )
        await session.commit()


class _JournalBoundary:
    def __init__(self, observer, journals):
        self.observer = observer
        self.journals = journals
        self.outcomes = {}
        self.fired = False

    async def probe(self):
        outcomes = {}
        for journal in self.journals:
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
            else:
                outcomes[journal["label"]] = "free" if found is not None else "missing"
        return outcomes

    async def guard(self, statement):
        if self.fired or " ".join(str(statement).split()) != _LEASE_LOCK:
            return
        self.fired = True
        self.outcomes = await self.probe()
        unheld = sorted(label for label, state in self.outcomes.items() if state != "held")
        assert not unheld, (
            "worker_authority.close_worker took a regional lease before locking every "
            f"command row for its worker incarnation: {unheld}"
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


class _RangeJournalBoundary:
    def __init__(self, observer, ranges, old_journal):
        self.observer = observer
        self.ranges = ranges
        self.old_journal = old_journal
        self.fired = False
        self.range_outcomes = {}
        self.old_journal_outcome = None
        self.lease_checked = False
        self.old_journal_at_lease = None

    async def probe_ranges(self):
        outcomes = {}
        for item in self.ranges:
            try:
                async with self.observer.transaction():
                    found = await self.observer.fetchval(
                        _RANGE_LOCK, item["range_id"], item["worker_id"],
                        item["incarnation"],
                    )
            except asyncpg.LockNotAvailableError:
                outcomes[str(item["range_id"])] = "held"
            else:
                outcomes[str(item["range_id"])] = "free" if found is not None else "missing"
        return outcomes

    async def probe_old_journal(self):
        row = self.old_journal
        try:
            async with self.observer.transaction():
                found = await self.observer.fetchval(
                    _JOURNAL_LOCK, row["worker_id"], row["incarnation"],
                    row["sequence"], row["account_user_id"],
                )
        except asyncpg.LockNotAvailableError:
            return "held"
        return "free" if found is not None else "missing"

    async def guard(self, statement):
        sql = " ".join(str(statement).split())
        if self.fired and not self.lease_checked and sql == _LEASE_LOCK:
            self.lease_checked = True
            self.old_journal_at_lease = await self.probe_old_journal()
            assert self.old_journal_at_lease == "free", (
                "close_worker locked retained history from another incarnation of the "
                f"same worker before its first regional lease: {self.old_journal_at_lease}"
            )
            return
        if self.fired or "FROM worker_commands" not in sql or "FOR UPDATE" not in sql:
            return
        self.fired = True
        self.old_journal_outcome = await self.probe_old_journal()
        self.range_outcomes = await self.probe_ranges()
        assert self.old_journal_outcome == "free", (
            "close_worker locked retained history from another incarnation of the same "
            f"worker: {self.old_journal_outcome}"
        )
        unheld = sorted(key for key, outcome in self.range_outcomes.items()
                        if outcome != "held")
        assert not unheld, (
            "close_worker began locking journal rows before locking every selected "
            f"physical range: {unheld}"
        )


async def _open_observer():
    return await asyncpg.connect(os.environ["DATABASE_URL"])


async def _wait_for_floor(worker_id: str, incarnation: uuid.UUID, floor: int):
    for _ in range(100):
        async with db.session_factory() as session:
            current = await session.scalar(
                text("SELECT acknowledged_floor FROM worker_connections "
                     "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                {"worker_id": worker_id, "incarnation": incarnation},
            )
        if current >= floor:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the worker did not compact the acknowledged open command")


async def _own_job(job_id: uuid.UUID, user_id: uuid.UUID):
    async with db.session_factory() as session:
        await session.execute(
            text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
            {"user_id": user_id, "job_id": job_id},
        )
        await session.commit()


async def _journal(worker_id: str, incarnation: uuid.UUID):
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT * FROM worker_commands WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation ORDER BY command_sequence"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().all()
    return [dict(row) for row in rows]


async def _old_incarnation_history(worker_id: str, incarnation: uuid.UUID,
                                   account_user_id: uuid.UUID):
    async with db.session_factory() as session:
        authority = (await session.execute(
            text("SELECT region, owner_epoch, lease_id, lease_expires_at, grant_nonce "
                 "FROM worker_connections WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one()
        old_incarnation = uuid.uuid4()
        subject_id = uuid.uuid4()
        command_id = uuid.uuid4()
        control = {
            "type": "cancel_job",
            "worker_id": worker_id,
            "incarnation": str(old_incarnation),
            "region": authority["region"],
            "owner_epoch": authority["owner_epoch"],
            "lease_id": str(authority["lease_id"]),
            "lease_expires_at": authority["lease_expires_at"].isoformat().replace(
                "+00:00", "Z"),
            "grant_nonce": str(authority["grant_nonce"]),
            "command_sequence": 1,
            "command_id": str(command_id),
            "job_id": str(subject_id),
            "dispatch_sequence": 7,
        }
        control["body_hash"] = hashlib.sha256(
            command_store.canonical_bytes(control)
        ).hexdigest()
        ack_identity_hash = command_store._ack_identity_hash("cancel_job", control)
        ack_body = command_store.canonical_bytes({
            "type": "command_ack",
            **command_ack_fields(control),
            "status": "accepted",
        })
        await session.execute(
            text("INSERT INTO worker_commands "
                 "(worker_id, incarnation, command_sequence, command_id, kind, body_hash, "
                 "account_user_id, subject_id, subject_sequence, ack_identity_hash, "
                 "key_version, encrypted_body, complete_bytes, state, acknowledged_at, "
                 "ack_status, ack_code, expected_sequence, ack_body) "
                 "VALUES (:worker_id, :incarnation, 1, :command_id, 'cancel_job', "
                 ":body_hash, :account_user_id, :subject_id, 7, :ack_identity_hash, "
                 "NULL, NULL, 0, 'acknowledged', clock_timestamp(), 'accepted', NULL, NULL, "
                 ":ack_body)"),
            {"worker_id": worker_id, "incarnation": old_incarnation,
             "command_id": command_id, "body_hash": control["body_hash"],
             "account_user_id": account_user_id, "subject_id": subject_id,
             "ack_identity_hash": ack_identity_hash, "ack_body": ack_body},
        )
        await session.commit()
        row = (await session.execute(
            text("SELECT * FROM worker_commands WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation AND command_sequence = 1"),
            {"worker_id": worker_id, "incarnation": old_incarnation},
        )).mappings().one()
    return dict(row)


async def _old_incarnation_history_row(worker_id: str, incarnation: uuid.UUID):
    async with db.session_factory() as session:
        row = (await session.execute(
            text("SELECT * FROM worker_commands WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation AND command_sequence = 1"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one()
    return dict(row)


async def _wait_for_update(worker_id: str, incarnation: uuid.UUID,
                           session_id: uuid.UUID):
    for _ in range(100):
        async with db.session_factory() as session:
            row = (await session.execute(
                text("SELECT * FROM worker_commands WHERE worker_id = :worker_id "
                     "AND incarnation = :incarnation AND kind = 'update_session' "
                     "AND subject_id = :session_id AND state = 'pending' "
                     "ORDER BY command_sequence DESC LIMIT 1"),
                {"worker_id": worker_id, "incarnation": incarnation,
                 "session_id": session_id},
            )).mappings().one_or_none()
        if row is not None:
            return dict(row)
        await asyncio.sleep(0.01)
    raise AssertionError("the session update did not enter the worker journal")


async def _connection(worker_id: str, incarnation: uuid.UUID):
    async with db.session_factory() as session:
        return dict((await session.execute(
            text("SELECT lifecycle, closed_at, next_command_sequence, acknowledged_floor "
                 "FROM worker_connections WHERE worker_id = :worker_id "
                 "AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one())


async def _attempts(session_ids: list[uuid.UUID]):
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT session_id, state, ended_at, range_id FROM realtime_session_attempts "
                 "WHERE session_id = ANY(:session_ids) ORDER BY session_id"),
            {"session_ids": session_ids},
        )).mappings().all()
    return [dict(row) for row in rows]


async def _range_states(range_ids: list[uuid.UUID]):
    async with db.session_factory() as session:
        rows = (await session.execute(
            text("SELECT range_id, state FROM worker_physical_ranges "
                 "WHERE range_id = ANY(:range_ids) ORDER BY range_id"),
            {"range_ids": range_ids},
        )).mappings().all()
    return {row["range_id"]: row["state"] for row in rows}


async def _job_attempt(job_id: uuid.UUID):
    async with db.session_factory() as session:
        return dict((await session.execute(
            text("SELECT attempt.worker_id, attempt.incarnation, attempt.state, "
                 "attempt.dispatch_sequence, attempt.range_id "
                 "FROM job_attempts attempt WHERE attempt.job_id = :job_id "
                 "AND attempt.dispatch_sequence = (SELECT current_dispatch_sequence "
                 "FROM jobs WHERE id = :job_id)"),
            {"job_id": job_id},
        )).mappings().one())


async def _other_connection(worker_id: str, incarnation: uuid.UUID):
    async with db.session_factory() as session:
        return dict((await session.execute(
            text("SELECT worker_id, incarnation, lifecycle, closed_at, "
                 "next_command_sequence, acknowledged_floor FROM worker_connections "
                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
            {"worker_id": worker_id, "incarnation": incarnation},
        )).mappings().one())


def _open_session(client, stack, fleet_ws, worker, grant, model_id: str):
    browser_ws = stack.enter_context(client.websocket_connect("/api/v1/realtime"))
    browser_ws.send_json({
        "type": "open",
        "model_id": model_id,
        "params": {"prompt": "close-order"},
        "frame_header": 2,
    })
    command = decode_control(fleet_ws.receive_text().encode("utf-8"))
    assert command["type"] == "open_session"
    fleet_ws.send_json(command_ack(command))
    fleet_ws.send_json(session_ready_message(worker, command, grant["grant_nonce"]))
    assert browser_ws.receive_json()["type"] == "ready"
    return browser_ws, command


@pytest.mark.db
@pytest.mark.parametrize(
    ("update_low_session", "boundary_kind"),
    [(True, "lease"), (False, "lease"), (False, "journal")],
    ids=["reversed-session-command-order", "first-session-has-no-journal-row",
         "ranges-before-first-journal-row"],
)
def test_close_worker_locks_incarnation_journal_before_first_regional_lease(
    accounts, monkeypatch, update_low_session, boundary_kind,
):
    root_keys(monkeypatch)

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    worker_id = f"s1-close-order-{uuid.uuid4()}"
    other_worker_id = f"s1-close-other-{uuid.uuid4()}"
    model_id = f"s1-close-model-{uuid.uuid4()}"
    other_model_id = f"s1-close-other-model-{uuid.uuid4()}"

    with _two_worker_client(worker_id, other_worker_id) as client:
        with ExitStack() as stack:
            user_a = client.portal.call(
                make_account_user, f"s1-close-a-{uuid.uuid4()}@example.test")
            user_b = client.portal.call(
                make_account_user, f"s1-close-b-{uuid.uuid4()}@example.test")
            issued = client.portal.call(account_sessions.mint, user_a, False)
            cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
            client.cookies.set(cookie, issued.token)
            client.cookies.set(csrf, issued.csrf)

            client.portal.call(worker_authority.acquire_scheduler_lease)
            incarnation = uuid.uuid4()
            fleet_ws = stack.enter_context(client.websocket_connect("/api/v1/fleet"))
            model_manifest = manifest(model_id)
            model_manifest["capabilities"] = ["realtime", "text_to_image"]
            registration = hello(worker_id, [model_manifest], incarnation=incarnation)
            registration["realtime_slots"] = 2
            fleet_ws.send_json(registration)
            assert fleet_ws.receive_json()["type"] == "registered"
            grant = request_work_grant(fleet_ws, worker_id, incarnation)
            worker = realtime.workers[worker_id]

            browser_first, open_first = _open_session(
                client, stack, fleet_ws, worker, grant, model_id)
            client.portal.call(_wait_for_floor, worker_id, incarnation,
                               open_first["command_sequence"])
            browser_second, open_second = _open_session(
                client, stack, fleet_ws, worker, grant, model_id)
            client.portal.call(_wait_for_floor, worker_id, incarnation,
                               open_second["command_sequence"])
            opened = {
                uuid.UUID(open_first["session_id"]): browser_first,
                uuid.UUID(open_second["session_id"]): browser_second,
            }
            low_session, high_session = sorted(opened)
            assert low_session != high_session
            open_attempts = client.portal.call(_attempts, list(opened))
            assert {row["state"] for row in open_attempts} == {"running"}

            job_id = client.portal.call(seed_queued_job, model_id)
            client.portal.call(_own_job, job_id, user_b.id)
            stack.callback(client.portal.call, _own_job, job_id, db.local_user_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            job_command = decode_control(fleet_ws.receive_text().encode("utf-8"))
            assert job_command["type"] == "dispatch_job"
            assert job_command["job_id"] == str(job_id)

            update_order = [high_session]
            if update_low_session:
                update_order.append(low_session)
            for session_id in update_order:
                opened[session_id].send_json({
                    "type": "update_params",
                    "params": {"prompt": str(session_id)},
                })
                update = client.portal.call(
                    _wait_for_update, worker_id, incarnation, session_id)
                assert update["subject_id"] == session_id

            stack.callback(client.portal.call, drop_worker_rows, other_worker_id)
            other_incarnation = uuid.uuid4()
            other_ws = stack.enter_context(client.websocket_connect("/api/v1/fleet"))
            other_ws.send_json(hello(
                other_worker_id, [manifest(other_model_id)], incarnation=other_incarnation))
            assert other_ws.receive_json()["type"] == "registered"
            client.portal.call(wait_for_identity, other_worker_id)

            journals = client.portal.call(_journal, worker_id, incarnation)
            expected = {
                ("dispatch_job", str(job_id)),
                ("update_session", str(high_session)),
            }
            if update_low_session:
                expected.add(("update_session", str(low_session)))
            assert {(row["kind"], str(row["subject_id"])) for row in journals} == expected
            assert all(row["state"] == "pending" for row in journals)
            if not update_low_session:
                assert low_session not in {
                    row["subject_id"] for row in journals
                    if row["kind"] == "update_session"
                }
            journal_descriptors = [
                {
                    "label": f"{row['kind']}:{row['command_sequence']}",
                    "worker_id": row["worker_id"],
                    "incarnation": row["incarnation"],
                    "sequence": row["command_sequence"],
                    "account_user_id": row["account_user_id"],
                }
                for row in journals
            ]
            counters_before = client.portal.call(_connection, worker_id, incarnation)
            job_before = next(row for row in journals if row["kind"] == "dispatch_job")
            attempt_before = client.portal.call(_job_attempt, job_id)
            other_before = client.portal.call(_other_connection,
                                              other_worker_id, other_incarnation)
            attempts_before = client.portal.call(_attempts, list(opened))
            assert len(attempts_before) == 2
            ranges_before = client.portal.call(
                _range_states, [row["range_id"] for row in attempts_before])
            assert set(ranges_before.values()) == {"outstanding"}

            observer = client.portal.call(_open_observer)
            stack.callback(client.portal.call, observer.close)
            if boundary_kind == "lease":
                boundary = _JournalBoundary(observer, journal_descriptors)
                free = client.portal.call(boundary.probe)
                assert set(free) == {row["label"] for row in journal_descriptors}
                assert all(state == "free" for state in free.values()), free
            else:
                old_journal = client.portal.call(
                    _old_incarnation_history, worker_id, incarnation, user_a.id)
                range_descriptors = [
                    {"range_id": row["range_id"], "worker_id": worker_id,
                     "incarnation": incarnation}
                    for row in attempts_before
                ]
                boundary = _RangeJournalBoundary(observer, range_descriptors, {
                    "worker_id": old_journal["worker_id"],
                    "incarnation": old_journal["incarnation"],
                    "sequence": old_journal["command_sequence"],
                    "account_user_id": old_journal["account_user_id"],
                })
                assert client.portal.call(boundary.probe_old_journal) == "free"
                ranges_free = client.portal.call(boundary.probe_ranges)
                assert set(ranges_free.values()) == {"free"}, ranges_free

            real_factory = db.session_factory
            erase_counts = {}
            real_erase_subject = command_store.erase_subject_commands

            async def watched_erase_subject(session, account_user_id, **identity):
                count = await real_erase_subject(session, account_user_id, **identity)
                erase_counts[identity["subject_id"]] = count
                return count

            monkeypatch.setattr(
                command_store, "erase_subject_commands", watched_erase_subject)

            def watched_factory():
                return _WatchedSession(real_factory(), boundary)

            db.session_factory = watched_factory
            try:
                client.portal.call(worker_authority.close_worker, worker_id, incarnation)
            finally:
                db.session_factory = real_factory

            assert boundary.fired
            if boundary_kind == "lease":
                assert all(state == "held" for state in boundary.outcomes.values())
            else:
                assert set(boundary.range_outcomes.values()) == {"held"}
                assert boundary.old_journal_outcome == "free"
                assert boundary.lease_checked
                assert boundary.old_journal_at_lease == "free"
            counters_after = client.portal.call(_connection, worker_id, incarnation)
            assert counters_after["lifecycle"] == "ended"
            assert counters_after["closed_at"] is not None
            assert (counters_after["next_command_sequence"],
                    counters_after["acknowledged_floor"]) == (
                counters_before["next_command_sequence"],
                counters_before["acknowledged_floor"],
            )

            journal_after = client.portal.call(_journal, worker_id, incarnation)
            job_after = next(row for row in journal_after if row["kind"] == "dispatch_job")
            assert job_after == job_before
            session_updates = [row for row in journal_after if row["kind"] == "update_session"]
            assert len(session_updates) == len(update_order)
            for row in session_updates:
                assert row["state"] == "erased"
                assert row["encrypted_body"] is None
                assert row["key_version"] is None
                assert row["complete_bytes"] == 0

            attempts_after = client.portal.call(_attempts, list(opened))
            assert {row["state"] for row in attempts_after} == {"ended"}
            assert all(row["ended_at"] is not None for row in attempts_after)
            assert client.portal.call(
                _range_states, [row["range_id"] for row in attempts_after]
            ) == ranges_before
            assert client.portal.call(_job_attempt, job_id) == attempt_before
            assert client.portal.call(
                _other_connection, other_worker_id, other_incarnation
            ) == other_before
            assert erase_counts == {
                session_id: int(session_id in update_order) for session_id in opened
            }
            if boundary_kind == "journal":
                old_after = client.portal.call(
                    _old_incarnation_history_row, worker_id, old_journal["incarnation"])
                assert old_after == old_journal

            client.portal.call(worker_authority.close_worker, worker_id, incarnation)
            assert client.portal.call(_connection, worker_id, incarnation) == counters_after
            assert client.portal.call(
                _range_states, [row["range_id"] for row in attempts_after]
            ) == ranges_before
            if boundary_kind == "journal":
                assert client.portal.call(
                    _old_incarnation_history_row, worker_id, old_journal["incarnation"]
                ) == old_journal
