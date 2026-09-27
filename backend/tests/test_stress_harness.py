"""The stress harness gives concurrent sockets separate account principals."""

import argparse
import asyncio
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("stress_harness", ROOT / "scripts/stress.py")
assert SPEC is not None and SPEC.loader is not None
stress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stress)


def arguments(**changes):
    values = {"sessions": 48, "workers": 4, "slots": 8, "churn": 200}
    values.update(changes)
    return argparse.Namespace(**values)


def test_account_pool_covers_the_largest_concurrent_scenario():
    assert stress.accounts_needed(arguments()) == 48
    assert stress.accounts_needed(arguments(sessions=2, workers=2, slots=2, churn=200)) == 20


def test_open_all_gives_each_concurrent_browser_a_distinct_account(monkeypatch):
    seen = []

    class Browser:
        def __init__(self, args, account):
            self.account = account
            seen.append(account)

        async def open(self):
            return True

    accounts = [stress.Account({"potocolom_session": str(number)}, "csrf")
                for number in range(6)]
    monkeypatch.setattr(stress, "Browser", Browser)
    ready, refused = asyncio.run(stress.open_all(argparse.Namespace(accounts=accounts), 6))

    assert len(ready) == 6
    assert refused == []
    assert seen == accounts


def test_database_cleanup_refuses_a_name_the_harness_did_not_generate(monkeypatch):
    assert stress.DATABASE_NAME.fullmatch("potocolom_stress_0123abcd_123_a1b2c3")
    called = False

    async def connect(url, database):
        nonlocal called
        called = True

    monkeypatch.setattr(stress, "database_connection", connect)
    for name in ("potocolom", "potocolom_test_0123abcd"):
        try:
            asyncio.run(stress.drop_database("postgresql://localhost/postgres", name))
        except RuntimeError as error:
            assert "refusing to drop" in str(error)
        else:
            raise AssertionError("unsafe database name was accepted")
    assert not called


def test_a_scenario_error_makes_the_run_fail(monkeypatch):
    async def broken(args, rng, report):
        raise RuntimeError("broken assertion")

    monkeypatch.setattr(stress, "broken", broken, raising=False)
    args = argparse.Namespace(scenario=["broken"], seed=1)
    assert asyncio.run(stress.run_scenarios(args)) == 1
