"""Keep query strings out of uvicorn access log records.

Workers fetch their inputs from a URL that carries a bearer capability in
its query string (app/storage.py, worker_fetch_url), and uvicorn's access
logger writes the full path of every request. Moving the capability out of
the URL would break workers that only GET the URL they were handed, so the
record's path argument is rewritten to the path alone before any handler
formats it (issue #690).
"""

import logging

ACCESS_LOGGER = "uvicorn.access"


class _NoQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # The args tuple uvicorn emits: client address, method, path (with
        # its query string), HTTP version, status. Other shapes are left
        # alone rather than guessed at.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = args[:2] + (args[2].split("?", 1)[0],) + args[3:]
        return True


def install() -> None:
    """Attach the rewrite to the access logger; calling it twice is fine."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(existing, _NoQuery) for existing in logger.filters):
        logger.addFilter(_NoQuery())
