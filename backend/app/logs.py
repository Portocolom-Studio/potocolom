"""Logging setup; keep in sync with worker/worker/logs.py.

Plain single-line logs for development; LOG_FORMAT=json switches to the
structured form the cloud profile ships to CloudWatch (docs/blueprint.md).
"""

import json
import logging
from typing import Literal


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(log_format: Literal["plain", "json"]) -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
