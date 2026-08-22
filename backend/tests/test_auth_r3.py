import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db
from app.keyring import get_key_ring
from app.settings import get_settings
from app.keyring import KeyRing, KeyRingError
from app.tables import (
    AuthFactor,
    AuthIdentity,
    AuthToken,
    Invitation,
    MailOutbox,
    RecoveryCode,
    Session,
    User,
)

TOTP_PURPOSE = "totp-factors"
V1 = bytes(range(32))
V2 = bytes(range(32, 64))


@pytest.fixture
def now():
    return datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def connected(portal_runner):
    """Every account this file makes goes at teardown.

    These tables are this file's alone, and a row left behind is handed to the
    next test as if it had created it: the rotation sweep reads by key version,
    so one stale factor from an earlier test is enough to fail it.
    """
    assert portal_runner(db.connect()) is True

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM invitations"))
            await session.execute(text("DELETE FROM mail_outbox"))
            await session.execute(text("DELETE FROM auth_tokens"))
            await session.execute(text("DELETE FROM users WHERE email <> :local"),
                                  {"local": db.LOCAL_USER_EMAIL})
            await session.commit()

    try:
        yield portal_runner
    finally:
        portal_runner(clear())
        portal_runner(db.dispose())


def _insert(portal_runner, *rows):
    async def go():
        async with db.session_factory() as session:
            session.add_all(rows)
            await session.commit()

    portal_runner(go())


def _expect_refusal(portal_runner, *rows):
    with pytest.raises((IntegrityError, DBAPIError)):
        _insert(portal_runner, *rows)


def _user(portal_runner, email, **kwargs):
    row = User(id=uuid.uuid4(), email=email, **kwargs)
    _insert(portal_runner, row)
    return row


@pytest.mark.db
def test_two_users_cannot_share_a_normalized_email(connected):
    _user(connected, "Person@Example.com")
    with pytest.raises((IntegrityError, DBAPIError)):
        _user(connected, "  person@example.COM  ")


@pytest.mark.db
def test_account_state_is_constrained_to_the_designed_states(connected):
    for state in ("active", "suspended", "disabled", "deletion_pending", "purging"):
        _user(connected, f"{state}@example.com", state=state)
    _expect_refusal(
        connected,
        User(id=uuid.uuid4(), email="x@example.com", state="banned"),
    )


@pytest.mark.db
def test_role_is_constrained_to_viewer_user_and_admin(connected):
    for role in ("viewer", "user", "admin"):
        _user(connected, f"{role}@example.com", role=role)
    _expect_refusal(
        connected,
        User(id=uuid.uuid4(), email="r@example.com", role="superuser"),
    )


@pytest.mark.db
def test_a_user_has_at_most_one_password_identity(connected):
    owner = _user(connected, "one-password@example.com")
    _insert(connected, AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="password",
                                    subject=owner.email, password_hash="argon2-a"))
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=owner.id,
                                            provider="password", subject="other@example.com",
                                            password_hash="argon2-b"))


@pytest.mark.db
def test_a_user_may_hold_several_provider_identities(connected):
    owner = _user(connected, "linked@example.com")
    _insert(
        connected,
        AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="google", subject="g-1"),
        AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="github", subject="h-1"),
    )


@pytest.mark.db
def test_one_provider_subject_belongs_to_one_account(connected):
    first = _user(connected, "first@example.com")
    second = _user(connected, "second@example.com")
    _insert(connected, AuthIdentity(id=uuid.uuid4(), user_id=first.id, provider="google",
                                    subject="shared-sub"))
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=second.id,
                                            provider="google", subject="shared-sub"))


@pytest.mark.db
def test_a_password_identity_without_a_hash_is_refused(connected):
    owner = _user(connected, "no-hash@example.com")
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=owner.id,
                                            provider="password", subject="no-hash@example.com"))


@pytest.mark.db
def test_a_provider_identity_never_carries_a_password_hash(connected):
    """A provider identity is proof from the provider, never a local credential."""
    owner = _user(connected, "provider-hash@example.com")
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="google",
                                            subject="g-2", password_hash="argon2-c"))


@pytest.mark.db
def test_an_unknown_identity_provider_is_refused(connected):
    owner = _user(connected, "unknown-provider@example.com")
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="facebook",
                                            subject="f-1"))


@pytest.mark.db
def test_a_session_token_hash_is_unique(connected, now):
    owner = _user(connected, "session@example.com")
    _insert(connected, Session(id=uuid.uuid4(), user_id=owner.id, token_hash=b"h" * 32,
                               absolute_expires_at=now))
    _expect_refusal(connected, Session(id=uuid.uuid4(), user_id=owner.id, token_hash=b"h" * 32,
                                       absolute_expires_at=now))


@pytest.mark.db
def test_deleting_an_account_takes_its_authentication_rows_with_it(connected, now):
    owner = _user(connected, "cascade@example.com")
    _insert(
        connected,
        AuthIdentity(id=uuid.uuid4(), user_id=owner.id, provider="google", subject="g-cascade"),
        Session(id=uuid.uuid4(), user_id=owner.id, token_hash=b"c" * 32,
                absolute_expires_at=now),
        AuthToken(id=uuid.uuid4(), user_id=owner.id, purpose="reset", token_hash=b"t" * 32,
                  expires_at=now),
        AuthFactor(id=uuid.uuid4(), user_id=owner.id, kind="totp", secret_ciphertext=b"blob",
                   key_version=1),
        RecoveryCode(id=uuid.uuid4(), user_id=owner.id, code_hash=b"r" * 32),
    )

    async def purge() -> dict:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner.id})
            await session.commit()
        async with db.session_factory() as session:
            return {
                table: (await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE user_id = :id"), {"id": owner.id}
                )).scalar_one()
                for table in ("auth_identities", "sessions", "auth_tokens", "auth_factors",
                              "recovery_codes")
            }

    assert set(connected(purge()).values()) == {0}


@pytest.mark.db
def test_an_unknown_auth_token_purpose_is_refused(connected, now):
    owner = _user(connected, "token-purpose@example.com")
    for purpose in ("reset", "recovery", "challenge"):
        _insert(connected, AuthToken(id=uuid.uuid4(), user_id=owner.id, purpose=purpose,
                                     token_hash=purpose.encode().ljust(32, b"."),
                                     expires_at=now))
    _insert(connected, AuthToken(id=uuid.uuid4(), user_id=None, purpose="setup",
                                 token_hash=b"setup".ljust(32, b"."), expires_at=now))
    _expect_refusal(connected, AuthToken(id=uuid.uuid4(), user_id=owner.id, purpose="wire-transfer",
                                         token_hash=b"w" * 32, expires_at=now))


@pytest.mark.db
def test_only_a_setup_token_may_stand_without_an_account(connected, now):
    """Setup mints the first administrator, so a consumer that reads a missing
    account as "this is the setup flow" must not meet a reset row shaped
    like one."""
    owner = _user(connected, "setup-shape@example.com")
    _expect_refusal(connected, AuthToken(id=uuid.uuid4(), user_id=owner.id, purpose="setup",
                                         token_hash=b"S" * 32, expires_at=now))
    _expect_refusal(connected, AuthToken(id=uuid.uuid4(), user_id=None, purpose="reset",
                                         token_hash=b"R" * 32, expires_at=now))


@pytest.mark.db
def test_a_password_identity_is_unique_per_normalized_address(connected):
    """Accounts are one per normalized address, so their password identities
    must be too, or a login lookup has two rows to choose between."""
    first = _user(connected, "Case@example.com")
    second = _user(connected, "other@example.com")
    _insert(connected, AuthIdentity(id=uuid.uuid4(), user_id=first.id, provider="password",
                                    subject="Case@example.com", password_hash="argon2-a"))
    _expect_refusal(connected, AuthIdentity(id=uuid.uuid4(), user_id=second.id,
                                            provider="password", subject=" case@EXAMPLE.com ",
                                            password_hash="argon2-b"))


@pytest.mark.db
def test_a_setup_token_needs_no_account(connected, now):
    """Setup mints a capability before anyone has claimed the installation."""
    _insert(connected, AuthToken(id=uuid.uuid4(), user_id=None, purpose="setup",
                                 token_hash=b"s" * 32, expires_at=now))


@pytest.mark.db
def test_one_open_invitation_per_normalized_email(connected, now):
    admin = _user(connected, "inviter@example.com", role="admin")

    def invitation(email, **kwargs):
        return Invitation(id=uuid.uuid4(), email=email, role="user", invited_by=admin.id,
                          token_hash=uuid.uuid4().bytes * 2, expires_at=now, **kwargs)

    _insert(connected, invitation("Guest@Example.com"))
    _expect_refusal(connected, invitation(" guest@example.com "))
    _insert(connected, invitation("later@example.com", revoked_at=now))
    _insert(connected, invitation("later@example.com"))


@pytest.mark.db
def test_a_user_has_at_most_one_totp_factor(connected):
    owner = _user(connected, "totp@example.com")
    _insert(connected, AuthFactor(id=uuid.uuid4(), user_id=owner.id, kind="totp",
                                  secret_ciphertext=b"blob-a", key_version=1))
    _expect_refusal(connected, AuthFactor(id=uuid.uuid4(), user_id=owner.id, kind="totp",
                                          secret_ciphertext=b"blob-b", key_version=1))


@pytest.mark.db
def test_a_recovery_code_is_recorded_once_per_account(connected):
    owner = _user(connected, "recovery@example.com")
    _insert(connected, RecoveryCode(id=uuid.uuid4(), user_id=owner.id, code_hash=b"k" * 32))
    _expect_refusal(connected, RecoveryCode(id=uuid.uuid4(), user_id=owner.id,
                                            code_hash=b"k" * 32))


@pytest.mark.db
def test_outbox_rows_start_pending_and_reject_an_unknown_state(connected, now):
    _insert(connected, MailOutbox(id=uuid.uuid4(), to_email="Mail@Example.com",
                                  template="invitation", payload={"token": "capability"},
                                  next_attempt_at=now))

    async def state_of() -> str:
        async with db.session_factory() as session:
            return (await session.execute(
                select(MailOutbox.state).where(MailOutbox.template == "invitation")
            )).scalar_one()

    assert connected(state_of()) == "pending"
    _expect_refusal(connected, MailOutbox(id=uuid.uuid4(), to_email="x@example.com",
                                          template="reset", payload={}, next_attempt_at=now,
                                          state="posted"))


@pytest.mark.db
def test_a_factor_secret_survives_rotation_and_the_old_key_being_removed(connected):
    """Active write, multi read, re-encrypt, then remove, against real rows."""
    owner = _user(connected, "rotate@example.com")
    written = KeyRing([(1, V1)])
    _insert(connected, AuthFactor(id=uuid.uuid4(), user_id=owner.id, kind="totp",
                                  secret_ciphertext=written.encrypt(TOTP_PURPOSE, b"seed",
                                                                    owner.id.bytes),
                                  key_version=written.active_version))

    rotating = KeyRing([(2, V2), (1, V1)])

    async def reencrypt() -> None:
        async with db.session_factory() as session:
            rows = (await session.execute(
                select(AuthFactor).where(AuthFactor.key_version != rotating.active_version)
            )).scalars().all()
            for row in rows:
                row.secret_ciphertext = rotating.reencrypt(
                    TOTP_PURPOSE, row.secret_ciphertext, aad=row.user_id.bytes
                )
                row.key_version = rotating.active_version
            await session.commit()

    async def stored() -> AuthFactor:
        async with db.session_factory() as session:
            return (await session.execute(
                select(AuthFactor).where(AuthFactor.user_id == owner.id)
            )).scalar_one()

    assert rotating.decrypt(TOTP_PURPOSE, connected(stored()).secret_ciphertext,
                            aad=owner.id.bytes) == b"seed"
    connected(reencrypt())
    row = connected(stored())
    assert row.key_version == 2
    removed = KeyRing([(2, V2)])
    assert removed.decrypt(TOTP_PURPOSE, row.secret_ciphertext, aad=owner.id.bytes) == b"seed"


@pytest.mark.db
def test_a_secret_still_on_a_removed_key_is_unreadable(connected):
    """Removing a key before re-encrypting must lose the secret, not expose it."""
    owner = _user(connected, "lost@example.com")
    _insert(connected, AuthFactor(id=uuid.uuid4(), user_id=owner.id, kind="totp",
                                  secret_ciphertext=KeyRing([(1, V1)]).encrypt(
                                      TOTP_PURPOSE, b"seed", aad=owner.id.bytes),
                                  key_version=1))

    async def stored() -> bytes:
        async with db.session_factory() as session:
            return (await session.execute(
                select(AuthFactor.secret_ciphertext).where(AuthFactor.user_id == owner.id)
            )).scalar_one()

    with pytest.raises(KeyRingError):
        KeyRing([(2, V2)]).decrypt(TOTP_PURPOSE, connected(stored()), aad=owner.id.bytes)


@pytest.mark.db
def test_enabling_accounts_records_the_key_version_it_writes_with(connected, monkeypatch):
    """A guard nothing arms cannot fire, and the install that needed it is
    exactly the one that finds out too late."""
    monkeypatch.setattr(db, "get_key_ring", lambda: KeyRing([(7, V1)]))
    try:
        connected(db.enable_accounts_mode(db.session_factory))
        assert connected(db.read_installation_root_key_version()) == 7
    finally:
        async def clear() -> None:
            async with db.session_factory() as session:
                await session.execute(text("DELETE FROM installation_auth_state"))
                await session.commit()

        connected(clear())


@pytest.mark.db
def test_enabling_accounts_without_a_usable_ring_is_refused(connected, monkeypatch):
    """Enabling accounts with no ring would write secrets nothing can read."""
    def unusable() -> KeyRing:
        raise KeyRingError("root keys are not configured")

    monkeypatch.setattr(db, "get_key_ring", unusable)
    with pytest.raises(KeyRingError):
        connected(db.enable_accounts_mode(db.session_factory))
    assert connected(db.read_installation_root_key_version()) is None


@pytest.mark.db
def test_startup_refuses_when_the_installation_key_version_is_gone(connected, monkeypatch):
    """PostgreSQL is the authority on which root version this install writes."""
    async def record(version: int | None) -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("INSERT INTO installation_auth_state (id, auth_mode, root_key_version) "
                     "VALUES (1, 'accounts', :v) "
                     "ON CONFLICT (id) DO UPDATE SET root_key_version = :v"),
                {"v": version},
            )
            await session.commit()

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM installation_auth_state"))
            await session.commit()

    connected(record(1))
    try:
        monkeypatch.setattr(db, "get_key_ring", lambda: KeyRing([(1, V1)]))
        connected(db.validate_startup_key_ring())
        monkeypatch.setattr(db, "get_key_ring", lambda: KeyRing([(2, V2)]))
        with pytest.raises(RuntimeError, match="root key"):
            connected(db.validate_startup_key_ring())
    finally:
        connected(clear())


@pytest.mark.db
def test_startup_key_ring_check_is_quiet_before_accounts_are_enabled(connected, monkeypatch):
    def refuse() -> KeyRing:
        raise AssertionError("the key ring must not be read before accounts are enabled")

    monkeypatch.setattr(db, "get_key_ring", refuse)
    connected(db.validate_startup_key_ring())


def test_the_key_ring_never_falls_back_to_the_fleet_secret(monkeypatch):
    """Acceptance: root keys and fleet keys are separate key material."""
    monkeypatch.delenv("ROOT_KEYS", raising=False)
    monkeypatch.setenv("FLEET_TOKEN_KEY", "a-fleet-shared-secret")
    get_settings.cache_clear()
    get_key_ring.cache_clear()
    try:
        with pytest.raises(KeyRingError):
            get_key_ring()
    finally:
        get_settings.cache_clear()
        get_key_ring.cache_clear()
