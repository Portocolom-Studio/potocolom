"""Self-hosted SPA static serving and the AUTH_MODE startup gate."""

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

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
        with pytest.raises(RuntimeError, match="AUTH_MODE"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


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
