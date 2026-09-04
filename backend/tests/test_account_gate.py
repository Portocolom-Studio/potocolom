"""The per-account gate every overlapping auth transaction takes first."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import db
from app.main import app
from tests.test_totp_flow import ORIGIN, PASSWORD, _csrf, _login, _make, accounts

__all__ = ["accounts"]

ROOT = Path(__file__).resolve().parents[1] / "app"
LAST_ADMIN_LOCK = 184468

SITES = {
    "credentials.py": 3,
    "recovery.py": 3,
    "deletion.py": 4,
    "roles.py": 1,
    "states.py": 1,
    "oauth.py": 1,
    "operator.py": 2,
}


def test_every_listed_site_takes_the_account_gate():
    """Deleting a hold_the_account call from a listed site goes red.

    The same shape as test_every_route_takes_the_factor_before_the_codes:
    asserted on the call sites, because staging a real deadlock hangs a suite
    rather than failing it. The measured pairing lives in
    test_collapse_waits_behind_a_password_change_instead_of_deadlocking.
    """
    for name, count in SITES.items():
        source = (ROOT / name).read_text()
        found = source.count("hold_the_account(")
        assert found == count, (name, found)
    collapse = (ROOT / "operator.py").read_text()
    assert "wait=True" in collapse
    assert "order_by(User.id)" in collapse
    lock = (ROOT / "account_lock.py").read_text()
    assert "SET LOCAL lock_timeout = '0'" in lock
    wait_arm = lock[lock.index("if wait:"): lock.index("try:")]
    assert "SET LOCAL lock_timeout = '0'" in wait_arm


def _param_key(parameters: object) -> object:
    if parameters is None:
        return None
    if isinstance(parameters, dict):
        return parameters.get("key", parameters.get("key_1"))
    if isinstance(parameters, (list, tuple)) and parameters:
        return parameters[0]
    return None


def _first_lock(statements: list[tuple[str, object]]) -> str | None:
    """account, last-admin, or row: whichever lock this transaction took first."""
    for statement, parameters in statements:
        lowered = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in lowered:
            return "last-admin" if _param_key(parameters) == LAST_ADMIN_LOCK else "account"
        locking = (lowered.startswith(("update", "delete", "insert"))
                   or (lowered.startswith("select") and "for update" in lowered))
        if locking:
            return "row"
    return None


@pytest.mark.db
@pytest.mark.parametrize("change", ["password", "role", "state", "deletion"])
def test_every_overlapping_route_takes_the_account_gate_before_the_rows(accounts, change):
    """First lock is the per-account key, or collapse can still cycle.

    A plain SELECT is not a lock: counting one would let a route that reads
    the user and then locks identities pass with the gate removed.
    """
    transactions: list[list[tuple[str, object]]] = []
    open_now: dict[int, list[tuple[str, object]]] = {}

    def record(conn, cursor, statement, parameters, context, executemany):
        open_now.setdefault(id(conn), []).append((statement, parameters))

    def close(conn):
        transactions.append(open_now.pop(id(conn), []))

    with TestClient(app, base_url=ORIGIN) as client:
        engine = db.engine.sync_engine
        if change in ("role", "state"):
            actor = "gate-actor@example.com"
            client.portal.call(_make, actor, "admin")
            target = client.portal.call(_make, "gate-target@example.com")
        else:
            actor = "gate-self@example.com"
            target = client.portal.call(_make, actor)
        assert _login(client, actor).status_code == 204
        listeners = (("before_cursor_execute", record), ("commit", close),
                     ("rollback", close))
        for name, handler in listeners:
            event.listen(engine, name, handler)
        try:
            if change == "password":
                answered = client.post(
                    "/api/v1/account/password", headers=_csrf(client),
                    json={"current_password": PASSWORD,
                          "password": "another-long-enough-password"})
            elif change == "role":
                answered = client.post(f"/api/v1/users/{target.id}/role",
                                       headers=_csrf(client),
                                       json={"role": "admin", "attested": True})
            elif change == "state":
                answered = client.post(f"/api/v1/users/{target.id}/state",
                                       headers=_csrf(client),
                                       json={"state": "suspended"})
            else:
                answered = client.request("DELETE", "/api/v1/account",
                                          headers=_csrf(client))
            assert answered.status_code == 204, answered.text
        finally:
            for name, handler in listeners:
                event.remove(engine, name, handler)

    firsts = [
        first for statements in transactions + list(open_now.values())
        if any(
            "pg_advisory_xact_lock" in statement.lower()
            and _param_key(parameters) != LAST_ADMIN_LOCK
            for statement, parameters in statements
        )
        for first in [_first_lock(statements)]
        if first is not None
    ]
    assert firsts, "no transaction took the account gate, so this proved nothing"
    assert all(first == "account" for first in firsts), firsts
