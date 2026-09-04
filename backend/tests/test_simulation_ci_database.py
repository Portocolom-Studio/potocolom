"""Simulation must not share the developer database with local auth-enable."""

import importlib.util
from pathlib import Path

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
