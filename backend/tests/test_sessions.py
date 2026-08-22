import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import db, sessions
from app.tables import Session, User

PLAIN = "http://box.lan:8080"
SECURE = "https://studio.example.com"


@pytest.fixture
def connected(portal_runner):
    assert portal_runner(db.connect()) is True

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM sessions"))
            await session.execute(text("DELETE FROM users WHERE email <> :local"),
                                  {"local": db.LOCAL_USER_EMAIL})
            await session.commit()

    portal_runner(clear())
    try:
        yield portal_runner
    finally:
        portal_runner(clear())
        portal_runner(db.dispose())


def _user(portal_runner, role="user", state="active") -> User:
    async def go():
        async with db.session_factory() as session:
            row = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com",
                       role=role, state=state)
            session.add(row)
            await session.commit()
            return row

    return portal_runner(go())


async def _row(token: str) -> Session | None:
    async with db.session_factory() as session:
        return (await session.execute(
            select(Session).where(Session.token_hash == sessions.token_hash(token))
        )).scalar_one_or_none()


def test_the_locked_lifetimes_are_what_the_contract_says():
    assert sessions.ABSOLUTE == timedelta(hours=12)
    assert sessions.REMEMBER_ABSOLUTE == timedelta(days=30)
    assert sessions.REMEMBER_IDLE == timedelta(days=7)
    assert sessions.ADMIN_IDLE == timedelta(minutes=30)
    assert sessions.RECENT_AUTH == timedelta(minutes=30)
    assert sessions.TOKEN_BYTES == 32


def test_cookie_names_carry_the_host_prefix_only_where_it_is_legal():
    """__Host- requires Secure, which a browser refuses to set over plain
    HTTP, and LAN self-hosting is plain HTTP."""
    assert sessions.cookie_names(SECURE) == (
        "__Host-potocolom_session", "__Host-potocolom_csrf")
    assert sessions.cookie_names(PLAIN) == ("potocolom_session", "potocolom_csrf")


@pytest.mark.db
def test_a_minted_session_keeps_only_the_hash(connected):
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=False))
    assert len(issued.token) >= 43 and len(issued.csrf) >= 43
    assert issued.token != issued.csrf
    row = connected(_row(issued.token))
    assert row is not None
    assert row.token_hash == sessions.token_hash(issued.token)
    assert issued.token.encode() not in row.token_hash


@pytest.mark.db
def test_two_mints_never_collide(connected):
    user = _user(connected)
    first = connected(sessions.mint(user, remember_me=False))
    second = connected(sessions.mint(user, remember_me=False))
    assert first.token != second.token and first.csrf != second.csrf


@pytest.mark.db
def test_a_plain_session_lasts_twelve_hours_with_no_idle_limit(connected):
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=False))
    row = connected(_row(issued.token))
    assert _about(row.absolute_expires_at, sessions.ABSOLUTE)
    assert row.idle_expires_at is None
    assert row.remember_me is False


@pytest.mark.db
def test_remember_me_lasts_thirty_days_and_idles_after_seven(connected):
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=True))
    row = connected(_row(issued.token))
    assert _about(row.absolute_expires_at, sessions.REMEMBER_ABSOLUTE)
    assert _about(row.idle_expires_at, sessions.REMEMBER_IDLE)
    assert row.remember_me is True


@pytest.mark.db
def test_an_administrator_idles_out_in_thirty_minutes_and_cannot_be_remembered(connected):
    """An administrator session is the most valuable thing in the install, so
    it does not get the convenience the other roles get."""
    admin = _user(connected, role="admin")
    issued = connected(sessions.mint(admin, remember_me=True))
    row = connected(_row(issued.token))
    assert _about(row.absolute_expires_at, sessions.ABSOLUTE)
    assert _about(row.idle_expires_at, sessions.ADMIN_IDLE)
    assert row.remember_me is False


def _about(moment: datetime, window: timedelta) -> bool:
    return abs((moment - datetime.now(timezone.utc)) - window) < timedelta(minutes=1)


@pytest.mark.db
def test_resolving_a_session_returns_its_account_and_moves_the_idle_window(connected):
    admin = _user(connected, role="admin")
    issued = connected(sessions.mint(admin, remember_me=False))
    before = connected(_row(issued.token)).idle_expires_at

    async def older() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("UPDATE sessions SET idle_expires_at = :soon, last_seen_at = :then"),
                {"soon": datetime.now(timezone.utc) + timedelta(minutes=5),
                 "then": datetime.now(timezone.utc) - timedelta(minutes=25)},
            )
            await session.commit()

    connected(older())
    resolved = connected(sessions.resolve(issued.token))
    assert resolved is not None and resolved.user.id == admin.id
    assert connected(_row(issued.token)).idle_expires_at > before - timedelta(minutes=1)


@pytest.mark.db
def test_an_unknown_token_resolves_to_nothing(connected):
    assert connected(sessions.resolve("not-a-session")) is None


@pytest.mark.db
@pytest.mark.parametrize("column", ["absolute_expires_at", "idle_expires_at"])
def test_an_expired_session_resolves_to_nothing(connected, column):
    user = _user(connected, role="admin")
    issued = connected(sessions.mint(user, remember_me=False))

    async def expire() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text(f"UPDATE sessions SET {column} = :past"),
                {"past": datetime.now(timezone.utc) - timedelta(seconds=1)},
            )
            await session.commit()

    connected(expire())
    assert connected(sessions.resolve(issued.token)) is None


@pytest.mark.db
def test_a_revoked_session_resolves_to_nothing(connected):
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=False))
    connected(sessions.revoke(connected(_row(issued.token)).id))
    assert connected(sessions.resolve(issued.token)) is None


@pytest.mark.db
def test_revoking_every_session_leaves_none_of_them_usable(connected):
    user = _user(connected)
    other = _user(connected)
    mine = [connected(sessions.mint(user, remember_me=False)) for _ in range(3)]
    theirs = connected(sessions.mint(other, remember_me=False))
    connected(sessions.revoke_all(user.id))
    assert all(connected(sessions.resolve(issued.token)) is None for issued in mine)
    assert connected(sessions.resolve(theirs.token)) is not None


@pytest.mark.db
def test_rotation_replaces_the_session_rather_than_adding_one(connected):
    """A credential or role change must not leave the old cookie working."""
    user = _user(connected)
    first = connected(sessions.mint(user, remember_me=True))
    row = connected(_row(first.token))
    second = connected(sessions.rotate(row.id, user))
    assert connected(sessions.resolve(first.token)) is None
    assert connected(sessions.resolve(second.token)) is not None
    # Remembered stays remembered across a rotation.
    assert connected(_row(second.token)).remember_me is True


@pytest.mark.db
def test_a_fresh_session_carries_no_recent_authentication_by_default(connected):
    """Setup proves a link, not a person, so it must not open the window that
    guards credential changes."""
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=False))
    assert connected(_row(issued.token)).recent_auth_at is None
    assert sessions.is_recent(connected(_row(issued.token))) is False


@pytest.mark.db
def test_a_password_login_opens_the_recent_window_for_thirty_minutes(connected):
    user = _user(connected)
    issued = connected(sessions.mint(user, remember_me=False, authenticated=True))
    row = connected(_row(issued.token))
    assert row.recent_auth_at is not None
    assert sessions.is_recent(row) is True

    async def age() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("UPDATE sessions SET recent_auth_at = :past"),
                {"past": datetime.now(timezone.utc) - sessions.RECENT_AUTH
                 - timedelta(seconds=1)},
            )
            await session.commit()

    connected(age())
    assert sessions.is_recent(connected(_row(issued.token))) is False


@pytest.mark.db
def test_a_session_record_keeps_no_address_and_no_user_agent(connected):
    """Locked acceptance: durable records carry neither."""
    user = _user(connected)
    connected(sessions.mint(user, remember_me=False))
    columns = {column.name for column in Session.__table__.columns}
    assert not columns & {"ip", "ip_address", "remote_addr", "user_agent"}


@pytest.mark.db
def test_resolving_does_not_write_on_every_single_request(connected):
    """Every authenticated request would otherwise be a SELECT plus an UPDATE
    plus a COMMIT on a fifteen connection pool, to move a window by
    milliseconds."""
    admin = _user(connected, role="admin")
    issued = connected(sessions.mint(admin, remember_me=False))
    first = connected(_row(issued.token))
    connected(sessions.resolve(issued.token))
    touched = connected(_row(issued.token))
    assert touched.last_seen_at is not None
    seen = touched.last_seen_at
    connected(sessions.resolve(issued.token))
    assert connected(_row(issued.token)).last_seen_at == seen
    assert first.idle_expires_at is not None


@pytest.mark.db
def test_a_stale_session_still_slides_before_it_idles_out(connected):
    admin = _user(connected, role="admin")
    issued = connected(sessions.mint(admin, remember_me=False))
    connected(sessions.resolve(issued.token))

    async def go_quiet() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("UPDATE sessions SET last_seen_at = :then, idle_expires_at = :soon"),
                {"then": datetime.now(timezone.utc) - timedelta(minutes=20),
                 "soon": datetime.now(timezone.utc) + timedelta(minutes=10)},
            )
            await session.commit()

    connected(go_quiet())
    assert connected(sessions.resolve(issued.token)) is not None
    assert _about(connected(_row(issued.token)).idle_expires_at, sessions.ADMIN_IDLE)
