import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, sessions
from app.main import app
from app.passwords import verify_password
from app.tables import AuthIdentity, User
from tests.test_totp_flow import (
    ORIGIN, PASSWORD, _csrf, _enrol, _login, _make, _next_code, _session_cookie, accounts,
)

__all__ = ["accounts"]

NEW = "a-brand-new-long-enough-password"


async def _identities(user_id: uuid.UUID) -> list[AuthIdentity]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthIdentity).where(AuthIdentity.user_id == user_id)
        )).scalars().all())


async def _user(email: str) -> User:
    async with db.session_factory() as session:
        return (await session.execute(
            select(User).where(User.email == email))).scalar_one()


async def _link(user_id: uuid.UUID, provider: str, subject: str) -> None:
    async with db.session_factory() as session:
        session.add(AuthIdentity(id=uuid.uuid4(), user_id=user_id, provider=provider,
                                 subject=subject))
        await session.commit()


def _change_password(client, current, new):
    return client.post("/api/v1/account/password", headers=_csrf(client),
                       json={"current_password": current, "password": new})


@pytest.mark.db
def test_changing_a_password_needs_the_current_one(accounts):
    """Recent authentication says this browser was somebody's. The current
    password says it is still theirs at this keyboard."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "changer@example.com")
        assert _login(client, "changer@example.com").status_code == 204
        assert _change_password(client, "the-wrong-password", NEW).status_code == 403
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        stored = client.portal.call(_identities,
                                    client.portal.call(_user, "changer@example.com").id)
    password = next(row for row in stored if row.provider == "password")
    assert verify_password(password.password_hash, NEW) is True
    assert verify_password(password.password_hash, PASSWORD) is False


@pytest.mark.db
def test_changing_a_password_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "stalechange@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert _change_password(client, PASSWORD, NEW).status_code == 403


@pytest.mark.db
def test_a_weak_new_password_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "weakchange@example.com")
        assert _login(client, "weakchange@example.com").status_code == 204
        assert _change_password(client, PASSWORD, "short").status_code == 400
        assert _change_password(client, PASSWORD, "passwordpassword").status_code == 400


@pytest.mark.db
def test_changing_a_password_ends_every_other_session(accounts):
    """The reason to change a password is usually that somebody else has it."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "kickother@example.com")
        elsewhere = client.portal.call(sessions.mint, user, False)
        assert _login(client, "kickother@example.com").status_code == 204
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {elsewhere.token}"}
                          ).status_code == 401
        # The browser that made the change keeps working.
        assert client.get("/api/v1/account").status_code == 200


@pytest.mark.db
def test_an_account_with_no_password_can_add_one(accounts):
    """An account that only ever signed in with a provider still needs a way
    in when the provider is unavailable."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "haspassword@example.com")
        assert _login(client, "haspassword@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        added = client.post("/api/v1/account/password", headers=_csrf(client),
                            json={"password": NEW})
        assert added.status_code == 204
        rows = client.portal.call(_identities, user.id)
    assert [row.provider for row in rows if row.provider == "password"] == ["password"]


@pytest.mark.db
def test_adding_a_password_still_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "addstale@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert client.post("/api/v1/account/password", headers=_csrf(client),
                           json={"password": NEW}).status_code == 403


def _change_email(client, address):
    return client.post("/api/v1/account/email", headers=_csrf(client),
                       json={"email": address})


@pytest.mark.db
def test_changing_the_primary_address_drops_the_assurance_with_it(accounts):
    """A provider proved the old address. It says nothing about the new one."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "assured@example.com")
        assert _login(client, "assured@example.com").status_code == 204

        async def assure():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE users SET mail_verified = true "
                                           "WHERE id = :id"), {"id": user.id})
                await session.commit()

        client.portal.call(assure)
        assert client.get("/api/v1/account").json()["mail_verified"] is True
        assert _change_email(client, "moved@example.com").status_code == 204
        me = client.get("/api/v1/account").json()
    assert me["email"] == "moved@example.com"
    assert me["mail_verified"] is False


@pytest.mark.db
def test_the_password_identity_follows_the_address(accounts):
    """Login matches on the identity subject, so leaving it behind would sign
    somebody in under an address they no longer hold."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "renamed@example.com")
        assert _login(client, "renamed@example.com").status_code == 204
        assert _change_email(client, "NewName@example.com").status_code == 204
        rows = client.portal.call(_identities, user.id)
    password = next(row for row in rows if row.provider == "password")
    assert password.subject == "newname@example.com"
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "newname@example.com").status_code == 204
        assert _login(fresh, "renamed@example.com").status_code == 401


@pytest.mark.db
def test_an_address_somebody_else_holds_is_refused(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "taken@example.com")
        client.portal.call(_make, "mover@example.com")
        assert _login(client, "mover@example.com").status_code == 204
        assert _change_email(client, "Taken@example.com").status_code == 409
        assert client.get("/api/v1/account").json()["email"] == "mover@example.com"


@pytest.mark.db
def test_changing_the_address_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "staleemail@example.com")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert _change_email(client, "other@example.com").status_code == 403


@pytest.mark.db
def test_unlinking_a_provider_needs_recent_authentication(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "unlinker@example.com")
        client.portal.call(_link, user.id, "google", "g-unlink")
        stale = client.portal.call(sessions.mint, user, False)
        client.cookies.set("__Host-potocolom_session", stale.token)
        assert client.delete("/api/v1/account/identities/google",
                             headers=_csrf(client)).status_code == 403


@pytest.mark.db
def test_unlinking_a_provider_removes_it(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "dropgoogle@example.com")
        client.portal.call(_link, user.id, "google", "g-drop")
        assert _login(client, "dropgoogle@example.com").status_code == 204
        assert client.delete("/api/v1/account/identities/google",
                             headers=_csrf(client)).status_code == 204
        rows = client.portal.call(_identities, user.id)
    assert {row.provider for row in rows} == {"password"}


@pytest.mark.db
def test_the_last_way_in_cannot_be_unlinked(accounts):
    """An account with no credential at all can only be recovered offline."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "onlygoogle@example.com")
        client.portal.call(_link, user.id, "google", "g-only")
        assert _login(client, "onlygoogle@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        refused = client.delete("/api/v1/account/identities/google", headers=_csrf(client))
        assert refused.status_code == 409
        rows = client.portal.call(_identities, user.id)
    assert {row.provider for row in rows} == {"google"}


@pytest.mark.db
def test_unlinking_ends_every_other_session(accounts):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "unlinkkick@example.com")
        client.portal.call(_link, user.id, "github", "h-kick")
        elsewhere = client.portal.call(sessions.mint, user, False)
        assert _login(client, "unlinkkick@example.com").status_code == 204
        assert client.delete("/api/v1/account/identities/github",
                             headers=_csrf(client)).status_code == 204
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {elsewhere.token}"}
                          ).status_code == 401


@pytest.mark.db
def test_a_credential_change_by_an_enrolled_account_still_passes_the_factor(accounts):
    """Recent authentication is what these routes require, and the only way to
    have it is to have passed the gate."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "gatedchange@example.com")
        assert _login(client, "gatedchange@example.com").status_code == 204
        secret, _ = _enrol(client)
    with TestClient(app, base_url=ORIGIN) as fresh:
        assert _login(fresh, "gatedchange@example.com").status_code == 200
        # Mid-challenge there is no session at all, so nothing can be changed.
        assert _change_password(fresh, PASSWORD, NEW).status_code == 401
        assert fresh.post("/api/v1/auth/totp", headers={"Origin": ORIGIN},
                          json={"code": _next_code(secret)}).status_code == 204
        assert _change_password(fresh, PASSWORD, NEW).status_code == 204


async def _live_tokens(user_id: uuid.UUID) -> list:
    from app.tables import AuthToken
    async with db.session_factory() as session:
        return list((await session.execute(
            select(AuthToken).where(AuthToken.user_id == user_id,
                                    AuthToken.consumed_at.is_(None))
        )).scalars().all())


@pytest.mark.db
def test_changing_a_credential_kills_a_reset_link_already_in_flight(accounts):
    """The reason to change an address after a mailbox is compromised is to
    end what that mailbox can still do. A link already sent is exactly that."""
    from app import recovery

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "inflight@example.com")
        token = client.portal.call(recovery.mint_reset, "inflight@example.com")
        assert _login(client, "inflight@example.com").status_code == 204
        assert _change_email(client, "safe@example.com").status_code == 204
        assert client.portal.call(_live_tokens, user.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_changing_a_password_kills_a_reset_link_too(accounts):
    from app import recovery

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "inflight2@example.com")
        token = client.portal.call(recovery.mint_reset, "inflight2@example.com")
        assert _login(client, "inflight2@example.com").status_code == 204
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        assert client.portal.call(_live_tokens, user.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_a_promotion_kills_a_reset_link_minted_before_it(accounts):
    """A reset is refused for an administrator. A link minted while the account
    was not one, and spent after it became one, is an administrator password
    from mailbox control alone."""
    from app import recovery
    from tests.test_totp_flow import _make as make

    with TestClient(app, base_url=ORIGIN) as client:
        target = client.portal.call(make, "climber@example.com")
        token = client.portal.call(recovery.mint_reset, "climber@example.com")
        boss = client.portal.call(make, "chief@example.com", "admin")
        admin_token = client.portal.call(sessions.mint, boss, False, True).token
        promoted = client.post(f"/api/v1/users/{target.id}/role",
                               headers={"Authorization": f"Bearer {admin_token}"},
                               json={"role": "admin", "attested": True})
        assert promoted.status_code == 204
        assert client.portal.call(_live_tokens, target.id) == []
        assert client.post("/api/v1/auth/reset/complete", headers={"Origin": ORIGIN},
                           json={"token": token, "password": NEW}).status_code == 403


@pytest.mark.db
def test_two_unlinks_at_once_cannot_empty_an_account(accounts):
    """Each sees two credentials and removes a different one, and the account
    is left with none, which is the state the guard exists to prevent."""
    import asyncio

    from app import credentials

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "racer@example.com")
        client.portal.call(_link, user.id, "google", "g-race")
        client.portal.call(_link, user.id, "github", "h-race")
        assert _login(client, "racer@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        principal = client.portal.call(sessions.resolve,
                                       next(c.value for c in client.cookies.jar
                                            if c.name.endswith("potocolom_session")))

        async def both():
            return await asyncio.gather(
                credentials.unlink_identity("google", principal),
                credentials.unlink_identity("github", principal),
                return_exceptions=True,
            )

        client.portal.call(both)
        left = client.portal.call(_identities, user.id)
    assert len(left) >= 1


@pytest.mark.db
def test_an_address_cannot_be_a_list_of_addresses(accounts):
    """It becomes the To header of mail this install sends, so a second
    recipient there is the install addressing a stranger."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lister@example.com")
        assert _login(client, "lister@example.com").status_code == 204
        for bad in ("me@evil.com, victim@corp.com", "me@evil.com;victim@corp.com",
                    "a@b.co <victim@corp.com>", "two@@example.com", "sp ace@example.com"):
            assert _change_email(client, bad).status_code == 400, bad


@pytest.mark.db
def test_an_address_needs_something_on_both_sides_of_the_at(accounts):
    """An empty local part or an empty domain reaches no mailbox, and an
    account whose address reaches nobody can never be recovered by mail."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "halves@example.com")
        assert _login(client, "halves@example.com").status_code == 204
        for bad in ("@example.com", "nobody@", "@", " @example.com", "nobody@ "):
            assert _change_email(client, bad).status_code == 400, bad


@pytest.mark.db
def test_two_requests_adding_a_password_at_once_leave_one_conflict(accounts):
    """Both read no password identity, and the unique index decides. The one
    that loses is a conflict, not a fault of this install."""
    import asyncio

    from fastapi import HTTPException

    from app import credentials

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "twice@example.com")
        client.portal.call(_link, user.id, "google", "g-twice")
        assert _login(client, "twice@example.com").status_code == 204

        async def drop_password():
            async with db.session_factory() as session:
                await session.execute(
                    text("DELETE FROM auth_identities WHERE user_id = :id "
                         "AND provider = 'password'"), {"id": user.id})
                await session.commit()

        client.portal.call(drop_password)
        principal = client.portal.call(sessions.resolve,
                                       next(c.value for c in client.cookies.jar
                                            if c.name.endswith("potocolom_session")))
        body = credentials.PasswordChange(password=NEW)

        async def both():
            return await asyncio.gather(
                credentials.change_password(body, principal),
                credentials.change_password(body, principal),
                return_exceptions=True,
            )

        outcomes = client.portal.call(both)
        identities = client.portal.call(_identities, user.id)
    codes = sorted(o.status_code for o in outcomes if isinstance(o, HTTPException))
    assert codes in ([], [409])
    assert all(not isinstance(o, Exception) or isinstance(o, HTTPException) for o in outcomes)
    assert sum(1 for i in identities if i.provider == "password") == 1


def _csrf_token(client) -> str:
    return next(c.value for c in client.cookies.jar if c.name.endswith("potocolom_csrf"))


def _unlink(client):
    return client.delete("/api/v1/account/identities/google", headers=_csrf(client))


@pytest.mark.db
@pytest.mark.parametrize("change", ["password", "address", "identity"])
def test_a_credential_change_rotates_the_token_that_made_it(accounts, change):
    """A copy of this browser's cookie is what a credential change is usually
    for, and revoking the other sessions never reached it.

    Asserted by presenting the old token, not by seeing a new cookie come
    back: a response that sets a cookie for a session nobody revoked would
    satisfy that and change nothing (issue #436).
    """
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, f"rotate-{change}@example.com")
        client.portal.call(_link, user.id, "google", f"g-rot-{change}")
        assert _login(client, f"rotate-{change}@example.com").status_code == 204
        stolen, stolen_csrf = _session_cookie(client), _csrf_token(client)

        made = {"password": lambda: _change_password(client, PASSWORD, NEW),
                "address": lambda: _change_email(client, f"moved-{change}@example.com"),
                "identity": lambda: _unlink(client)}[change]()
        assert made.status_code == 204, made.text

        # The copy is dead, which is the whole of the fix.
        assert client.get("/api/v1/account",
                          headers={"Authorization": f"Bearer {stolen}"}).status_code == 401
        assert _session_cookie(client) != stolen
        # And the person who made the change is still signed in, reading and
        # writing, with the CSRF token that came back beside the new session.
        assert client.get("/api/v1/account").status_code == 200
        assert _csrf_token(client) != stolen_csrf
        assert _change_email(client, f"again-{change}@example.com").status_code == 204


@pytest.mark.db
def test_a_rotated_session_keeps_the_authentication_it_already_proved(accounts):
    """A rotation replaces a credential, not the person holding it. Dropping
    the recent-authentication window would tell somebody who proved themselves
    a moment ago to prove themselves again, in the middle of securing their
    own account."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "stillrecent@example.com")
        assert _login(client, "stillrecent@example.com").status_code == 204
        assert _change_password(client, PASSWORD, NEW).status_code == 204
        assert client.get("/api/v1/account").json()["recent_auth"] is True
        assert _change_email(client, "stillrecent2@example.com").status_code == 204


class _Drains:
    """A browser that reads what it is sent, so the close completes."""

    async def send_json(self, _payload) -> None:
        return None

    async def close(self, **_kwargs) -> None:
        return None


@pytest.mark.db
def test_a_credential_change_leaves_its_own_canvas_up(accounts):
    """The canvas in front of the person changing their password stays, and
    every other socket on the account goes.

    Through the sweep as well, not only the explicit close. The sweep asks
    PostgreSQL which account sessions are still live rather than trusting the
    id the change asked it to spare, so a rotation that replaced the session
    row instead of its token would pass the line above and close this canvas
    thirty seconds later, for no reason its owner could see.
    """
    from app import realtime

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "canvas@example.com")
        elsewhere = client.portal.call(sessions.mint, user, False)
        theirs = client.portal.call(sessions.resolve, elsewhere.token)
        assert _login(client, "canvas@example.com").status_code == 204
        ours = client.portal.call(sessions.resolve, _session_cookie(client))

        drawing = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=_Drains(),
                                   user_id=user.id, auth_session_id=ours.session.id)
        watched = realtime.Session(id=uuid.uuid4(), model_id="sd-sim", browser=_Drains(),
                                   user_id=user.id, auth_session_id=theirs.session.id)
        realtime.sessions[drawing.id] = drawing
        realtime.sessions[watched.id] = watched
        try:
            assert _change_password(client, PASSWORD, NEW).status_code == 204
            assert watched.id not in realtime.sessions
            assert drawing.id in realtime.sessions
            client.portal.call(realtime.close_dead_sessions)
            assert drawing.id in realtime.sessions
        finally:
            realtime.sessions.pop(drawing.id, None)
            realtime.sessions.pop(watched.id, None)


async def _address_of(user_id: uuid.UUID) -> str:
    async with db.session_factory() as session:
        return (await session.execute(
            select(User.email).where(User.id == user_id))).scalar_one()


def _issued_token(response) -> str:
    from http.cookies import SimpleCookie

    for header in response.headers.getlist("set-cookie"):
        jar = SimpleCookie(header)
        if "__Host-potocolom_session" in jar:
            return jar["__Host-potocolom_session"].value
    raise AssertionError("that response carried no session cookie")


@pytest.mark.db
def test_two_changes_presenting_one_token_leave_one_winner(accounts):
    """Only the request that presents the stored token may replace it.

    The principal is resolved in an earlier transaction, so a stolen cookie
    and the browser it was copied from both reach the swap holding the same
    token. Matching on the id alone let the second overwrite the first: the
    owner's change committed and set a cookie, the copy's change replaced that
    cookie a moment later, and the owner was signed out holding a dead token
    while whoever held the copy kept the session and landed their change as
    well. Before the rotation both cookies simply went on working, so losing
    this race is what costs the owner exclusive access (#443).
    """
    import asyncio

    from fastapi import HTTPException

    from app import credentials
    from tests.test_totp_flow import _live_sessions_for

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "onewinner@example.com")
        assert _login(client, "onewinner@example.com").status_code == 204
        presented = _session_cookie(client)
        principal = client.portal.call(sessions.resolve, presented)
        wanted = ("won@example.com", "lost@example.com")

        async def both():
            return await asyncio.gather(*(
                credentials.change_email(credentials.AddressChange(email=address), principal)
                for address in wanted), return_exceptions=True)

        outcomes = client.portal.call(both)
        live = client.portal.call(_live_sessions_for, user.id)
        won = wanted[0] if not isinstance(outcomes[0], Exception) else wanted[1]
        landed = client.portal.call(_address_of, user.id)

    refused = [out for out in outcomes if isinstance(out, HTTPException)]
    assert [out.status_code for out in refused] == [409], outcomes
    # Refused by raising, which is what rolls the loser's transaction back:
    # a browser handed a token for a change that did not happen is the hole
    # this closes, not a smaller version of it.
    served = [out for out in outcomes if not isinstance(out, Exception)]
    assert len(served) == 1
    assert len(live) == 1
    assert sessions.token_hash(_issued_token(served[0])) == live[0].token_hash
    assert sessions.token_hash(presented) != live[0].token_hash
    # And the winner's change stands rather than being rolled back with the
    # loser's.
    assert landed == won
