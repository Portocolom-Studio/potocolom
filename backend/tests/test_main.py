"""Self-hosted SPA static serving and the AUTH_MODE startup gate."""

import importlib
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

import app.main as main_module
from app.main import SPAStaticFiles, app
from app.settings import get_settings


@pytest.mark.parametrize("mode", ["local", "oauth"])
def test_retired_auth_mode_fails_settings_validation(monkeypatch, mode):
    monkeypatch.setenv("AUTH_MODE", mode)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as error:
            get_settings()
        assert mode in str(error.value)
        assert "none" in str(error.value)
        assert "accounts" in str(error.value)
    finally:
        get_settings.cache_clear()


def test_accounts_mode_imports_and_authenticates_nobody(monkeypatch):
    """Accounts mode boots now, and the guard that used to be an import-time
    refusal moved to the principal: nothing resolves to the implicit admin."""
    monkeypatch.setenv("AUTH_MODE", "accounts")
    get_settings.cache_clear()
    try:
        importlib.reload(main_module)
        assert get_settings().auth_mode == "accounts"
    finally:
        monkeypatch.delenv("AUTH_MODE", raising=False)
        get_settings.cache_clear()
        importlib.reload(main_module)


FLEET_TOKEN_KEY_UNSET = (
    "FLEET_TOKEN_KEY is unset; refusing fleet handshakes. "
    "Run scripts/preflight.sh to write deploy/compose/.env, "
    "then set FLEET_TOKEN_KEY from FLEET_SECRET."
)


def test_unset_fleet_token_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("FLEET_TOKEN_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=re.escape(FLEET_TOKEN_KEY_UNSET)):
            with TestClient(app):
                pass
    finally:
        monkeypatch.setenv("FLEET_TOKEN_KEY", "test-fleet-token")
        get_settings.cache_clear()


def test_unset_fleet_token_key_cannot_even_import(monkeypatch):
    monkeypatch.delenv("FLEET_TOKEN_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=re.escape(FLEET_TOKEN_KEY_UNSET)):
            importlib.reload(main_module)
    finally:
        monkeypatch.setenv("FLEET_TOKEN_KEY", "test-fleet-token")
        get_settings.cache_clear()
        importlib.reload(main_module)


def test_auth_mode_none_starts(monkeypatch):
    """AUTH_MODE=none is the only implemented mode and must not be refused.

    The full lifespan can run here without a database: an unreachable one
    degrades startup (app/db.py) instead of raising, so entering the lifespan
    asserts the gate itself.
    """
    monkeypatch.setenv("AUTH_MODE", "none")
    get_settings.cache_clear()
    try:
        with TestClient(app):
            pass
    finally:
        get_settings.cache_clear()


def test_auth_mode_unset_starts(monkeypatch):
    """An unset AUTH_MODE falls back to the shipped none default, not a refusal."""
    monkeypatch.delenv("AUTH_MODE", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app):
            pass
    finally:
        get_settings.cache_clear()


def _prerendered_build(tmp_path: Path) -> Path:
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>landing</title>")
    (dist / "login.html").write_text("<!doctype html><title>sign in</title>")
    (dist / "404.html").write_text("<!doctype html><title>not found</title>")
    (dist / "asset.txt").write_text("asset")
    (dist / "folder.html").mkdir()
    return dist


def test_prerendered_page_is_served_for_its_route(tmp_path: Path):
    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=_prerendered_build(tmp_path), html=True))

    with TestClient(app) as client:
        for path in ("/", "/index.html"):
            response = client.get(path)
            assert response.status_code == 200
            assert "landing" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        for path in ("/login", "/login/"):
            response = client.get(path)
            assert response.status_code == 200
            assert "sign in" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        assert client.head("/login").status_code == 200
        # The contract is that a hashed asset is not forced to revalidate, not
        # that the framework omits the header entirely.
        asset = client.get("/asset.txt")
        assert "no-cache" not in asset.headers.get("Cache-Control", "")


def test_unknown_path_is_the_not_found_page(tmp_path: Path):
    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=_prerendered_build(tmp_path), html=True))

    with TestClient(app) as client:
        for path in ("/nope", "/app/generate", "/api/v1/no-such-endpoint"):
            response = client.get(path)
            assert response.status_code == 404
            assert "not found" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        assert client.head("/nope").status_code == 404
        assert client.get("/folder").status_code == 404
        for malformed in ("/nope%00", "/" + "x" * 5000):
            assert client.get(malformed).status_code == 404
