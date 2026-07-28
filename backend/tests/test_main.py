"""Self-hosted SPA static serving."""

from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.main import SPAStaticFiles


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
        assert "Cache-Control" not in client.get("/asset.txt").headers
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
