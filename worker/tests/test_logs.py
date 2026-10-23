import asyncio
import io
import json
import logging
import uuid

import httpx
import pytest

from worker.logs import setup_logging


@pytest.mark.parametrize("log_format", ["plain", "json"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("status", [200, 403])
def test_http_logs_do_not_expose_signed_url_credentials(
    log_format: str, method: str, status: int
) -> None:
    canaries = [f"url-canary-{uuid.uuid4().hex}" for _ in range(4)]
    url = (
        f"https://user-{canaries[0]}:pass-{canaries[1]}@storage.invalid"
        f"/worker-assets/output-42.webp?token={canaries[3]}%2Fencoded"
        f"&expires=123#fragment-{canaries[2]}"
    )
    output = io.StringIO()
    setup_logging(log_format)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    old_stream = handler.stream
    handler.setStream(output)
    httpx_logger = logging.getLogger("httpx")
    old_level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        return httpx.Response(status, request=request)

    try:
        async def make_request() -> None:
            try:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as client:
                    response = await client.request(method, url, content=b"image")
                    response.raise_for_status()
            except httpx.HTTPStatusError as error:
                try:
                    raise RuntimeError("upload wrapper failed") from error
                except RuntimeError:
                    logging.getLogger("potocolom.worker.jobs").exception(
                        "job job-canary-7 upload failed"
                    )

        asyncio.run(make_request())
        logging.getLogger("potocolom.worker.jobs").info(
            "job job-canary-7 ordinary status=%d", status
        )
    finally:
        httpx_logger.setLevel(old_level)
        handler.setStream(old_stream)

    lines = output.getvalue()
    for canary in canaries:
        assert canary not in lines, lines
    assert "storage.invalid" in lines, lines
    assert "/worker-assets/output-42.webp" in lines, lines
    assert f"HTTP Request: {method} https://" in lines, lines
    assert "job job-canary-7 ordinary status=" in lines, lines
    if status == 403:
        assert "403" in lines, lines
        assert "HTTPStatusError" in lines, lines
        assert "The above exception was the direct cause" in lines, lines
        assert "upload wrapper failed" in lines, lines
    else:
        assert "HTTPStatusError" not in lines, lines

    if log_format == "json":
        entries = [json.loads(line) for line in lines.splitlines()]
        assert any(
            entry["message"].startswith(f"HTTP Request: {method} https://")
            for entry in entries
        )
        assert any("ordinary status=" in entry["message"] for entry in entries)


@pytest.mark.parametrize("log_format", ["plain", "json"])
@pytest.mark.parametrize("extra_userinfo", [False, True])
def test_malformed_signed_url_cannot_bypass_log_redaction(
    log_format: str, extra_userinfo: bool
) -> None:
    secret = "malformed-" + uuid.uuid4().hex
    output = io.StringIO()
    setup_logging(log_format)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    old_stream = handler.stream
    handler.setStream(output)
    try:
        logging.getLogger("potocolom.worker").warning(
            "job job-canary-8 bad URL %s",
            f"https://user:{secret}@{'extra-' + secret + '@' if extra_userinfo else ''}"
            f"[invalid/worker-input?token={secret}#frag-{secret}",
        )
    finally:
        handler.setStream(old_stream)
    assert secret not in output.getvalue()
    assert "job-canary-8" in output.getvalue()
    assert "worker-input" in output.getvalue()
