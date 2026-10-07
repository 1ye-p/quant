"""Tests for cquant.scheduler.scheduler_lock (F3 — single-instance flock)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cquant.scheduler.scheduler_lock import acquire_scheduler_lock

_CHILD_HOLDER = """
import sys
sys.path.insert(0, {pkg!r})
from cquant.scheduler.scheduler_lock import acquire_scheduler_lock
fd = acquire_scheduler_lock(sys.argv[1])
assert fd is not None, "child failed to acquire fresh lock"
print("HELD", flush=True)
# keep holding until parent tells us to exit (stdin close or signal)
input()
"""


class TestAcquireSchedulerLock:
    def test_second_acquire_returns_none_while_child_holds(self, tmp_path):
        lock = tmp_path / "scheduler.lock"
        pkg = str(Path(__file__).resolve().parents[2])
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD_HOLDER.format(pkg=pkg), str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "HELD"
            # While the child holds the lock, this process must fail (None)
            assert acquire_scheduler_lock(lock) is None
        finally:
            child.stdin.close()
            child.wait(timeout=10)

    def test_lock_reacquirable_after_holder_exits(self, tmp_path):
        lock = tmp_path / "scheduler.lock"
        pkg = str(Path(__file__).resolve().parents[2])
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD_HOLDER.format(pkg=pkg), str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout.readline().strip() == "HELD"
        child.stdin.close()
        child.wait(timeout=10)
        # Holder exited — the advisory flock is released; we can acquire
        fd = acquire_scheduler_lock(lock)
        assert isinstance(fd, int) and fd > 0

    def test_same_process_reacquire_returns_none(self, tmp_path):
        """Within one process, opening the lock file twice: second flock fails."""
        lock = tmp_path / "scheduler.lock"
        assert acquire_scheduler_lock(lock) is not None
        assert acquire_scheduler_lock(lock) is None
