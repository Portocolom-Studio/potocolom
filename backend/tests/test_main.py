"""Self-hosted SPA static serving."""

from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.main import SPAStaticFiles


def test_spa_static_files_fallback_to_index(tmp_path: Path):
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>potocolom</title>")

    app = Starlette()
    app.mount("/", SPAStaticFiles(directory=dist, html=True))

    with TestClient(app) as client:
        for path in ("/app", "/app/generate", "/whitepaper"):
            response = client.get(path)
            assert response.status_code == 200
            assert "potocolom" in response.text
