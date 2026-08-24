"""The commands an operator runs at the machine.

None of these is reachable over HTTP. They are the way back into an install
that locked its administrators out, the way out of accounts mode, and the way
to change the key everything else is sealed with.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import db, keyring, operator
from app.main import app
from app.tables import Job, User
from tests.test_totp_flow import ORIGIN, ROOT_KEYS, _factors, _login, _make, accounts

__all__ = ["accounts"]

SECOND_KEY = "2:" + "B" * 43 + "=," + ROOT_KEYS


@pytest.fixture
def library(accounts, monkeypatch):
    """Puts the installation back the way the accounts fixture expects it.

    Two of these commands change what the install is: a collapse turns
    accounts off, and a rotation leaves the key ring somewhere else. The next
    test's connect refuses both, so they are undone here rather than left for
    whatever runs next to trip over.
    """
    yield accounts

    monkeypatch.setenv("ROOT_KEYS", ROOT_KEYS)
    keyring.get_key_ring.cache_clear()
    from app.settings import get_settings

    get_settings.cache_clear()

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("asset_shares", "assets", "jobs"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.execute(
                text("UPDATE installation_auth_state SET auth_mode = 'accounts', "
                     "root_key_version = 1 WHERE id = 1"))
            await session.commit()

    if db.session_factory is None:
        # serving=False: a rotation left the recorded key version ahead of the
        # ring this fixture just put back, and the serving path refuses that
        # before it can be undone.
        accounts(db.connect(serving=False))
    accounts(clear())
    accounts(db.dispose())


async def _count(table: str) -> int:
    async with db.session_factory() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


async def _mode() -> str:
    async with db.session_factory() as session:
        return str(await session.scalar(
            text("SELECT auth_mode FROM installation_auth_state WHERE id = 1")))


@pytest.mark.db
def test_collapsing_needs_the_phrase_typed_out(library):
    """It destroys every account on the installation. A flag is too easy to
    pass by accident and too easy to copy out of a forum post."""
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "kept@example.com")
        for wrong in ("", "yes", "COLLAPSE", operator.COLLAPSE_PHRASE.upper()):
            with pytest.raises(ValueError):
                client.portal.call(operator.collapse, wrong)
        assert client.portal.call(_count, "users") >= 2
        assert client.portal.call(_mode) == "accounts"


@pytest.mark.db
def test_collapsing_destroys_the_accounts_and_keeps_the_work(library):
    from tests.test_account_deletion import _owned_work

    with TestClient(app, base_url=ORIGIN) as client:
        subject = client.portal.call(_make, "collapsing@example.com")
        client.portal.call(_owned_work, subject.id)
        assert _login(client, "collapsing@example.com").status_code == 204
        assert client.portal.call(_count, "sessions") == 1

        counted = client.portal.call(operator.collapse, operator.COLLAPSE_PHRASE)

        assert counted["accounts"] == 1
        assert counted["generations"] == 1
        assert client.portal.call(_count, "users") == 1
        assert client.portal.call(_count, "sessions") == 0
        assert client.portal.call(_count, "auth_identities") == 0
        assert client.portal.call(_count, "jobs") == 1
        assert client.portal.call(_count, "assets") == 1
        assert client.portal.call(_mode) == "none"

        async def owner() -> uuid.UUID:
            async with db.session_factory() as session:
                return (await session.execute(select(Job.user_id))).scalar_one()

        async def local() -> uuid.UUID:
            async with db.session_factory() as session:
                return (await session.execute(
                    select(User.id).where(User.email == db.LOCAL_USER_EMAIL))).scalar_one()

        assert client.portal.call(owner) == client.portal.call(local)


@pytest.mark.db
def test_reclaiming_restores_one_account_to_an_active_administrator(library):
    """Every administrator suspended at once leaves nobody to press the
    button that would fix it."""
    with TestClient(app, base_url=ORIGIN) as client:
        locked = client.portal.call(_make, "lockedout@example.com")

        async def suspend() -> None:
            async with db.session_factory() as session:
                await session.execute(
                    text("UPDATE users SET state = 'suspended' WHERE id = :id"),
                    {"id": locked.id})
                await session.commit()

        client.portal.call(suspend)
        was = client.portal.call(operator.reclaim_restore, "  LockedOut@Example.com ")
        assert was == "suspended"

        async def reread() -> User:
            async with db.session_factory() as session:
                return await session.get(User, locked.id)

        row = client.portal.call(reread)
        assert (row.state, row.role) == ("active", "admin")
        assert _login(client, "lockedout@example.com").status_code == 204


@pytest.mark.db
def test_reclaiming_an_address_nobody_holds_says_so(library):
    with TestClient(app, base_url=ORIGIN) as client:
        with pytest.raises(LookupError):
            client.portal.call(operator.reclaim_restore, "nobody@example.com")


@pytest.mark.db
def test_reclaiming_mints_a_setup_link_and_retires_the_old_one(library):
    from app.tables import AuthToken

    with TestClient(app, base_url=ORIGIN) as client:
        first = client.portal.call(operator.reclaim_claim)
        second = client.portal.call(operator.reclaim_claim)
        assert first != second

        async def live_setup_tokens() -> int:
            async with db.session_factory() as session:
                return int(await session.scalar(
                    select(func.count()).select_from(AuthToken)
                    .where(AuthToken.purpose == "setup",
                           AuthToken.consumed_at.is_(None))) or 0)

        assert client.portal.call(live_setup_tokens) == 1


@pytest.mark.db
def test_rotating_re_encrypts_every_secret_under_the_newest_key(library, monkeypatch):
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "rotated@example.com")
        assert _login(client, "rotated@example.com").status_code == 204
        secret, _ = _enrol(client)
        before = client.portal.call(_factors)[0]
        assert before.key_version == 1

        monkeypatch.setenv("ROOT_KEYS", SECOND_KEY)
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        result = client.portal.call(operator.rotate_keys)
        after = client.portal.call(_factors)[0]

    assert result == {"reencrypted": 1, "active_version": 2}
    assert after.key_version == 2
    assert after.secret_ciphertext != before.secret_ciphertext
    # The same secret, readable under the new key.
    ring = keyring.get_key_ring()
    assert ring.decrypt("totp-factors", after.secret_ciphertext,
                        after.user_id.bytes).decode() == secret


@pytest.mark.db
def test_rotating_refuses_when_a_key_it_would_need_is_gone(library, monkeypatch):
    """Rewriting a blob this install cannot read destroys the secret behind
    it, which is somebody's second factor."""
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "lostkey@example.com")
        assert _login(client, "lostkey@example.com").status_code == 204
        _enrol(client)

        # A ring that has moved on and left version 1 behind.
        monkeypatch.setenv("ROOT_KEYS", "2:" + "B" * 43 + "=")
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        with pytest.raises(keyring.KeyRingError):
            client.portal.call(operator.rotate_keys)
        assert client.portal.call(_factors)[0].key_version == 1


@pytest.mark.db
def test_the_check_says_which_keys_are_still_holding_something(library, monkeypatch):
    from tests.test_totp_flow import _enrol

    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(_make, "checked@example.com")
        assert _login(client, "checked@example.com").status_code == 204
        _enrol(client)
        assert client.portal.call(operator.retired_versions) == []

        monkeypatch.setenv("ROOT_KEYS", SECOND_KEY)
        keyring.get_key_ring.cache_clear()
        from app.settings import get_settings

        get_settings.cache_clear()

        assert client.portal.call(operator.retired_versions) == [1]
        client.portal.call(operator.rotate_keys)
        assert client.portal.call(operator.retired_versions) == []


def test_the_configuration_report_says_what_would_happen(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        report = operator._configured()
    finally:
        get_settings.cache_clear()
    assert report["public_url"] == ORIGIN
    assert "SMTP_HOST" in report["mail"]
    assert report["oauth"] == "ok"


def test_reclaim_takes_one_of_the_two_things_it_can_do():
    with pytest.raises(SystemExit):
        operator.main(["reclaim"])
    with pytest.raises(SystemExit):
        operator.main(["reclaim", "--claim", "--restore", "someone@example.com"])
