"""Logging setup; keep in sync with backend/app/logs.py.

Plain single-line logs for development; LOG_FORMAT=json switches to the
structured form the cloud profile ships to CloudWatch (docs/blueprint.md).
"""

import json
import logging
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


_URL_RE = re.compile(r"(?i)https?://[^\s<>\"]+")


def _redact_url(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        parts = urlsplit(value)
        host = parts.netloc.rsplit("@", 1)[-1]
        safe = urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        # Keep malformed URL text from bypassing the log boundary.
        safe = re.sub(r"(?i)(https?://)[^/\s]*@", r"\1", value)
        safe = safe.split("?", 1)[0].split("#", 1)[0]
    return safe


def _redact_urls(value: str) -> str:
    return _URL_RE.sub(_redact_url, value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_urls(record.getMessage()),
        }
        if record.exc_info:
            entry["exception"] = _redact_urls(self.formatException(record.exc_info))
        return json.dumps(entry)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_urls(super().format(record))


def setup_logging(log_format: Literal["plain", "json"]) -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            PlainFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
