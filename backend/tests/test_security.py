"""Security response headers on API, static, SPA fallback, and error paths."""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import SPAStaticFiles, app
from app.security import (
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
    unhandled_exception_response,
)

client = TestClient(app)

# Repo-root-relative path to the Cloudflare Pages header file that must stay
# byte-for-byte aligned with SECURITY_HEADERS.
_HEADERS_FILE = (
    Path(__file__).resolve().parents[2] / "frontend" / "static" / "_headers"
)


def _assert_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, (
            f"{name}: expected {value!r}, got {response.headers.get(name)!r}"
        )


def test_security_headers_on_health():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    _assert_security_headers(response)


def test_interactive_api_docs_disabled():
    """CDN-backed /docs and /redoc are off; OpenAPI JSON stays available."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "openapi" in openapi.json()
    _assert_security_headers(openapi)


def test_security_headers_on_api_404():
    response = client.get("/api/v1/no-such-endpoint")
    assert response.status_code == 404
    _assert_security_headers(response)


def test_security_headers_on_unhandled_500():
    """ServerErrorMiddleware is outside user middleware; the Exception handler covers 500s."""
    # Production wiring: if main.py drops the handler, this fails even when a
    # miniature app below is configured correctly.
    assert app.exception_handlers.get(Exception) is unhandled_exception_response

    inner = FastAPI()
    inner.add_middleware(SecurityHeadersMiddleware)
    inner.add_exception_handler(Exception, unhandled_exception_response)

    @inner.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(inner, raise_server_exceptions=False) as boom_client:
        response = boom_client.get("/boom")
        assert response.status_code == 500
        _assert_security_headers(response)


def test_security_headers_on_static_and_spa_fallback(tmp_path: Path):
    dist = tmp_path / "static"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>potocolom</title>")
    (dist / "asset.txt").write_text("ok")

    spa = FastAPI()
    spa.add_middleware(SecurityHeadersMiddleware)
    spa.mount("/", SPAStaticFiles(directory=dist, html=True), name="frontend")

    with TestClient(spa) as spa_client:
        static = spa_client.get("/asset.txt")
        assert static.status_code == 200
        assert static.text == "ok"
        _assert_security_headers(static)

        fallback = spa_client.get("/app/generate")
        assert fallback.status_code == 200
        assert "potocolom" in fallback.text
        _assert_security_headers(fallback)


def test_security_headers_skip_websocket():
    """Middleware must not wrap WebSocket scopes (no HTTP response headers)."""
    seen: list[str] = []

    async def raw_app(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})

    wrapped = SecurityHeadersMiddleware(raw_app)

    async def receive():
        return {"type": "websocket.connect"}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    asyncio.run(
        wrapped(
            {"type": "websocket", "path": "/api/v1/fleet", "headers": []},
            receive,
            send,
        )
    )
    assert seen == ["websocket"]
    assert all(m["type"].startswith("websocket.") for m in messages)


def test_cloudflare_headers_byte_for_byte_aligned():
    text = _HEADERS_FILE.read_text()
    # Parse the /* block only; /images/* keeps its own cache rule.
    block: dict[str, str] = {}
    in_star = False
    for line in text.splitlines():
        if line.strip() == "/*":
            in_star = True
            continue
        if in_star:
            if not line.startswith("  ") or not line.strip() or line.lstrip().startswith("#"):
                if block:
                    break
                continue
            name, _, value = line.strip().partition(": ")
            block[name] = value

    assert block == SECURITY_HEADERS
