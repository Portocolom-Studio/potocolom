import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from app import command_store, db, deletion, jobs, realtime, sessions as account_sessions, worker_authority
from app.tables import User
from app.settings import get_settings
from test_fleet_protocol6 import (
    hello, manifest, protocol6_client, request_work_grant, root_keys, seed_queued_job,
)

from test_realtime import canvas_frame
from test_receipt_recovery import job_failed_from_dispatch
from test_v6_sessions import (
    command_ack, open_live_session, session_closed_message, session_ready_message,
)
from test_totp_flow import _make as make_account_user, accounts

__all__ = ["accounts"]


@pytest.mark.db
def test_session_revoke_erases_only_bound_realtime_commands(accounts, monkeypatch):
    worker_id = f"s1-revoke-{uuid.uuid4()}"
    model_id = f"s1-revoke-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        async def mint_auth_session():
            async with db.session_factory() as session:
                user = (await session.execute(
                    select(User).where(User.email == db.LOCAL_USER_EMAIL)
                )).scalar_one()
            issued = await account_sessions.mint(user, remember_me=False)
            resolved = await account_sessions.resolve(issued.token)
            return issued, resolved.session.id

        issued, auth_session_id = client.portal.call(mint_auth_session)
        session_cookie, csrf_cookie = account_sessions.cookie_names(
            get_settings().public_url
        )
        client.cookies.set(session_cookie, issued.token)
        client.cookies.set(csrf_cookie, issued.csrf)
        worker_ws, worker, grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({
                "type": "update_params",
                "params": {"prompt": "new prompt"},
            })
            update_command = worker_ws.receive_json()
            assert update_command["type"] == "update_session"
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            response = client.post(
                "/api/v1/auth/logout",
                headers={"origin": get_settings().public_url,
                         "x-csrf-token": issued.csrf},
            )
            assert response.status_code == 204

            async def rows():
                async with db.session_factory() as session:
                    return list((await session.execute(
                        text("SELECT kind, state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "ORDER BY command_sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )).mappings().all())

            stored = client.portal.call(rows)
            by_kind = {row["kind"]: row for row in stored}
            assert by_kind["update_session"]["state"] == "erased"
            assert by_kind["update_session"]["encrypted_body"] is None
            assert by_kind["dispatch_job"]["state"] == "pending"
            assert by_kind["dispatch_job"]["encrypted_body"] is not None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_password_reset_erases_account_commands(accounts, monkeypatch):
    from app import recovery

    email = f"s1-reset-{uuid.uuid4()}@example.test"
    worker_id = f"s1-reset-{uuid.uuid4()}"
    model_id = f"s1-reset-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, email)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            assert update_command["type"] == "update_session"
            reset_token = client.portal.call(recovery.mint_reset, email)
            response = client.post(
                "/api/v1/auth/reset/complete",
                headers={"origin": get_settings().public_url},
                json={"token": reset_token, "password": "a-brand-new-long-enough-password"},
            )
            assert response.status_code == 204

            async def inspect():
                async with db.session_factory() as session:
                    command = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": update_command["command_sequence"]},
                    )).mappings().one()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    return command["state"], command["encrypted_body"], closed_at

            state, body, closed_at = client.portal.call(inspect)
            assert state == "erased" and body is None and closed_at is not None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_credential_rotation_keeps_current_realtime_and_job_commands(accounts, monkeypatch):
    email = f"s1-rotate-{uuid.uuid4()}@example.test"
    worker_id = f"s1-rotate-{uuid.uuid4()}"
    model_id = f"s1-rotate-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, email)
        issued = client.portal.call(account_sessions.mint, user, False)
        other = client.portal.call(account_sessions.mint, user, False)
        resolved = client.portal.call(account_sessions.resolve, issued.token)
        keep_id = resolved.session.id
        other_id = client.portal.call(account_sessions.resolve, other.token).session.id
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, _open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        job_id = None
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            assert update_command["type"] == "update_session"
            job_id = client.portal.call(seed_queued_job, model_id)

            async def assign_job_owner():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
                        {"user_id": user.id, "job_id": job_id},
                    )
                    await session.commit()

            client.portal.call(assign_job_owner)
            assert client.portal.call(jobs.dispatch, job_id) is True

            async def rotate():
                async with db.session_factory() as session:
                    async with session.begin():
                        return await account_sessions.rotate_and_revoke_others(
                            session, resolved
                        )

            replacement = client.portal.call(rotate)

            async def inspect():
                async with db.session_factory() as session:
                    auth = (await session.execute(
                        text("SELECT id, revoked_at, token_hash FROM sessions "
                             "WHERE user_id = :user_id ORDER BY id"),
                        {"user_id": user.id},
                    )).mappings().all()
                    commands = (await session.execute(
                        text("SELECT command_sequence, kind, state, encrypted_body "
                             "FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation ORDER BY command_sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )).mappings().all()
                    return auth, commands

            auth, commands = client.portal.call(inspect)
            by_session = {row["id"]: row for row in auth}
            by_kind = {row["kind"]: row for row in commands}
            assert set(by_session) == {keep_id, other_id}
            assert by_session[keep_id]["revoked_at"] is None
            assert by_session[keep_id]["token_hash"] != account_sessions.token_hash(issued.token)
            assert by_session[other_id]["revoked_at"] is not None
            assert replacement.token
            assert by_kind["update_session"]["state"] == "pending"
            assert by_kind["update_session"]["encrypted_body"] is not None
            assert by_kind["dispatch_job"]["state"] == "pending"
            assert by_kind["dispatch_job"]["encrypted_body"] is not None
            assert by_kind["update_session"]["command_sequence"] < by_kind["dispatch_job"]["command_sequence"]
        finally:
            if job_id is not None:
                async def restore_job_owner():
                    async with db.session_factory() as session:
                        await session.execute(
                            text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
                            {"user_id": db.local_user_id, "job_id": job_id},
                        )
                        await session.commit()

                client.portal.call(restore_job_owner)
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_due_account_purge_erases_and_fences_before_delete(accounts, monkeypatch):
    email = f"s1-purge-{uuid.uuid4()}@example.test"
    worker_id = f"s1-purge-{uuid.uuid4()}"
    model_id = f"s1-purge-model-{uuid.uuid4()}"

    async def discard_keys(_keys, *, what):
        return None

    monkeypatch.setattr(jobs, "purge_keys", discard_keys)
    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, email)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            cutoff = datetime.now(timezone.utc) - timedelta(days=deletion.RESTORE_WINDOW_DAYS)

            async def mark_due():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE users SET state = 'deletion_pending', "
                             "deletion_requested_at = :cutoff WHERE id = :user_id"),
                        {"cutoff": cutoff - timedelta(seconds=1), "user_id": user.id},
                    )
                    await session.commit()

            client.portal.call(mark_due)
            client.portal.call(deletion._purge, user.id, cutoff)

            async def inspect():
                async with db.session_factory() as session:
                    command = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": update_command["command_sequence"]},
                    )).mappings().one()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    user_exists = await session.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM users WHERE id = :user_id)"),
                        {"user_id": user.id},
                    )
                    return command["state"], command["encrypted_body"], closed_at, user_exists

            state, body, closed_at, user_exists = client.portal.call(inspect)
            assert state == "erased" and body is None and closed_at is not None
            assert not user_exists
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_operator_clear_factor_erases_live_realtime_commands(accounts, monkeypatch):
    from app import factors, keyring, operator
    from app.tables import AuthFactor

    root_keys(monkeypatch)
    email = f"s1-factor-{uuid.uuid4()}@example.test"
    worker_id = f"s1-factor-{uuid.uuid4()}"
    model_id = f"s1-factor-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, email)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            ring = keyring.get_key_ring()

            async def add_factor():
                async with db.session_factory() as session:
                    session.add(AuthFactor(
                        user_id=user.id,
                        kind="totp",
                        secret_ciphertext=ring.encrypt(factors.TOTP_PURPOSE, b"S" * 32,
                                                       user.id.bytes),
                        key_version=ring.active_version,
                        confirmed_at=datetime.now(timezone.utc),
                    ))
                    await session.commit()

            client.portal.call(add_factor)
            assert client.portal.call(operator.clear_factor, email) is True

            async def inspect():
                async with db.session_factory() as session:
                    command = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": update_command["command_sequence"]},
                    )).mappings().one()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    return command["state"], command["encrypted_body"], closed_at

            state, body, closed_at = client.portal.call(inspect)
            assert state == "erased" and body is None and closed_at is not None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_operator_collapse_erases_original_account_work_before_remap(accounts, monkeypatch):
    from app import operator

    root_keys(monkeypatch)
    email = f"s1-collapse-{uuid.uuid4()}@example.test"
    worker_id = f"s1-collapse-{uuid.uuid4()}"
    model_id = f"s1-collapse-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        user = client.portal.call(make_account_user, email)
        issued = client.portal.call(account_sessions.mint, user, False)
        cookie, csrf = account_sessions.cookie_names(get_settings().public_url)
        client.cookies.set(cookie, issued.token)
        client.cookies.set(csrf, issued.csrf)
        worker_ws, worker, _grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            assert worker_ws.receive_json()["type"] == "update_session"
            job_id = client.portal.call(seed_queued_job, model_id)

            async def assign_job_owner():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE jobs SET user_id = :user_id WHERE id = :job_id"),
                        {"user_id": user.id, "job_id": job_id},
                    )
                    await session.commit()

            client.portal.call(assign_job_owner)
            assert client.portal.call(jobs.dispatch, job_id) is True
            client.portal.call(operator.collapse, operator.COLLAPSE_PHRASE)

            async def inspect():
                async with db.session_factory() as session:
                    commands = (await session.execute(
                        text("SELECT state, encrypted_body, account_user_id, kind, subject_id "
                             "FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation ORDER BY command_sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )).mappings().all()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    session_count = await session.scalar(text("SELECT count(*) FROM sessions"))
                    user_exists = await session.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM users WHERE id = :user_id)"),
                        {"user_id": user.id},
                    )
                    remapped_owner = await session.scalar(
                        text("SELECT user_id FROM jobs WHERE id = :job_id"),
                        {"job_id": job_id},
                    )
                    return commands, closed_at, session_count, user_exists, remapped_owner

            commands, closed_at, session_count, user_exists, remapped_owner = client.portal.call(inspect)
            by_kind = {command["kind"]: command for command in commands}
            assert by_kind["update_session"]["state"] == "erased"
            assert by_kind["update_session"]["encrypted_body"] is None
            assert by_kind["dispatch_job"]["state"] == "erased"
            assert by_kind["dispatch_job"]["encrypted_body"] is None
            assert by_kind["update_session"]["account_user_id"] == user.id
            assert by_kind["dispatch_job"]["account_user_id"] == user.id
            assert by_kind["dispatch_job"]["subject_id"] == job_id
            assert closed_at is not None and session_count == 0 and not user_exists
            assert remapped_owner == db.local_user_id
        finally:
            async def restore_accounts_mode():
                async with db.session_factory() as session:
                    await session.execute(
                        text("UPDATE installation_auth_state SET auth_mode = 'accounts', "
                             "root_key_version = 1 WHERE id = 1")
                    )
                    await session.commit()

            client.portal.call(restore_accounts_mode)
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_job_cancel_erases_dispatch_without_allocating_a_cancel_command(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-cancel-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-cancel-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            dispatch = worker.receive_json()

            assert client.portal.call(jobs.cancel, job_id) is True

            async def command_rows():
                async with db.session_factory() as session:
                    rows = (await session.execute(
                        text("SELECT kind, command_sequence, state, encrypted_body "
                             "FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation ORDER BY command_sequence"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().all()
                    next_sequence = await session.scalar(
                        text("SELECT next_command_sequence FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return rows, next_sequence

            rows, next_sequence = client.portal.call(command_rows)
            assert dispatch["type"] == "dispatch_job"
            assert [(row["kind"], row["command_sequence"], row["state"]) for row in rows] == [
                ("dispatch_job", dispatch["command_sequence"], "erased")
            ]
            assert rows[0]["encrypted_body"] is None
            assert next_sequence == dispatch["command_sequence"] + 1


@pytest.mark.db
def test_acknowledged_dispatch_cancel_fences_its_worker(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-cancel-acked-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-cancel-acked-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            dispatch = worker.receive_json()
            worker.send_json({
                **{key: dispatch[key] for key in (
                    "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
                    "lease_expires_at", "grant_nonce", "command_sequence", "command_id",
                    "body_hash", "job_id", "dispatch_sequence",
                )},
                "type": "command_ack",
                "status": "accepted",
            })

            async def wait_for_ack():
                for _ in range(100):
                    async with db.session_factory() as session:
                        floor = await session.scalar(
                            text("SELECT acknowledged_floor FROM worker_connections "
                                 "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                            {"worker_id": worker_id, "incarnation": incarnation},
                        )
                    if floor is not None and floor >= dispatch["command_sequence"]:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("worker ACK did not advance the durable floor")

            client.portal.call(wait_for_ack)
            assert client.portal.call(jobs.cancel, job_id) is True

            async def fenced():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text("SELECT closed_at FROM worker_connections WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )).mappings().one()
                    next_sequence = await session.scalar(
                        text("SELECT next_command_sequence FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    commands = await session.scalar(
                        text("SELECT count(*) FROM worker_commands WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return row["closed_at"], next_sequence, commands

            closed_at, next_sequence, command_count = client.portal.call(fenced)
            assert closed_at is not None
            assert next_sequence == dispatch["command_sequence"] + 1
            assert command_count == 0


@pytest.mark.db
def test_idle_release_closes_attempt_and_keeps_browser_session_resumable(monkeypatch):
    root_keys(monkeypatch)
    monkeypatch.setattr(realtime, "IDLE_RELEASE_SECONDS", 0)
    worker_id = f"s1-idle-{uuid.uuid4()}"
    model_id = f"s1-idle-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        worker_ws, worker, grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            session_id = uuid.UUID(open_command["session_id"])
            client.portal.call(realtime.release_idle_sessions)
            close_command = worker_ws.receive_json()
            assert close_command["type"] == "close_session"
            assert close_command["control_generation"] == open_command["control_generation"]

            async def idle_state():
                async with db.session_factory() as session:
                    return (await session.execute(
                        text("SELECT state, control_generation FROM realtime_sessions "
                             "WHERE id = :id"),
                        {"id": session_id},
                    )).mappings().one()

            assert client.portal.call(idle_state)["state"] == "idle"
            worker_ws.send_json(session_closed_message(
                worker, open_command, grant["grant_nonce"]
            ))
            report_ack = worker_ws.receive_json()
            assert report_ack["type"] == "checkpoint_ack"
            assert report_ack["status"] == "accepted"
            assert realtime.workers.get(worker_id) is worker

            async def connection_state():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )

            assert client.portal.call(connection_state) is None
            with pytest.raises(command_store.CommandRefused):
                client.portal.call(lambda: command_store.read_exact_pending(
                    worker_id,
                    worker.incarnation,
                    transport_owner_id=worker.transport_owner_id,
                ))
            worker_ws.send_json(command_ack(close_command))
            browser_ws.send_bytes(canvas_frame(session_id, b"resume", revision=1))
            reopened = worker_ws.receive_json()
            assert reopened["type"] == "open_session"
            assert reopened["control_generation"] == close_command["control_generation"] + 1
            worker_ws.send_json(command_ack(reopened))
            worker_ws.send_json(session_ready_message(worker, reopened, grant["grant_nonce"]))
            assert worker_ws.receive_bytes() == canvas_frame(
                session_id, b"resume", revision=1
            )
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)


@pytest.mark.db
def test_physical_job_completion_before_dispatch_ack_keeps_worker_open(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-report-before-ack-{uuid.uuid4()}"
    incarnation = uuid.uuid4()
    model_id = f"s1-report-before-ack-model-{uuid.uuid4()}"

    async def park_dispatch():
        return None

    monkeypatch.setattr(jobs, "dispatch_step", park_dispatch)
    with protocol6_client(worker_id) as client:
        client.portal.call(worker_authority.acquire_scheduler_lease)
        with client.websocket_connect("/api/v1/fleet") as worker:
            worker.send_json(hello(worker_id, [manifest(model_id)], incarnation=incarnation))
            worker.receive_json()
            grant = request_work_grant(worker, worker_id, incarnation)
            job_id = client.portal.call(seed_queued_job, model_id)
            assert client.portal.call(jobs.dispatch, job_id) is True
            command = worker.receive_json()
            report = job_failed_from_dispatch(command, grant["grant_nonce"])
            worker.send_json(report)
            report_ack = worker.receive_json()
            assert report_ack["type"] == "checkpoint_ack"
            assert report_ack["status"] == "accepted"

            async def command_state():
                async with db.session_factory() as session:
                    row = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": incarnation,
                         "sequence": command["command_sequence"]},
                    )).mappings().one()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )
                    return row["state"], row["encrypted_body"], closed_at

            state, body, closed_at = client.portal.call(command_state)
            assert state == "erased" and body is None and closed_at is None
            with pytest.raises(command_store.CommandRefused):
                client.portal.call(lambda: command_store.read_exact_pending(
                    worker_id,
                    incarnation,
                    transport_owner_id=worker_authority.TRANSPORT_OWNER_ID,
                ))

            worker.send_json(command_ack(command))

            async def acknowledged():
                async with db.session_factory() as session:
                    return await session.scalar(
                        text("SELECT acknowledged_floor FROM worker_connections "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": incarnation},
                    )

            for _ in range(100):
                if client.portal.call(acknowledged) >= command["command_sequence"]:
                    break
                client.portal.call(asyncio.sleep, 0.01)
            else:
                raise AssertionError("dispatch ACK did not consume the retired head")
            assert client.portal.call(acknowledged) == command["command_sequence"]


@pytest.mark.db
def test_deletion_request_erasure_rolls_back_with_its_caller_transaction(monkeypatch):
    root_keys(monkeypatch)
    worker_id = f"s1-delete-rollback-{uuid.uuid4()}"
    model_id = f"s1-delete-rollback-model-{uuid.uuid4()}"

    with protocol6_client(worker_id) as client:
        worker_ws, worker, grant, open_command, browser_ws, browser, fleet = (
            open_live_session(client, worker_id, model_id)
        )
        try:
            browser_ws.send_json({"type": "update_params", "params": {"prompt": "pending"}})
            update_command = worker_ws.receive_json()
            assert update_command["type"] == "update_session"
            user_id = client.portal.call(
                lambda: realtime.sessions[uuid.UUID(open_command["session_id"])].user_id
            )

            async def rollback_request():
                async with db.session_factory() as session:
                    try:
                        async with session.begin():
                            assert await deletion.request(session, user_id)
                            raise RuntimeError("roll back caller transaction")
                    except RuntimeError as error:
                        if str(error) != "roll back caller transaction":
                            raise

            client.portal.call(rollback_request)

            async def inspect():
                async with db.session_factory() as session:
                    user_state = await session.scalar(
                        text("SELECT state FROM users WHERE id = :id"), {"id": user_id}
                    )
                    command = (await session.execute(
                        text("SELECT state, encrypted_body FROM worker_commands "
                             "WHERE worker_id = :worker_id AND incarnation = :incarnation "
                             "AND command_sequence = :sequence"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation,
                         "sequence": update_command["command_sequence"]},
                    )).mappings().one()
                    closed_at = await session.scalar(
                        text("SELECT closed_at FROM worker_connections WHERE worker_id = :worker_id "
                             "AND incarnation = :incarnation"),
                        {"worker_id": worker_id, "incarnation": worker.incarnation},
                    )
                    return user_state, command["state"], command["encrypted_body"], closed_at

            user_state, command_state, command_body, closed_at = client.portal.call(inspect)
            assert user_state == "active"
            assert command_state == "pending" and command_body is not None
            assert closed_at is None
        finally:
            browser.__exit__(None, None, None)
            fleet.__exit__(None, None, None)
