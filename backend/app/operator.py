"""The commands an operator runs at the machine, when the browser cannot help.

Every one of these is deliberately offline. They are the way back into an
install that has locked its administrators out, the way out of accounts mode,
and the way to change the key everything else is sealed with. None of them is
reachable over HTTP, because a route that can do any of this is a route worth
stealing a session for.
"""

import argparse
import asyncio
import uuid

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, db, factors, keyring, sessions
from app.enable import AlreadyClaimed, mint_setup_token
from app.settings import get_settings
from app.tables import (
    Asset, AuthFactor, AuthIdentity, AuthToken, Invitation, Job, RecoveryCode, Session, User,
)

COLLAPSE_PHRASE = "destroy the accounts on this installation"
TOTP_PURPOSE = "totp-factors"


async def collapse(confirmation: str) -> dict:
    """Turn accounts off, destroying every account this install has.

    The only way out of accounts mode, and it is offline on purpose: nothing
    reachable over HTTP may undo the decision to require authentication.

    The work stays. Jobs and assets belong to the installation, and destroying
    somebody's images because their account is going away is a different act
    from ending the accounts; they move to the implicit local user, which is
    who owns everything in none mode.
    """
    if confirmation != COLLAPSE_PHRASE:
        raise ValueError(f'the confirmation phrase is "{COLLAPSE_PHRASE}"')
    assert db.session_factory is not None
    async with db.session_factory() as session:
        async with session.begin():
            local = await _local_user(session)
            counted = {
                "accounts": (await session.execute(
                    select(func.count()).select_from(User).where(User.id != local)
                )).scalar_one(),
                "generations": (await session.execute(
                    select(func.count()).select_from(Job).where(Job.user_id != local)
                )).scalar_one(),
            }
            # The work first: it is what a foreign key would otherwise hold on
            # to, and moving it is the whole reason the accounts can go.
            await session.execute(update(Job).where(Job.user_id != local).values(user_id=local))
            await session.execute(update(Asset).where(Asset.user_id != local).values(user_id=local))
            # Sessions after the factor, not before it, which is how this read
            # until #443. No foreign key ties these five together, so the order
            # is free to choose, and this is the one every route that touches
            # more than one of them already takes: a challenge claims its token
            # before it locks the factor, and enrolling or removing a factor
            # holds auth_factors and recovery_codes while it rotates the
            # session making the change. Deleting sessions first put this
            # transaction on the far side of that pair, and a collapse run
            # while the API was still up died of a deadlock instead.
            for table in (AuthToken, AuthFactor, RecoveryCode, Session, AuthIdentity):
                await session.execute(delete(table))
            await session.execute(delete(Invitation))
            await session.execute(delete(User).where(User.id != local))
            await session.execute(
                text("UPDATE users SET role = 'admin', state = 'active', "
                     "prior_state = NULL, deletion_requested_at = NULL WHERE id = :id"),
                {"id": local})
            await session.execute(
                text("UPDATE installation_auth_state SET auth_mode = 'none', "
                     "root_key_version = NULL WHERE id = 1"))
    return counted


async def reclaim_claim() -> str:
    """A fresh one-use setup link, for an install nobody can get into.

    The same link `make auth-enable` prints, minted again. Whoever opens it
    becomes the administrator, so it replaces any link still outstanding.
    """
    assert db.session_factory is not None
    async with db.session_factory() as session:
        async with session.begin():
            # One transaction, so the decision about whether this install can
            # be claimed and the link that acts on it cannot disagree.
            # Minting retires whatever link was outstanding, which is the
            # point: whoever held the old one is not who is at the machine.
            try:
                return await mint_setup_token(session)
            except AlreadyClaimed as refused:
                raise LookupError(str(refused)) from None


async def reclaim_restore(email: str) -> str:
    """Put one account back on its feet, from the machine.

    An install can end up with every administrator suspended, deleted, or
    locked out at once, and then there is nobody left to press the button that
    would fix it.
    """
    assert db.session_factory is not None
    async with db.session_factory() as session:
        async with session.begin():
            target = (await session.execute(
                select(User).where(func.lower(func.btrim(User.email)) == email.strip().lower())
            )).scalar_one_or_none()
            if target is None:
                raise LookupError(f"no account holds {email}")
            if target.state == "purging":
                raise LookupError(f"{email} is being purged and cannot be restored")
            was = target.state
            target.state = "active"
            target.role = "admin"
            target.prior_state = None
            target.deletion_requested_at = None
    return was


async def clear_factor(email: str) -> bool:
    """Take the second factor off one account, from the machine.

    An account that enrolled a factor and then lost both the authenticator and
    every recovery code cannot be helped over HTTP: a route that removes a
    factor without presenting one is exactly the route worth stealing a
    session for. So this is a command here, like every other way back in.

    It ends the account's sessions as well. Somebody running this has lost
    control of the second factor, and whether anybody else has hold of the
    account is precisely what nobody knows.
    """
    assert db.session_factory is not None
    async with db.session_factory() as session:
        target = (await session.execute(
            select(User).where(func.lower(func.btrim(User.email)) == email.strip().lower())
        )).scalar_one_or_none()
    if target is None:
        raise LookupError(f"no account holds {email}")
    async with db.session_factory() as session:
        async with session.begin():
            # The key the enrolment routes take, from the same function so the
            # two processes lock the same thing, and taken first, before the
            # row lock below. An account with no factor yet leaves that row
            # lock holding nothing, so this is what keeps a first enrolment
            # and this command apart.
            #
            # No lock_timeout here, unlike the routes: this is one short
            # transaction in a process of its own with no connection pool to
            # starve, and an operator at a terminal can wait.
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                  {"key": factors._budget_lock(target.id)})
            await session.execute(
                select(AuthFactor.id).where(AuthFactor.user_id == target.id)
                .with_for_update())
            removed = (await session.execute(
                delete(AuthFactor).where(AuthFactor.user_id == target.id)
                .returning(AuthFactor.id)
            )).first()
            if removed is None:
                # Nothing to clear, so nothing is written: not the codes, not
                # the sessions, and not a record saying a second factor was
                # taken off an account that never had one. Under the lock
                # above that is the account's answer for the whole
                # transaction rather than a guess about one moment in it.
                return False
            await session.execute(
                delete(RecoveryCode).where(RecoveryCode.user_id == target.id))
            await session.execute(
                update(Session).where(Session.user_id == target.id,
                                      Session.revoked_at.is_(None))
                .values(revoked_at=func.now()))
    await sessions.close_sockets(target.id)
    # Actorless and high: nobody signed in did this, and a second factor
    # disappearing is the kind of thing somebody should be able to find later.
    await audit.record("factor.cleared", target_user_id=target.id, severity="high")
    return True


async def rotate_keys() -> dict:
    """Re-encrypt everything sealed with an older root key under the newest.

    ROOT_KEYS carries every version so a running install can still read what
    the old key sealed. Nothing moves on its own: this walks the stored
    ciphertext, rewrites each blob under the active version, and only then is
    the old key safe to remove.
    """
    assert db.session_factory is not None
    ring = keyring.get_key_ring()
    moved = 0
    async with db.session_factory() as session:
        async with session.begin():
            factors = list((await session.execute(
                select(AuthFactor).where(AuthFactor.key_version != ring.active_version)
            )).scalars().all())
            for factor in factors:
                stored = ring.version_of(factor.secret_ciphertext)
                if stored not in ring.versions:
                    # Refused, not skipped and not dropped: the secret behind
                    # this blob is somebody's second factor, and an install
                    # that rewrites what it cannot read has destroyed it.
                    raise keyring.KeyRingError(
                        f"root key version {stored} is missing; add it to ROOT_KEYS "
                        f"before rotating, or that factor can never be read again")
                factor.secret_ciphertext = ring.reencrypt(
                    TOTP_PURPOSE, factor.secret_ciphertext, factor.user_id.bytes)
                factor.key_version = ring.active_version
                moved += 1
            await session.execute(
                text("UPDATE installation_auth_state SET root_key_version = :version WHERE id = 1"),
                {"version": ring.active_version})
    return {"reencrypted": moved, "active_version": ring.active_version}


async def retired_versions() -> list[int]:
    """The key versions this install still has something sealed under.

    What `rotate-keys --check` answers before an operator deletes a key from
    ROOT_KEYS: an empty list is the only safe moment to remove one.
    """
    assert db.session_factory is not None
    ring = keyring.get_key_ring()
    async with db.session_factory() as session:
        versions = (await session.execute(
            select(AuthFactor.key_version).distinct()
        )).scalars().all()
    return sorted(version for version in versions if version != ring.active_version)


async def _local_user(session: AsyncSession) -> uuid.UUID:
    local = (await session.execute(
        select(User.id).where(User.email == db.LOCAL_USER_EMAIL))).scalar_one_or_none()
    if local is None:
        raise LookupError("this install has no implicit local user to collapse onto")
    return local


def _configured() -> dict:
    """What mail and OAuth would do if the API started right now.

    Guided configuration is reading before writing: an operator who cannot see
    the current answer edits the file until something works.
    """
    from app import mail, oauth

    settings = get_settings()
    report: dict = {"public_url": settings.public_url, "email_backend": settings.email_backend,
                    "auth_methods": settings.auth_methods}
    for name, check in (("mail", mail.check_configuration), ("oauth", oauth.check_configuration)):
        try:
            check(settings)
            report[name] = "ok"
        except RuntimeError as refused:
            report[name] = str(refused)
    return report


async def _connected(action):
    if not await db.connect(serving=False):
        raise SystemExit("could not reach PostgreSQL; is the database up?")
    try:
        return await action()
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.operator",
                                     description="offline operator commands")
    commands = parser.add_subparsers(dest="command", required=True)
    collapsing = commands.add_parser("collapse", help="turn accounts off, destroying them")
    collapsing.add_argument("--confirm", default="", help=f'must be "{COLLAPSE_PHRASE}"')
    reclaiming = commands.add_parser("reclaim", help="get back into an install")
    reclaiming.add_argument("--claim", action="store_true", help="mint a fresh setup link")
    reclaiming.add_argument("--restore", metavar="EMAIL", help="make one account an active admin")
    rotating = commands.add_parser("rotate-keys", help="re-encrypt under the newest root key")
    rotating.add_argument("--check", action="store_true",
                          help="report which older versions are still in use")
    clearing = commands.add_parser("clear-factor",
                                   help="remove one account's second factor")
    clearing.add_argument("email")
    commands.add_parser("configure", help="what mail and OAuth would do right now")
    parsed = parser.parse_args(argv)

    if parsed.command == "collapse":
        _run_collapse(parsed.confirm)
    elif parsed.command == "reclaim":
        _run_reclaim(parsed)
    elif parsed.command == "rotate-keys":
        _run_rotate(parsed.check)
    elif parsed.command == "clear-factor":
        _run_clear_factor(parsed.email)
    else:
        for key, value in _configured().items():
            print(f"{key}: {value}")


def _run_clear_factor(email: str) -> None:
    try:
        removed = asyncio.run(_connected(lambda: clear_factor(email)))
    except LookupError as missing:
        raise SystemExit(str(missing)) from missing
    if not removed:
        print(f"{email} had no second factor; nothing to remove.")
        return
    print(f"Removed the second factor on {email}, its recovery codes, and every")
    print("session it had open. They sign in with their password alone now,")
    print("and can enrol again whenever they like.")


def _run_collapse(confirmation: str) -> None:
    try:
        counted = asyncio.run(_connected(lambda: collapse(confirmation)))
    except ValueError as refused:
        raise SystemExit(
            f'{refused}\n\nThis destroys every account, every session, every second factor\n'
            f'and every invitation on this installation. The generations and\n'
            f'assets stay and move to the single local user.\n\n'
            f'  make auth-collapse CONFIRM="{COLLAPSE_PHRASE}"') from None
    print(f"accounts off: {counted['accounts']} accounts destroyed, "
          f"{counted['generations']} generations kept and moved to the local user")
    print("Set AUTH_MODE=none in deploy/compose/.env and restart the API.")


def _run_reclaim(parsed: argparse.Namespace) -> None:
    if parsed.claim == bool(parsed.restore):
        raise SystemExit("choose one: reclaim --claim, or reclaim --restore EMAIL")
    if parsed.restore:
        try:
            was = asyncio.run(_connected(lambda: reclaim_restore(parsed.restore)))
        except LookupError as missing:
            raise SystemExit(str(missing)) from None
        print(f"{parsed.restore} is an active administrator again (it was {was}).")
        print("Its old sessions are still revoked; it signs in from the login page.")
        return
    try:
        token = asyncio.run(_connected(reclaim_claim))
    except LookupError as refused:
        raise SystemExit(str(refused)) from None
    base = get_settings().public_url.rstrip("/")
    print()
    print("Whoever spends this becomes an administrator of this installation.")
    print("Any setup link still outstanding was retired to mint it.")
    print()
    print(f"  curl -X POST {base}/api/v1/auth/setup \\")
    print("    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"token\": \"{token}\",")
    print("         \"email\": \"you@example.com\",")
    print("         \"password\": \"a password of fifteen characters or more\"}'")
    print()
    print("One use, one hour. A password typed on a command line reaches your")
    print("shell history.")


def _run_rotate(check_only: bool) -> None:
    if check_only:
        stale = asyncio.run(_connected(retired_versions))
        if not stale:
            print("nothing is sealed under an older key; every retired version can be removed")
        else:
            print("still in use: " + ", ".join(str(version) for version in stale))
            print("Run rotate-keys without --check before removing those from ROOT_KEYS.")
        return
    try:
        result = asyncio.run(_connected(rotate_keys))
    except keyring.KeyRingError as refused:
        raise SystemExit(str(refused)) from None
    print(f"re-encrypted {result['reencrypted']} secrets under root key version "
          f"{result['active_version']}")
    print("Check with rotate-keys --check before removing the old key from ROOT_KEYS.")


if __name__ == "__main__":
    main()
