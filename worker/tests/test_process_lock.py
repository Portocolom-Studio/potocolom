"""Tests for the exclusive local worker lock."""

from pathlib import Path

import pytest

from worker.process_lock import acquire_exclusive_lock


def test_acquire_exclusive_lock_blocks_second_holder(tmp_path: Path) -> None:
    path = str(tmp_path / "worker.lock")
    first = acquire_exclusive_lock(path)
    with pytest.raises(BlockingIOError):
        acquire_exclusive_lock(path)
    first.close()
    second = acquire_exclusive_lock(path)
    second.close()


def test_acquire_exclusive_lock_writes_pid(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    handle = acquire_exclusive_lock(str(path))
    try:
        assert path.read_text(encoding="utf-8").strip().isdigit()
    finally:
        handle.close()
