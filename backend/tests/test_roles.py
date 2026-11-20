import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, sessions
from app.main import app
from app.tables import Job, Model, User
from tests.test_account_deletion import library
from tests.test_invitations import _account, _admin, _sign_in, accounts

__all__ = ["accounts", "library"]


async def _role_of(email: str) -> str:
    async with db.session_factory() as session:
        return (await session.execute(select(User.role).where(User.email == email))).scalar_one()


def _change(client, headers, user_id, **body):
    return client.post(f"/api/v1/users/{user_id}/role", headers=headers, json=body)


@pytest.mark.db
def test_an_administrator_promotes_a_member_who_has_verified_mail(accounts):
    target = _account(accounts, "promote@example.com", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.portal.call(_role_of, "promote@example.com") == "admin"


@pytest.mark.db
def test_promotion_without_verified_mail_needs_an_explicit_attestation(accounts):
    """A no-mail install cannot prove the address, so the administrator says
    on the record that they know who this is."""
    target = _account(accounts, "unverified@example.com", mail_verified=False)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        refused = _change(client, headers, target.id, role="admin")
        assert refused.status_code == 409
        assert _change(client, headers, target.id, role="admin",
                       attested=True).status_code == 204
        assert client.portal.call(_role_of, "unverified@example.com") == "admin"


@pytest.mark.db
def test_the_attestation_is_recorded_against_the_account_it_promoted(accounts):
    target = _account(accounts, "attested@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin", attested=True).status_code == 204
        rows = client.portal.call(_audit)
    # The role check records the attempt before it runs; the route records the
    # outcome, because only it knows who was changed and to what.
    assert any(row["action"] == "POST /api/v1/users/{user_id}/role" for row in rows)
    promoted = [row for row in rows if row["action"] == "user.role"]
    assert len(promoted) == 1
    assert promoted[0]["target_user_id"] == str(target.id)
    # The row has to say the promotion rested on an attestation, not on a
    # verified address, or a later reader cannot tell them apart.
    assert promoted[0]["object_ids"] == ["admin", "attested"]


async def _audit():
    from app import audit
    return await audit.search()


@pytest.mark.db
def test_a_demotion_needs_no_attestation(accounts):
    """Attestation is evidence for handing power over, not for taking it back."""
    target = _account(accounts, "demote@example.com", role="admin")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="user").status_code == 204
        assert client.portal.call(_role_of, "demote@example.com") == "user"


@pytest.mark.db
def test_promotion_requires_recent_authentication(accounts):
    target = _account(accounts, "stale@example.com", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)

        async def go_stale():
            from datetime import datetime, timedelta, timezone

            from sqlalchemy import text
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE sessions SET recent_auth_at = :past"),
                    {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                     - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(go_stale)
        assert _change(client, headers, target.id, role="admin").status_code == 403


@pytest.mark.db
def test_an_administrator_cannot_change_their_own_role(accounts):
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        me = client.get("/api/v1/account").json()
        assert _change(client, headers, me["id"], role="user").status_code == 403
        assert client.get("/api/v1/account").json()["role"] == "admin"


@pytest.mark.db
def test_a_peer_administrator_can_be_demoted_while_another_remains(accounts):
    other = _account(accounts, "second@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client, email="first@example.com")
        assert _change(client, headers, other.id, role="user").status_code == 204
        assert client.portal.call(_role_of, "second@example.com") == "user"


@pytest.mark.db
def test_the_last_administrator_cannot_be_demoted(accounts):
    """Reached through the count rather than through self-protection: an
    install with no administrator can only be recovered offline.

    Self-protection covers this today, because to be the last administrator is
    to be the caller. The count is what keeps it true once account state
    changes can retire an administrator without demoting them.
    """
    from app.roles import _remaining_administrators

    solo = _account(accounts, "solo@example.com", role="admin")

    async def remaining():
        async with db.session_factory() as session:
            return await _remaining_administrators(session, solo.id)

    with TestClient(app) as client:
        _admin(accounts, client, email="first@example.com")
        assert client.portal.call(remaining) >= 1
        # The implicit local administrator is still one until someone claims
        # the install, so retire both to reach the case that matters.
        client.portal.call(_demote, "first@example.com")
        client.portal.call(_demote, db.LOCAL_USER_EMAIL)
        assert client.portal.call(remaining) == 0


async def _demote(email: str) -> None:
    from sqlalchemy import text

    async with db.session_factory() as session:
        await session.execute(text("UPDATE users SET role = 'user' WHERE email = :e"),
                              {"e": email})
        await session.commit()


async def _user_id(email: str):
    async with db.session_factory() as session:
        return str((await session.execute(
            select(User.id).where(User.email == email))).scalar_one())


@pytest.mark.db
def test_a_role_change_ends_every_session_the_account_held(accounts):
    """The old session carries the old role's session shape and the old
    authority, so it must not survive the change."""
    target = _account(accounts, "rotated@example.com", mail_verified=True)
    with TestClient(app) as client:
        theirs = client.portal.call(sessions.mint, target, False)
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {theirs.token}"}).status_code == 401


@pytest.mark.db
def test_only_an_administrator_may_change_a_role(accounts):
    target = _account(accounts, "target@example.com")
    _account(accounts, "nobody@example.com")
    with TestClient(app) as client:
        headers = _sign_in(client, "nobody@example.com")
        assert _change(client, headers, target.id, role="admin").status_code == 403


@pytest.mark.db
def test_an_unknown_role_is_refused(accounts):
    target = _account(accounts, "weird@example.com")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="superuser").status_code == 422


@pytest.mark.db
def test_changing_the_role_of_an_account_that_does_not_exist(accounts):
    import uuid as _uuid

    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, _uuid.uuid4(), role="user").status_code == 404


@pytest.mark.db
def test_setting_the_same_role_again_still_ends_the_sessions(accounts):
    """The role change commits before the sessions are revoked. If that second
    step fails, the operator retries, and a retry that short-circuits on
    "already that role" would leave the old sessions alive for good."""
    target = _account(accounts, "retry@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        theirs = client.portal.call(sessions.mint, target, False)
        headers = _admin(accounts, client, email="first@example.com")
        assert _change(client, headers, target.id, role="admin").status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {theirs.token}"}).status_code == 401


@pytest.mark.db
def test_a_denied_privileged_attempt_is_recorded_at_high_severity(accounts):
    """Someone with an account reaching for privileged work they do not have
    is exactly the signal the audit exists to carry."""
    target = _account(accounts, "prize@example.com")
    _account(accounts, "climber@example.com")
    with TestClient(app) as client:
        headers = _sign_in(client, "climber@example.com")
        assert _change(client, headers, target.id, role="admin").status_code == 403
        rows = client.portal.call(_audit)
    denied = [row for row in rows if row["action"] == "POST /api/v1/users/{user_id}/role"]
    assert len(denied) == 1
    assert denied[0]["severity"] == "high"


@pytest.mark.db
def test_a_promotion_on_verified_mail_is_not_marked_attested(accounts):
    target = _account(accounts, "verified@example.com", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, target.id, role="admin").status_code == 204
        rows = client.portal.call(_audit)
    promoted = [row for row in rows if row["action"] == "user.role"]
    assert promoted[0]["object_ids"] == ["admin"]


async def _seed_live(owner_id: uuid.UUID, other_id: uuid.UUID):
    async with db.session_factory() as session:
        if await session.get(Model, "sd-test") is None:
            session.add(Model(
                id="sd-test", name="SD Test", capabilities=["text_to_image"],
                parameters_schema={"type": "object"}, min_vram_gb=0,
            ))
            await session.flush()
        queued, running, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for job_id, user_id, state in (
            (queued, owner_id, "queued"),
            (running, owner_id, "running"),
            (other, other_id, "queued"),
        ):
            session.add(Job(
                id=job_id, user_id=user_id, model_id="sd-test",
                params={"prompt": state}, state=state, attempt=1,
            ))
        await session.commit()
    return queued, running, other


@pytest.mark.db
def test_demotion_to_viewer_cancels_owned_work_before_the_sockets_close(library, monkeypatch):
    """The role and the cancellations commit together. A close that fails
    after that commit still finds both jobs cancelled, and nobody else's."""
    seen: dict = {}

    async def spy(user_id):
        async with db.session_factory() as session:
            seen["role"] = await session.scalar(
                text("SELECT role FROM users WHERE id = :id"), {"id": user_id})
            seen["jobs"] = list((await session.execute(
                text("SELECT state, failure_reason FROM jobs WHERE user_id = :id"),
                {"id": user_id},
            )).all())
            seen["other"] = await session.scalar(
                text("SELECT state FROM jobs WHERE id = :id"), {"id": seen["other_id"]})
        raise RuntimeError("sockets")

    monkeypatch.setattr(sessions, "close_sockets", spy)
    with TestClient(app) as client:
        owner = client.portal.call(_make_member, "demoted-owner@example.com")
        other = client.portal.call(_make_member, "demoted-other@example.com")
        _ids = client.portal.call(_seed_live, owner, other)
        seen["other_id"] = _ids[2]
        headers = _admin(accounts, client)
        with pytest.raises(RuntimeError, match="sockets"):
            _change(client, headers, owner, role="viewer")
    assert seen["role"] == "viewer"
    assert len(seen["jobs"]) == 2
    assert {row[0] for row in seen["jobs"]} == {"cancelled"}
    assert {row[1] for row in seen["jobs"]} == {"owner can no longer generate"}
    assert seen["other"] == "queued"


async def _make_member(email: str) -> uuid.UUID:
    from tests.test_totp_flow import _make
    user = await _make(email)
    return user.id


@pytest.mark.db
def test_promotion_logout_and_repeating_a_role_keep_queued_work(library):
    """A job belongs to the account. Logout, a promotion between roles that
    can both generate, and setting the same role again leave it queued."""
    with TestClient(app) as client:
        owner = client.portal.call(_make_member, "kept-owner@example.com")
        other = client.portal.call(_make_member, "kept-other@example.com")
        queued, _running, _other = client.portal.call(_seed_live, owner, other)
        member = _sign_in(client, "kept-owner@example.com")
        assert client.post("/api/v1/auth/logout", headers=member).status_code == 204
        assert client.portal.call(_one_state, queued) == "queued"

        headers = _admin(accounts, client, email="kept-admin@example.com")
        assert _change(client, headers, owner, role="admin", attested=True).status_code == 204
        assert client.portal.call(_one_state, queued) == "queued"
        assert _change(client, headers, owner, role="admin", attested=True).status_code == 204
        assert client.portal.call(_one_state, queued) == "queued"
        assert _change(client, headers, owner, role="viewer").status_code == 204
        assert client.portal.call(_one_state, queued) == "cancelled"
        assert _change(client, headers, owner, role="user").status_code == 204
        assert client.portal.call(_one_state, queued) == "cancelled"


async def _one_state(job_id: uuid.UUID) -> str:
    async with db.session_factory() as session:
        return await session.scalar(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})


def _subject(client, headers, delegate_id, subject_id):
    return client.put(
        f"/api/v1/users/{delegate_id}/billing-subject",
        headers=headers,
        json={"subject_user_id": str(subject_id)},
    )


async def _binding(delegate_id):
    from app.tables import BillingBinding
    async with db.session_factory() as session:
        row = await session.get(BillingBinding, delegate_id)
        if row is None:
            return None
        return (str(row.subject_user_id), None if row.granted_by is None else str(row.granted_by))


@pytest.mark.db
def test_a_billing_account_reads_only_its_bound_subject(library):
    """Delegate D, subject A, unrelated B. Scoped usage is the other test.
    Here: no election of B, no prompts or assets, no generation, no drawing,
    no invitation, no role change, no cancelling A's work, no admin read.
    """
    from app import realtime
    from tests.test_account_deletion import _owned_work

    delegate = _account(library, "delegate@example.com", role="user")
    subject_a = _account(library, "subject-a@example.com", role="user")
    other = _account(library, "subject-b@example.com", role="user")
    with TestClient(app) as client:
        headers = _admin(library, client)
        admin = client.get("/api/v1/account").json()
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        granted = _subject(client, headers, delegate.id, subject_a.id)
        assert granted.status_code == 200, granted.text
        assert granted.json()["delegate_user_id"] == str(delegate.id)
        assert granted.json()["subject_user_id"] == str(subject_a.id)
        assert granted.json()["granted_by"] == admin["id"]
        replaced = _subject(client, headers, delegate.id, other.id)
        assert replaced.status_code == 200
        assert replaced.json()["subject_user_id"] == str(other.id)
        # B was a replacement of the binding, not a second one D chose alone.
        assert client.portal.call(_binding, delegate.id)[0] == str(other.id)
        inspected = client.get(f"/api/v1/users/{delegate.id}/billing-subject")
        assert inspected.status_code == 200
        assert inspected.json()["subject_user_id"] == str(other.id)
        rows = client.portal.call(_audit)
        actions = [row["action"] for row in rows]
        assert "billing.granted" in actions
        assert "billing.replaced" in actions
        assert any(row["action"] == "user.read" and row["target_user_id"] == str(delegate.id)
                   and row["actor_user_id"] == admin["id"] for row in rows)
        for row in rows:
            if row["action"] in ("billing.granted", "billing.replaced"):
                assert row["severity"] == "high"

        own = client.portal.call(_owned_work, delegate.id)
        theirs = client.portal.call(_owned_work, subject_a.id)
        delegate_headers = _sign_in(client, "delegate@example.com")
        listed = client.get("/api/v1/generations")
        assert listed.status_code == 200
        assert str(own.job_id) in {row["id"] for row in listed.json()}
        assert str(theirs.job_id) not in {row["id"] for row in listed.json()}
        own_asset = client.get(f"/api/v1/assets/{own.id}")
        assert own_asset.status_code == 404
        assert own_asset.json()["detail"] == "no such file"
        foreign_asset = client.get(f"/api/v1/assets/{theirs.id}")
        assert foreign_asset.status_code == 404
        assert foreign_asset.json()["detail"] == "no such asset"
        foreign_job = client.get(f"/api/v1/generations/{theirs.job_id}")
        assert foreign_job.status_code == 404
        cancelled = client.post(f"/api/v1/generations/{theirs.job_id}/cancel",
                                headers=delegate_headers)
        assert cancelled.status_code == 404
        assert cancelled.json()["detail"] == "no such generation"

        generated = client.post("/api/v1/generations", headers=delegate_headers, json={})
        assert generated.status_code == 403
        assert generated.json()["detail"] == "insufficient role"
        invited = client.post("/api/v1/invitations", headers=delegate_headers,
                              json={"email": "nobody@example.com", "role": "user"})
        assert invited.status_code == 403
        assert invited.json()["detail"] == "insufficient role"
        role = _change(client, delegate_headers, other.id, role="viewer")
        assert role.status_code == 403
        assert role.json()["detail"] == "insufficient role"
        directory = client.get("/api/v1/users")
        assert directory.status_code == 403
        assert directory.json()["detail"] == "insufficient role"
        gpu = client.get("/api/v1/studio/gpu")
        assert gpu.status_code == 403
        assert gpu.json()["detail"] == "insufficient role"
        with client.websocket_connect("/api/v1/realtime") as ws:
            message = ws.receive_json()
        assert message["code"] == realtime.CLOSE_FORBIDDEN

        # The delegate cannot move its own binding. A stale administrator
        # cannot grant one, and neither endpoint may be the actor.
        own_put = _subject(client, delegate_headers, delegate.id, subject_a.id)
        assert own_put.status_code == 403
        assert own_put.json()["detail"] == "insufficient role"
        own_delete = client.delete(f"/api/v1/users/{delegate.id}/billing-subject",
                                   headers=delegate_headers)
        assert own_delete.status_code == 403
        assert own_delete.json()["detail"] == "insufficient role"


@pytest.mark.db
def test_binding_writes_need_recent_auth_and_a_cookie_origin(accounts):
    delegate = _account(accounts, "cookie-delegate@example.com", role="user")
    subject = _account(accounts, "cookie-subject@example.com", role="user")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        foreign = client.put(
            f"/api/v1/users/{delegate.id}/billing-subject",
            headers={"Origin": "http://evil.example"},
            json={"subject_user_id": str(subject.id)},
        )
        assert foreign.status_code == 403
        assert foreign.json()["detail"] == "origin not allowed"
        origin_only = client.put(
            f"/api/v1/users/{delegate.id}/billing-subject",
            headers={"Origin": "http://localhost:8000"},
            json={"subject_user_id": str(subject.id)},
        )
        assert origin_only.status_code == 403
        assert origin_only.json()["detail"] == "missing or mismatched CSRF token"
        self_subject = _subject(client, headers, delegate.id, client.get("/api/v1/account").json()["id"])
        assert self_subject.status_code == 403
        assert self_subject.json()["detail"] == "an administrator cannot bind their own account"
        self_delegate = client.put(
            f"/api/v1/users/{client.get('/api/v1/account').json()['id']}/billing-subject",
            headers=headers,
            json={"subject_user_id": str(subject.id)},
        )
        assert self_delegate.status_code == 403
        assert _subject(client, headers, delegate.id, subject.id).status_code == 200

        async def go_stale():
            from datetime import datetime, timedelta, timezone
            from sqlalchemy import text
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE sessions SET recent_auth_at = :past"),
                    {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                     - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(go_stale)
        stale = _subject(client, headers, delegate.id, subject.id)
        assert stale.status_code == 403
        assert stale.json()["detail"] == "recent authentication required"
        stale_delete = client.delete(
            f"/api/v1/users/{delegate.id}/billing-subject", headers=headers)
        assert stale_delete.status_code == 403
        assert stale_delete.json()["detail"] == "recent authentication required"
        assert client.portal.call(_binding, delegate.id) is not None


@pytest.mark.db
def test_a_billing_subject_must_be_a_member_or_administrator(accounts):
    delegate = _account(accounts, "finance@example.com", role="user")
    viewer = _account(accounts, "reader@example.com", role="viewer")
    second = _account(accounts, "second-finance@example.com", role="user")
    subject = _account(accounts, "payer@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        assert _change(client, headers, second.id, role="billing").status_code == 204
        refused = _subject(client, headers, delegate.id, viewer.id)
        assert refused.status_code == 409
        assert client.portal.call(_binding, delegate.id) is None
        billing_subject = _subject(client, headers, delegate.id, second.id)
        assert billing_subject.status_code == 409
        granted = _subject(client, headers, delegate.id, subject.id)
        assert granted.status_code == 200
        taken = _subject(client, headers, second.id, subject.id)
        assert taken.status_code == 409
        assert taken.json()["detail"] == "that account already has a billing account"


@pytest.mark.db
def test_a_role_round_trip_does_not_bring_the_binding_back(accounts):
    delegate = _account(accounts, "round@example.com", role="user")
    subject = _account(accounts, "round-subject@example.com", role="user")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        assert _subject(client, headers, delegate.id, subject.id).status_code == 200
        assert _change(client, headers, delegate.id, role="user").status_code == 204
        assert client.portal.call(_binding, delegate.id) is None
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        assert client.portal.call(_binding, delegate.id) is None
        assert _subject(client, headers, delegate.id, subject.id).status_code == 200
        assert _change(client, headers, subject.id, role="viewer").status_code == 204
        assert client.portal.call(_binding, delegate.id) is None
        assert _change(client, headers, subject.id, role="user").status_code == 204
        assert client.portal.call(_binding, delegate.id) is None


@pytest.mark.db
def test_a_grant_waits_out_a_demotion_and_then_refuses(accounts):
    import asyncio
    import threading
    import time

    from sqlalchemy import text

    from app.account_lock import hold_the_account

    delegate = _account(accounts, "race-delegate@example.com", role="user")
    subject = _account(accounts, "race-subject@example.com", role="user")
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        held = asyncio.Event()
        release = asyncio.Event()
        pid = {}

        async def demote_while_holding() -> None:
            async with db.session_factory() as session:
                async with session.begin():
                    await hold_the_account(session, delegate.id, wait=True)
                    pid["value"] = await session.scalar(text("SELECT pg_backend_pid()"))
                    held.set()
                    await release.wait()
                    row = await session.get(User, delegate.id)
                    previous = row.role
                    row.role = "user"
                    from app.billing import drop_invalid_bindings
                    await drop_invalid_bindings(session, delegate.id, previous, "user")

        async def waiting() -> bool:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                async with db.session_factory() as session:
                    count = await session.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity a "
                        "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
                        "AND a.wait_event_type = 'Lock' "
                        "AND :holder = ANY(pg_blocking_pids(a.pid)) "
                        "AND a.query ILIKE '%pg_advisory_xact_lock%'"
                    ), {"holder": pid["value"]})
                if count:
                    return True
                await asyncio.sleep(0.02)
            return False

        task = client.portal.start_task_soon(demote_while_holding)
        result = {}
        thread = None

        def put() -> None:
            result["response"] = _subject(client, headers, delegate.id, subject.id)

        try:
            client.portal.call(held.wait)
            thread = threading.Thread(target=put)
            thread.start()
            assert client.portal.call(waiting) is True
        finally:
            client.portal.call(release.set)
            if thread is not None:
                thread.join(timeout=10)
            task.result(timeout=10)
        assert result["response"].status_code == 409
        assert client.portal.call(_binding, delegate.id) is None


@pytest.mark.db
def test_binding_a_billing_account_to_an_admin_grants_no_recovery(accounts):
    from app.recovery import NoSuchAdministrator, mint_admin_recovery

    delegate = _account(accounts, "recover-delegate@example.com", role="user")
    subject = _account(accounts, "recover-admin@example.com", role="admin", mail_verified=True)
    with TestClient(app) as client:
        headers = _admin(accounts, client)
        assert _change(client, headers, delegate.id, role="billing").status_code == 204
        assert _subject(client, headers, delegate.id, subject.id).status_code == 200

        async def reset_for() -> list[str]:
            from sqlalchemy import select

            from app.recovery import _deliver_reset
            from app.tables import AuthToken
            await _deliver_reset("recover-delegate@example.com")
            await _deliver_reset("recover-admin@example.com")
            async with db.session_factory() as session:
                rows = (await session.execute(
                    select(User.email)
                    .join(AuthToken, AuthToken.user_id == User.id)
                    .where(AuthToken.purpose == "reset", AuthToken.consumed_at.is_(None))
                )).scalars().all()
                return sorted(rows)

        # A billing account gets the ordinary reset token. The admin it is
        # bound to does not, and the binding does not change that.
        assert client.portal.call(reset_for) == ["recover-delegate@example.com"]
        with pytest.raises(NoSuchAdministrator):
            client.portal.call(mint_admin_recovery, "recover-delegate@example.com")


@pytest.mark.db
def test_an_administrator_cannot_move_the_binding_that_names_them(accounts):
    """DELETE already refuses the subject; a PUT to another subject is the
    same change by another route."""
    delegate = _account(accounts, "named-delegate@example.com", role="billing")
    other = _account(accounts, "named-other@example.com", role="user")
    with TestClient(app) as client:
        first = _admin(accounts, client, email="first-admin@example.com")
        named = client.portal.call(_make_admin, "named-admin@example.com")
        assert _subject(client, first, delegate.id, named).status_code == 200
        client.cookies.clear()
        own = _sign_in(client, "named-admin@example.com")
        moved = _subject(client, own, delegate.id, other.id)
        assert moved.status_code == 403
        assert client.portal.call(_binding, delegate.id)[0] == str(named)


async def _make_admin(email: str) -> uuid.UUID:
    from tests.test_totp_flow import _make
    user = await _make(email, "admin")
    return user.id
