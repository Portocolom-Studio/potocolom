"""Database and object-store calls have explicit deadlines (issue #688).

Every bound under test is a module constant shrunk to a fraction of a second,
so each assertion compares against the bound that was under test rather than
against a wall-clock guess, and the whole file runs in a few seconds.
"""

import asyncio
import base64
import socket
import threading
import time
import zlib

import pytest
from botocore.exceptions import FlexibleChecksumError, ReadTimeoutError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import app.storage as storage_module
from app import db
from app.settings import Settings, get_settings
from app.storage import S3Storage
from conftest import run_on_test_loop

# Shrunk from the production 10 s / 30 s / 5 s / 10 s so the suite stays fast.
CONNECT_TIMEOUT = 0.5
READ_TIMEOUT = 2
STATEMENT_TIMEOUT_MS = 300
SLEEP_JUST_PAST_IT = 0.6


class _StalledServer:
    """A listener on 127.0.0.1 that accepts connections and answers nothing.

    A stalled peer is what every deadline here exists for: the TCP connect
    succeeds, so nothing fails until a bound runs out.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)
        self._thread.start()

    def _accept_forever(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._held.append(connection)

    def __enter__(self) -> "_StalledServer":
        return self

    def __exit__(self, *exc) -> None:
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        for connection in self._held:
            try:
                connection.close()
            except OSError:
                pass
        self._held.clear()


class _TrickleServer:
    """Answers one request with valid headers, then sends one byte at a time.

    It sends `count` bytes with a pause between them and then holds the
    connection open, waiting to see the client hang up. That is the shape the
    read deadline has to stop: the store never stops sending, so no socket
    read timeout fires, and the connection is still open when the client
    closes it.
    """

    def __init__(self, count: int, gap: float) -> None:
        self._count = count
        self._gap = gap
        self.client_closed = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                part = connection.recv(65536)
                if not part:
                    return
                request += part
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n"
                b"Content-Type: image/png\r\n\r\n"
            )
            for _ in range(self._count):
                connection.sendall(b"x")
                time.sleep(self._gap)
            connection.settimeout(5)
            try:
                while connection.recv(1024):
                    pass
            except socket.timeout:
                return  # nothing hung up; leave client_closed unset
            self.client_closed.set()
        except OSError:
            # The deadline closed the body under us: a send or receive on a
            # hung-up connection is exactly what the test waits to see.
            self.client_closed.set()
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=10)


class _WatchedClient:
    """The real S3 client, plus a note of when its head_bucket finishes."""

    def __init__(self, client, finished: threading.Event) -> None:
        self._client = client
        self._finished = finished

    def head_bucket(self, **kwargs):
        try:
            return self._client.head_bucket(**kwargs)
        finally:
            self._finished.set()


def _s3_storage(port: int) -> S3Storage:
    return S3Storage(Settings(storage_backend="s3",
                              storage_s3_endpoint=f"http://127.0.0.1:{port}",
                              storage_s3_access_key="key",
                              storage_s3_secret_key="secret"))


def _stall_the_s3_client(monkeypatch, connect: float, read: float) -> None:
    # One attempt: the bound is then exactly connect + read, with no retry
    # backoff to allow for.
    monkeypatch.setattr(storage_module, "S3_CONNECT_TIMEOUT", connect)
    monkeypatch.setattr(storage_module, "S3_READ_TIMEOUT", read)
    monkeypatch.setattr(storage_module, "S3_MAX_ATTEMPTS", 1)


@pytest.mark.db
def test_statement_timeout_cuts_a_serving_statement_and_not_an_offline_one(monkeypatch):
    """The serving engine gets a statement timeout; offline commands do not.

    The operator collapse waits on the account key with no bound on purpose,
    so an offline engine that cut that wait short would break it. This test
    runs first in the file because its connect() is what migrates the test
    database for the test after it.
    """
    monkeypatch.setattr(db, "DB_STATEMENT_TIMEOUT_MS", STATEMENT_TIMEOUT_MS)

    async def through_both_engines() -> None:
        try:
            assert await db.connect(serving=True)
            assert db.engine is not None
            started = time.monotonic()
            with pytest.raises(DBAPIError):
                async with db.engine.connect() as connection:
                    await connection.execute(text("SELECT pg_sleep(1)"))
            # Cut short at 300 ms, well before its own one second.
            assert time.monotonic() - started < 1
            await db.dispose()

            assert await db.connect(serving=False)
            assert db.engine is not None
            started = time.monotonic()
            async with db.engine.connect() as connection:
                await connection.execute(text(f"SELECT pg_sleep({SLEEP_JUST_PAST_IT})"))
            # Not cut short: the same sleep past the bound ran to completion.
            assert time.monotonic() - started >= SLEEP_JUST_PAST_IT
        finally:
            await db.dispose()

    run_on_test_loop(through_both_engines())


@pytest.mark.db
def test_a_stalled_database_fails_within_the_connect_timeout_and_degrades(monkeypatch):
    """A peer that accepts and then says nothing fails at the connect bound.

    connect() has always answered an unreachable database by coming up
    degraded instead of flapping the health check; the deadline only decides
    how long that takes.
    """
    monkeypatch.setattr(db, "DB_CONNECT_TIMEOUT", CONNECT_TIMEOUT)

    async def against_the_stall() -> bool:
        await db.dispose()
        return await db.connect()

    with _StalledServer() as server:
        monkeypatch.setenv(
            "DATABASE_URL",
            f"postgresql://deadline:deadline@127.0.0.1:{server.port}/deadline",
        )
        get_settings.cache_clear()
        started = time.monotonic()
        connected = run_on_test_loop(against_the_stall())
        elapsed = time.monotonic() - started
    assert connected is False
    assert elapsed < 2, elapsed
    # Degraded, as the API comes up when the database cannot be reached.
    assert db.engine is None
    assert db.session_factory is None


def test_ready_returns_within_the_caller_deadline_and_the_probe_ends_within_the_client_bound(
    monkeypatch,
):
    """The caller waits two seconds; the abandoned probe ends at the client bound.

    wait_for stops the await, not the thread, so what bounds the thread is the
    client config: connect plus read, one attempt here, which is deliberately
    longer than the two second caller deadline so the probe is still running
    when ready() gives up.
    """
    connect, read = 0.3, 2.5
    _stall_the_s3_client(monkeypatch, connect, read)
    client_bound = (connect + read) * 1
    with _StalledServer() as server:
        storage = _s3_storage(server.port)
        finished = threading.Event()
        storage.client = _WatchedClient(storage.client, finished)
        started = time.monotonic()
        assert asyncio.run(storage.ready()) is False
        waited = time.monotonic() - started
        assert waited <= 2.1, waited
        assert waited < client_bound, waited
        assert finished.wait(client_bound - waited), "probe outlived the client bound"


def test_image_info_returns_within_the_client_bound_when_the_store_stalls(monkeypatch):
    connect, read = 0.3, 0.5
    _stall_the_s3_client(monkeypatch, connect, read)
    client_bound = (connect + read) * 1
    with _StalledServer() as server:
        storage = _s3_storage(server.port)
        started = time.monotonic()
        # The guard only exists so a missing bound fails this test in seconds
        # instead of hanging the suite; the client config is what answers.
        with pytest.raises(ReadTimeoutError):
            asyncio.run(asyncio.wait_for(storage.image_info("u/j.png"), 10))
        elapsed = time.monotonic() - started
    assert elapsed <= client_bound + 0.5, elapsed


def test_image_info_stops_a_trickling_store_at_the_read_deadline_and_closes_the_body(
    monkeypatch,
):
    """A store that never stops sending defeats every socket read timeout.

    Only the whole-read deadline stops it, and the body has to close behind
    the failure: the store sees that hang-up, which is what the server below
    reports.
    """
    deadline = 0.5
    monkeypatch.setattr(storage_module, "S3_READ_DEADLINE", deadline)
    _stall_the_s3_client(monkeypatch, connect=0.3, read=READ_TIMEOUT)
    # Enough bytes to still be trickling when the deadline fires, and a pause
    # per byte short enough that no read timeout fires first.
    server = _TrickleServer(count=2000, gap=0.001)
    try:
        storage = _s3_storage(server.port)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            asyncio.run(storage.image_info("u/j.png"))
        elapsed = time.monotonic() - started
        assert deadline <= elapsed <= deadline + 0.5, elapsed
        assert server.client_closed.wait(5), "the response body was not closed"
    finally:
        server.close()


class _AnsweringServer:
    """Answers one request with a fixed response, then closes."""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                part = connection.recv(65536)
                if not part:
                    return
                request += part
            connection.sendall(self._response)

    def close(self) -> None:
        self._listener.close()


def _object_with_crc32(body: bytes, crc32: int) -> bytes:
    checksum = base64.b64encode(crc32.to_bytes(4, "big"))
    return (b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"x-amz-checksum-crc32: " + checksum + b"\r\n\r\n" + body)


@pytest.mark.parametrize("matches", [True, False])
def test_image_info_still_checks_the_response_checksum(monkeypatch, matches):
    """Reading under the deadline bypasses the body's own reads, which are
    where botocore checks the response checksum; the check must survive."""
    _stall_the_s3_client(monkeypatch, connect=0.3, read=READ_TIMEOUT)
    body = b"not an image"
    crc32 = zlib.crc32(body) if matches else zlib.crc32(body) ^ 1
    server = _AnsweringServer(_object_with_crc32(body, crc32))
    try:
        storage = _s3_storage(server.port)
        if matches:
            assert asyncio.run(storage.image_info("u/j.png")) is None
        else:
            with pytest.raises(FlexibleChecksumError):
                asyncio.run(storage.image_info("u/j.png"))
    finally:
        server.close()
