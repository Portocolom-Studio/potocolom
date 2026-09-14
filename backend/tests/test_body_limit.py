"""Request bodies are capped before FastAPI parses them."""

import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.body_limit import (
    MAX_JSON_BODY_BYTES,
    TOO_LARGE,
    RequestBodyLimitMiddleware,
    max_body_bytes,
)
from app.files import MAX_UPLOAD_BYTES
from app.main import app
from app.security import SECURITY_HEADERS, SecurityHeadersMiddleware
from app.ses_feedback import MAX_BODY_BYTES


def _chunked(*parts: bytes):
    for part in parts:
        yield part


def _assert_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, (
            f"{name}: expected {value!r}, got {response.headers.get(name)!r}"
        )


def test_max_body_bytes_uses_the_path_caps():
    assert max_body_bytes("/api/v1/shared") == MAX_JSON_BODY_BYTES
    assert max_body_bytes("/api/v1/auth/reset") == MAX_JSON_BODY_BYTES
    assert max_body_bytes("/api/v1/files/u/j.png") == MAX_UPLOAD_BYTES
    assert max_body_bytes("/api/v1/mail/feedback") == MAX_BODY_BYTES
    assert MAX_JSON_BODY_BYTES < MAX_UPLOAD_BYTES
    assert MAX_BODY_BYTES < MAX_JSON_BODY_BYTES


def _limited_app(monkeypatch, json_limit: int = 16):
    monkeypatch.setattr("app.body_limit.MAX_JSON_BODY_BYTES", json_limit)
    ran: list[str] = []
    inner = FastAPI()
    inner.add_middleware(RequestBodyLimitMiddleware)
    inner.add_middleware(SecurityHeadersMiddleware)

    async def mark() -> None:
        ran.append("dep")

    @inner.post("/api/v1/shared")
    async def shared(request: Request, _: None = Depends(mark)) -> dict:
        ran.append("handler")
        await request.body()
        return {"ok": True}

    @inner.put("/api/v1/files/{key:path}")
    async def upload(key: str, request: Request) -> dict:
        ran.append("handler")
        async for _chunk in request.stream():
            pass
        return {"stored": key}

    @inner.post("/api/v1/mail/feedback")
    async def feedback(request: Request) -> dict:
        ran.append("handler")
        await request.body()
        return {"ok": True}

    return inner, ran


def test_fixed_length_over_the_json_limit_is_413_before_the_route(monkeypatch):
    inner, ran = _limited_app(monkeypatch, json_limit=16)
    with TestClient(inner) as client:
        response = client.post(
            "/api/v1/shared",
            content=b'{"token":"' + b"a" * 32 + b'"}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    _assert_security_headers(response)
    assert ran == []


def test_fixed_length_at_the_json_limit_reaches_the_route(monkeypatch):
    inner, ran = _limited_app(monkeypatch, json_limit=16)
    with TestClient(inner) as client:
        response = client.post(
            "/api/v1/shared",
            content=b"{" + b"a" * 14 + b"}",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200
    assert ran == ["dep", "handler"]


def test_chunked_over_the_json_limit_is_413_before_the_route(monkeypatch):
    inner, ran = _limited_app(monkeypatch, json_limit=16)
    with TestClient(inner) as client:
        response = client.post(
            "/api/v1/shared",
            content=_chunked(b'{"token":"', b"a" * 32, b'"}'),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    assert ran == []


def test_chunked_under_the_json_limit_reaches_the_route(monkeypatch):
    inner, ran = _limited_app(monkeypatch, json_limit=64)
    with TestClient(inner) as client:
        response = client.post(
            "/api/v1/shared",
            content=_chunked(b'{"token":"', b"x", b'"}'),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200
    assert ran == ["dep", "handler"]


def test_ses_feedback_uses_the_sns_cap_not_the_json_cap(monkeypatch):
    monkeypatch.setattr("app.ses_feedback.MAX_BODY_BYTES", 8)
    inner, ran = _limited_app(monkeypatch, json_limit=64)
    with TestClient(inner) as client:
        ok = client.post("/api/v1/mail/feedback", content=b"x" * 8)
        too_big = client.post("/api/v1/mail/feedback", content=b"x" * 9)
    assert ok.status_code == 200
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": TOO_LARGE}
    assert ran == ["handler"]


def test_chunked_file_upload_over_the_upload_cap_is_413(monkeypatch):
    monkeypatch.setattr("app.files.MAX_UPLOAD_BYTES", 8)
    inner, ran = _limited_app(monkeypatch, json_limit=64)
    with TestClient(inner) as client:
        response = client.put(
            "/api/v1/files/u/j.png",
            content=_chunked(b"x" * 5, b"y" * 5),
        )
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    assert ran == ["handler"]


def test_file_upload_uses_the_upload_cap_not_the_json_cap(monkeypatch):
    monkeypatch.setattr("app.files.MAX_UPLOAD_BYTES", 32)
    inner, ran = _limited_app(monkeypatch, json_limit=8)
    with TestClient(inner) as client:
        ok = client.put("/api/v1/files/u/j.png", content=b"x" * 32)
        too_big = client.put("/api/v1/files/u/j.png", content=b"x" * 33)
    assert ok.status_code == 200
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": TOO_LARGE}
    assert ran == ["handler"]


def test_shared_oversized_body_is_413_on_the_real_app(monkeypatch):
    """The public share resolver is the unauthenticated JSON entry point."""
    monkeypatch.setattr("app.body_limit.MAX_JSON_BODY_BYTES", 32)
    with TestClient(app) as client:
        response = client.post("/api/v1/shared", json={"token": "a" * 64})
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    _assert_security_headers(response)


def test_body_limit_skips_websocket():
    seen: list[str] = []

    async def raw_app(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})

    wrapped = RequestBodyLimitMiddleware(raw_app)

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
