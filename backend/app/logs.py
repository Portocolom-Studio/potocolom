"""Logging setup; keep the plain and JSON shapes in sync with worker/worker/logs.py.

Plain single-line logs for development; LOG_FORMAT=json switches to the
structured form the cloud profile ships to CloudWatch (docs/blueprint.md).
Request-id stamping is backend-only: the worker has no HTTP server, so its
copy needs no filter and its records never carry a request id.
"""

import json
import logging
from typing import IO, Literal

from app.request_id import current_request_id


class RequestIdFilter(logging.Filter):
    """Copy the active request's id onto records logged during that request."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = current_request_id.get()
        if request_id is not None:
            record.request_id = request_id
        return True


class PlainFormatter(logging.Formatter):
    """The plain format, with the request id appended when one is set."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:
        # formatMessage, not format: the id belongs on the message line, not
        # after a traceback that format() appends.
        line = super().formatMessage(record)
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            line += f" request_id={request_id}"
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            entry["request_id"] = request_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def build_handler(
    log_format: Literal["plain", "json"], stream: IO[str] | None = None
) -> logging.StreamHandler:
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestIdFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter())
    return handler


def setup_logging(log_format: Literal["plain", "json"]) -> None:
    logging.basicConfig(level=logging.INFO, handlers=[build_handler(log_format)], force=True)
