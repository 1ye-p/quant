"""cquant.scheduler.scheduler_lock — single-instance advisory lock (F3).

Both scheduler hosts (api_server resident scheduler + CLI BlockingScheduler)
register overlapping cron jobs. Without coordination the two hosts race the
same pipelines against the same DuckDB file (four ``wal.corrupt`` incidents
on record). This module provides a cross-process ``flock`` so exactly one
host runs the scheduled jobs at any time.

Escape hatch: set ``CQUANT_DISABLE_API_SCHEDULER=1`` to keep the api host's
scheduler entirely off (API keeps serving; the CLI host owns the jobs).
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = Path("data/scheduler.lock")


def acquire_scheduler_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> int | None:
    """Acquire the scheduler single-instance lock (non-blocking).

    Opens *lock_path* (created if missing) and takes an exclusive advisory
    ``flock``. On success the file descriptor is returned; the caller keeps
    it open for the process lifetime (closing it releases the lock — so
    callers must NOT close it). On failure (another host holds the lock)
    returns ``None``.

    Parameters
    ----------
    lock_path
        Lock file location. Relative paths resolve against the current
        working directory — both hosts must be started from the repo root
        (or set ``CQUANT_SCHEDULER_LOCK``).
    """
    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.error("Scheduler lock: cannot open lock file %s: %s", lock_path, exc)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logger.warning("Scheduler lock held by another host: %s", lock_path)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass  # advisory payload only; lock is what matters
    logger.info("Scheduler lock acquired: %s (pid=%s)", lock_path, os.getpid())
    return fd


def lock_path_from_env() -> Path:
    """Resolve the lock path from ``CQUANT_SCHEDULER_LOCK`` (or default)."""
    return Path(os.environ.get("CQUANT_SCHEDULER_LOCK", str(DEFAULT_LOCK_PATH)))
