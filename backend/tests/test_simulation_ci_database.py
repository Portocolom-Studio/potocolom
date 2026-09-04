"""Simulation must not share the developer database with local auth-enable."""

import asyncio
import importlib.util
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ci_database():
    path = ROOT / "scripts" / "ci_database.py"
    spec = importlib.util.spec_from_file_location("ci_database", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_database = _ci_database()
DEVELOPER_DATABASE = ci_database.DEVELOPER_DATABASE
CI_DATABASE = ci_database.CI_DATABASE
DEVELOPER_URL = f"postgresql://potocolom:potocolom@localhost:5432/{DEVELOPER_DATABASE}"
CI_URL = f"postgresql://potocolom:potocolom@localhost:5432/{CI_DATABASE}"


def test_database_name_reads_the_path():
    assert ci_database.database_name(DEVELOPER_URL) == DEVELOPER_DATABASE
    assert ci_database.database_name(CI_URL) == CI_DATABASE
    assert ci_database.database_name(CI_URL + "?sslmode=disable") == CI_DATABASE


def test_ci_refuses_the_developer_database():
    with pytest.raises(SystemExit, match="must not use the developer database"):
        ci_database.refuse_developer_database(url=DEVELOPER_URL, ci=True)


def test_ci_accepts_the_ci_database():
    ci_database.refuse_developer_database(url=CI_URL, ci=True)


def test_a_local_run_may_use_the_developer_database():
    ci_database.refuse_developer_database(url=DEVELOPER_URL, ci=False)


def test_simulate_calls_the_developer_database_guard():
    source = (ROOT / "scripts" / "simulate.py").read_text()
    assert "refuse_developer_database" in source
    assert "prepare_ci_database" in source


def test_simulation_workflow_sets_the_ci_database():
    source = (ROOT / ".github" / "workflows" / "simulation.yml").read_text()
    assert "DATABASE_URL" in source
    assert CI_URL in source


class _Catalog:
    def __init__(self, *, exists=None, create_error=None):
        self._exists = exists
        self._create_error = create_error
        self.executed = []

    async def fetchval(self, query, *args):
        assert "pg_database" in query
        return self._exists

    async def execute(self, query):
        self.executed.append(query)
        if self._create_error is not None:
            raise self._create_error

    async def close(self):
        return None


class _Target:
    def __init__(self):
        self.executed = []

    async def fetchval(self, query, *args):
        assert "to_regclass" in query
        return "installation_auth_state"

    async def execute(self, query):
        self.executed.append(query)

    async def close(self):
        return None


def _stub_connect(monkeypatch, catalog, target):
    async def connect(**kwargs):
        if kwargs.get("database") == "postgres":
            return catalog
        return target

    monkeypatch.setattr(asyncpg, "connect", connect)


def test_prepare_treats_a_duplicate_create_as_already_there(monkeypatch):
    catalog = _Catalog(
        exists=None,
        create_error=asyncpg.DuplicateDatabaseError(
            'database "potocolom_ci" already exists'
        ),
    )
    target = _Target()
    _stub_connect(monkeypatch, catalog, target)
    asyncio.run(ci_database._prepare(CI_URL))
    assert target.executed == [
        "UPDATE installation_auth_state SET auth_mode = 'none', "
        "root_key_version = NULL WHERE id = 1"
    ]


def test_prepare_still_fails_on_an_unrelated_create_error(monkeypatch):
    catalog = _Catalog(exists=None, create_error=asyncpg.CannotConnectNowError("starting"))
    target = _Target()
    _stub_connect(monkeypatch, catalog, target)
    with pytest.raises(asyncpg.CannotConnectNowError):
        asyncio.run(ci_database._prepare(CI_URL))
    assert target.executed == []
