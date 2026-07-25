"""Exclusive process lock for a single local worker (dev stack)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


def acquire_exclusive_lock(path: str) -> TextIO:
    """Take a non-blocking exclusive flock on ``path``.

    Raises ``BlockingIOError`` when another process already holds the lock.
    The returned file object must stay open for the process lifetime; closing
    it (or exiting) releases the lock.
    """
    import fcntl

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle
