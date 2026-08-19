"""Self-hosted SPA static serving and the AUTH_MODE startup gate."""

import importlib
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import app.main as main_module
from app.main import SPAStaticFiles, app
from app.settings import get_settings


@pytest.mark.parametrize("mode", ["local", "oauth"])
def test_unimplemented_auth_mode_refuses_to_start(monkeypatch, mode):
    """An unimplemented AUTH_MODE must fail startup loudly, not silently downgrade.

    current_user never reads the mode and resolves every request to the local
    admin user, so leaving local or oauth configured would serve the API with
    no authentication at all. The lifespan raises on entry, before the rest of
    the process comes up.
    """
    monkeypatch.setenv("AUTH_MODE", mode)
    get_settings.cache_clear()
    try:
        # Match the mode as well as the variable: a message naming a mode the
        # operator never set would satisfy the looser check while telling them
        # to fix the wrong thing.
        with pytest.raises(RuntimeError, match=f"AUTH_MODE={mode}"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_unimplemented_auth_mode_cannot_even_import(monkeypatch):
    """An unimplemented AUTH_MODE must refuse to import, not just to start.

    The ASGI lifespan protocol is optional: uvicorn --lifespan off and a
    TestClient outside a context manager serve the app without it, so a gate
    that runs only inside lifespan is bypassable. Importing the module is
    unavoidable, so the gate must fire there too.
    """
    monkeypatch.setenv("AUTH_MODE", "local")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="AUTH_MODE=local"):
            importlib.reload(main_module)
    finally:
        monkeypatch.delenv("AUTH_MODE", raising=False)
        get_settings.cache_clear()
        # Reload with the environment restored so the module left in
        # sys.modules is the good one and later tests are not poisoned.
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


def test_spa_static_files_fallback_to_index(tmp_path: Path):
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>potocolom</title>")
    (dist / "asset.txt").write_text("asset")

    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=dist, html=True))

    with TestClient(app) as client:
        for path in ("/", "/index.html"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-cache"
        for path in ("/app", "/app/generate", "/whitepaper"):
            response = client.get(path)
            assert response.status_code == 200
            assert "potocolom" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        # The contract is that a hashed asset is not forced to revalidate, not
        # that the framework omits the header entirely.
        asset = client.get("/asset.txt")
        assert "no-cache" not in asset.headers.get("Cache-Control", "")
        # API paths must stay 404s, never the SPA shell.
        for path in ("/api", "/api/v1/no-such-endpoint"):
            response = client.get(path)
            assert response.status_code == 404


def test_spa_fallback_wins_over_a_shipped_404_document(tmp_path: Path):
    """A build carrying 404.html must not shadow the client-side routes.

    StaticFiles(html=True) answers a miss with that document and a 404 status
    rather than raising, so the fallback has to inspect the response too.
    """
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>potocolom</title>")
    (dist / "404.html").write_text("<!doctype html><title>not found</title>")

    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=dist, html=True))

    with TestClient(app) as client:
        for path in ("/app", "/app/generate", "/benchmark"):
            response = client.get(path)
            assert response.status_code == 200
            assert "potocolom" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        for path in ("/api", "/api/v1/no-such-endpoint"):
            assert client.get(path).status_code == 404
