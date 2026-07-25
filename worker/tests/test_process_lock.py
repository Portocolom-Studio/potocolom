"""Tests for the exclusive local worker lock."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from worker.process_lock import acquire_exclusive_lock


def _hold_lock(path: str, ready: mp.synchronize.Event, release: mp.synchronize.Event) -> None:
    handle = acquire_exclusive_lock(path)
    ready.set()
    release.wait(timeout=10)
    handle.close()


def test_acquire_exclusive_lock_blocks_second_holder(tmp_path: Path) -> None:
    path = str(tmp_path / "worker.lock")
    ready = mp.Event()
    release = mp.Event()
    holder = mp.Process(target=_hold_lock, args=(path, ready, release))
    holder.start()
    try:
        assert ready.wait(timeout=5), "holder did not acquire the lock"
        with pytest.raises(BlockingIOError):
            acquire_exclusive_lock(path)
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)
        assert holder.exitcode == 0


def test_acquire_exclusive_lock_writes_pid(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    handle = acquire_exclusive_lock(str(path))
    try:
        assert path.read_text(encoding="utf-8").strip().isdigit()
    finally:
        handle.close()
