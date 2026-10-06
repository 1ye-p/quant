"""cquant.api_server.deps — FastAPI dependency injection.

All shared resources (Catalog, KnowledgeBaseService, AdvisorOrchestrator) are
created once at startup and injected via FastAPI's Depends() mechanism.

This module also owns the global job concurrency primitives: a bounded
``JOB_SEMAPHORE`` that gates heavy background jobs (backtest / factors / ML /
scoring / datasets / pipeline) and a ``JobQueueStats`` counter that tracks how
many jobs are waiting and running for frontend display.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cquant.core.config import settings
from cquant.core.jobs import JobCancelledError

__all__ = [
    "JOB_REGISTRY",
    "JobCancelledError",
    "JobHandle",
    "activate_job",
    "check_job_cancel",
    "get_job_cancel_event",
    "get_job_progress",
    "register_job",
    "request_job_cancel",
    "run_job_async",
    "set_job_stage",
]
from cquant.datahub.catalog import Catalog
from cquant.knowledge_base import KnowledgeBaseService


# ---------------------------------------------------------------------------
# Global job concurrency control
# ---------------------------------------------------------------------------
#
# Heavy background jobs (backtest, factor analytics, ML training, scoring,
# data ingest, full pipeline) are CPU/IO bound and can saturate the host if
# submitted unbounded. ``JOB_SEMAPHORE`` caps how many run concurrently; the
# cap is configurable via ``CQUANT_MAX_CONCURRENT_JOBS`` (default 2).
#
# Jobs are submitted through ``run_job_async`` (an async coroutine), so FastAPI
# runs them on the event loop where ``async with JOB_SEMAPHORE`` actually
# blocks, then dispatches the (synchronous) job body to a worker thread via
# ``asyncio.to_thread`` to avoid blocking the loop while the job runs.

#: Maximum number of heavy jobs that may execute concurrently.
JOB_SEMAPHORE = asyncio.Semaphore(int(os.getenv("CQUANT_MAX_CONCURRENT_JOBS", "2")))


class JobQueueStats:
    """Process-wide counters for the heavy job queue.

    All mutators are *not* async-safe by themselves — they are guarded by
    ``JOB_SEMAPHORE`` / ``_queue_counter_lock`` at the call sites in
    ``run_job_async``. Reads (``snapshot``) are atomic enough for observability.
    """

    __slots__ = ("waiting", "running", "total_submitted", "total_completed", "_next_position")

    def __init__(self) -> None:
        self.waiting: int = 0
        self.running: int = 0
        self.total_submitted: int = 0
        self.total_completed: int = 0
        self._next_position: int = 1

    def reserve(self) -> int:
        """Called when a job enters the queue. Returns its queue position."""
        self.waiting += 1
        self.total_submitted += 1
        position = self._next_position
        self._next_position += 1
        return position

    def acquire(self) -> None:
        """Called when a job leaves the queue and starts running."""
        self.waiting = max(0, self.waiting - 1)
        self.running += 1

    def release(self) -> None:
        """Called when a job finishes (success or failure)."""
        self.running = max(0, self.running - 1)
        self.total_completed += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable view of current queue state."""
        cap = int(os.getenv("CQUANT_MAX_CONCURRENT_JOBS", "2"))
        return {
            "max_concurrent": cap,
            "waiting": self.waiting,
            "running": self.running,
            "total_submitted": self.total_submitted,
            "total_completed": self.total_completed,
        }


#: Global queue counter shared by all routes.
job_queue_stats = JobQueueStats()


# ---------------------------------------------------------------------------
# Cooperative-cancel job registry (P5)
# ---------------------------------------------------------------------------
#
# Every job submitted through ``run_job_async`` *with a job_id* is registered
# here: a per-job ``threading.Event`` (the cooperative cancel signal checked
# at engine/fill day-loop checkpoints), a deadline (watcher thread), and a
# ``stage`` field for frontend progress display. Jobs without a job_id keep
# the legacy behaviour unchanged.


def _job_timeout_sec() -> float:
    """Per-call deadline from ``CQUANT_JOB_TIMEOUT_SEC`` (default 3600s)."""
    try:
        return max(1.0, float(os.getenv("CQUANT_JOB_TIMEOUT_SEC", "3600")))
    except ValueError:
        return 3600.0


@dataclass
class JobHandle:
    """Registry entry for one cancellable job."""

    job_id: str
    job_type: str = "job"
    cancel_event: threading.Event = field(default_factory=threading.Event)
    stage: str = "queued"
    stage_history: list[str] = field(default_factory=list)
    started_ts: float = 0.0
    deadline_ts: float = 0.0
    terminal_reason: str | None = None  # "timeout" | "cancelled" once decided

    def progress(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "elapsed_s": round(time.monotonic() - self.started_ts, 1),
            "cancel_requested": self.cancel_event.is_set(),
            "stage_history": list(self.stage_history),
            "terminal_reason": self.terminal_reason,
        }


#: job_id -> JobHandle (guarded by ``_registry_lock``).
JOB_REGISTRY: dict[str, JobHandle] = {}
#: job_ids cancelled BEFORE registration (cancel endpoint fires while the
#: BackgroundTask hasn't started run_job_async yet). register_job consumes
#: the entry and pre-arms the cancel event — otherwise the job would run to
#: completion with a fresh clear event and overwrite DB status 'cancelled'
#: with its own 'completed' (T7 review I3).
_PENDING_CANCELS: set[str] = set()
_registry_lock = threading.Lock()


def register_job(job_id: str, job_type: str = "job") -> JobHandle:
    """Register a queued job; deadline bookkeeping starts at :func:`activate_job`.

    The job is registered *before* the semaphore acquire so cancels requested
    while queued are honoured; its deadline, however, only starts ticking once
    the job actually begins running (queue wait must not burn the budget).
    """
    import time

    handle = JobHandle(
        job_id=job_id,
        job_type=job_type,
        stage="queued",
        stage_history=["queued"],
        started_ts=time.monotonic(),
    )
    with _registry_lock:
        JOB_REGISTRY[job_id] = handle
        if job_id in _PENDING_CANCELS:
            # Cancelled before this registration — honour it immediately so
            # the first engine/fill checkpoint raises instead of a full run.
            _PENDING_CANCELS.discard(job_id)
            handle.terminal_reason = "cancelled"
            handle.cancel_event.set()
    return handle


def activate_job(job_id: str) -> None:
    """Transition a queued job to running and (re)start its deadline clock."""
    import time

    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
        if handle is None or handle.stage == "running":
            return
        handle.stage = "running"
        handle.stage_history.append("running")
        handle.started_ts = time.monotonic()
        handle.deadline_ts = handle.started_ts + _job_timeout_sec()


def unregister_job(job_id: str) -> None:
    with _registry_lock:
        JOB_REGISTRY.pop(job_id, None)
        # Never-registered stale cancels must not accumulate forever.
        _PENDING_CANCELS.discard(job_id)


def request_job_cancel(job_id: str) -> bool:
    """Cooperatively request cancellation. Returns True if the job is registered.

    A cancel for an unregistered job_id is remembered in ``_PENDING_CANCELS``
    (the job body may not have started yet) — register_job will pre-arm the
    event when it eventually registers.
    """
    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
        if handle is None:
            _PENDING_CANCELS.add(job_id)
            return False
    handle.terminal_reason = handle.terminal_reason or "cancelled"
    handle.cancel_event.set()
    return True


def set_job_stage(job_id: str, stage: str) -> None:
    """Advance a job's stage for frontend display; unknown job_id is a no-op."""
    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
        if handle is None:
            return
        handle.stage = stage
        handle.stage_history.append(stage)


def get_job_progress(job_id: str) -> dict[str, Any] | None:
    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
    return handle.progress() if handle is not None else None


def get_job_cancel_event(job_id: str) -> threading.Event | None:
    """Fetch the cooperative cancel event for a job (None if unregistered)."""
    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
    return handle.cancel_event if handle is not None else None


def check_job_cancel(job_id: str) -> None:
    """Cooperative checkpoint: raise :class:`JobCancelledError` if cancelled.

    Timeout cancels carry ``reason="timeout"`` so the job persists as
    ``failed(timeout)``; user cancels as ``cancelled``.
    """
    with _registry_lock:
        handle = JOB_REGISTRY.get(job_id)
    if handle is not None and handle.cancel_event.is_set():
        raise JobCancelledError(
            reason=handle.terminal_reason or "cancelled", job_id=job_id
        )


def _persist_job_record(catalog, job_id: str, job_type: str, job_status: str,
                        error: str | None = None, run_id: str | None = None) -> None:
    """Best-effort persistence via the canonical ``_save_job`` upsert."""
    if catalog is None:
        return
    try:
        from cquant.api_server.routes.backtests import _save_job

        _save_job(catalog, job_id, job_type, job_status, run_id=run_id, error=error)
    except Exception as exc:  # pragma: no cover — persistence is best-effort
        logging.getLogger(__name__).debug("_persist_job_record(%s): %s", job_id, exc)


def _start_timeout_watcher(handle: JobHandle, catalog) -> threading.Thread:
    """Daemon thread: fire the cancel event + persist failed(timeout) at deadline."""

    def _watch() -> None:
        import time

        remaining = handle.deadline_ts - time.monotonic()
        if remaining <= 0 or not handle.cancel_event.wait(remaining):
            # Deadline hit (or already fired elsewhere) — mark and persist.
            first_timeout = handle.terminal_reason is None
            handle.terminal_reason = handle.terminal_reason or "timeout"
            handle.cancel_event.set()
            if first_timeout:
                handle.stage = "timeout"
                _persist_job_record(
                    catalog, handle.job_id, handle.job_type, "failed",
                    error=f"Timeout: exceeded {int(_job_timeout_sec())}s "
                          f"(CQUANT_JOB_TIMEOUT_SEC)",
                )

    thread = threading.Thread(
        target=_watch, name=f"job-watch-{handle.job_id}", daemon=True
    )
    thread.start()
    return thread


async def run_job_async(
    _run_job: Callable[..., Any],
    /,
    *args: Any,
    job_id: str | None = None,
    job_type: str | None = None,
    catalog: Any = None,
    **kwargs: Any,
) -> Any:
    """Run a heavy (synchronous) job under the global semaphore.

    The job callable runs in a worker thread (``asyncio.to_thread``) so the
    event loop stays responsive while it blocks. ``job_queue_stats`` is updated
    across the queue→run→done transitions for frontend display.

    Per-job observability is wired in here so all callers (backtest / factor
    analysis / sensitivity) get it for free: wall-clock duration, peak RSS
    delta, and outcome are emitted to the ``backtest_duration_seconds`` and
    ``backtest_job_duration_seconds`` Prometheus histograms and a structured
    log record. ``job_type`` is derived from the callable name (``_run_job``
    / ``_run_analysis`` / ``_run_sensitivity``) so dashboards stay readable.

    Cooperative cancel (P5): when ``job_id`` is provided the job is registered
    in ``JOB_REGISTRY`` with a per-job cancel event (fetchable inside the job
    body via ``get_job_cancel_event`` / checked with ``check_job_cancel``) and
    a deadline watcher (``CQUANT_JOB_TIMEOUT_SEC``, default 3600s). The
    deadline starts when the job leaves the queue (semaphore acquired) so
    queue wait does not burn the budget. The watcher
    persists ``failed(timeout)`` at the deadline; a job that hits a checkpoint
    afterwards raises :class:`JobCancelledError`, which is swallowed here after
    persisting the terminal status — artifacts already written are kept
    (diagnostics first). Without ``job_id`` behaviour is unchanged.

    Parameters
    ----------
    _run_job:
        The synchronous job function. Positional-only so arbitrary keyword
        arguments can be forwarded without collision.
    *args, **kwargs:
        Forwarded verbatim to ``_run_job``.
    job_id:
        Optional registry key enabling cancel/timeout/stage tracking.
    job_type:
        Overrides the derived job-type label (used for persistence).
    catalog:
        Optional catalog used to persist the terminal job record on
        cancel/timeout.
    """
    job_type = job_type or _derive_job_type(_run_job)
    job_queue_stats.reserve()
    start_ts = time.perf_counter()
    status = "success"
    handle: JobHandle | None = None
    watcher: threading.Thread | None = None
    if job_id:
        handle = register_job(job_id, job_type=job_type)
    try:
        async with JOB_SEMAPHORE:
            job_queue_stats.acquire()
            if handle is not None:
                # Deadline starts only now (queue wait doesn't burn budget);
                # the watcher is started alongside so it never fires early.
                activate_job(job_id)
                watcher = _start_timeout_watcher(handle, catalog)
            try:
                result = await asyncio.to_thread(_run_job, *args, **kwargs)
                return result
            finally:
                job_queue_stats.release()
    except JobCancelledError as exc:
        # Cooperative checkpoint fired. The watcher already persisted
        # failed(timeout) on a deadline hit; for user cancels (or if the
        # watcher could not persist) record the terminal status here.
        status = "failure"
        if handle is not None:
            handle.terminal_reason = handle.terminal_reason or exc.reason
            reason = handle.terminal_reason
        else:
            reason = exc.reason
        # Persist unconditionally for user cancels; for timeouts the watcher
        # already persisted failed(timeout), but if its persist threw (swallowed
        # at debug level) this upsert is the fallback — otherwise the record
        # stays 'running' forever with no registry entry (review M8).
        _persist_job_record(
            catalog, job_id or "?", job_type,
            "failed" if reason == "timeout" else "cancelled",
            error=str(exc),
        )
        logging.getLogger(__name__).info("job_cancelled: %s (%s)", job_id, reason)
        return None
    except BaseException:
        status = "failure"
        # If reserve()/acquire() itself never happened (e.g. cancelled before
        # entering the semaphore), the finally above still balances acquire.
        # Here we only reach if something went wrong outside the try body;
        # ensure waiting counter does not leak on cancellation.
        raise
    finally:
        if handle is not None:
            # Release the watcher (no-op if it already fired) and join it so
            # no zombie threads outlive the job. Setting the event here is
            # safe: the job body has finished, nothing checks the event after.
            handle.cancel_event.set()
        if watcher is not None:
            watcher.join(timeout=5.0)
        if handle is not None and job_id:
            unregister_job(job_id)
        _record_job_metrics(job_type, status, start_ts)


def _derive_job_type(func: Callable[..., Any]) -> str:
    """Best-effort human label for a job callable (``_run_job`` -> ``job``)."""
    name = getattr(func, "__name__", "job") or "job"
    # Strip a leading ``_run_`` prefix used by the route call sites so the
    # label reads ``backtest`` rather than ``_run_job``.
    if name.startswith("_run_"):
        name = name[len("_run_"):]
    return name or "job"


def _record_job_metrics(job_type: str, status: str, start_ts: float) -> None:
    """Observe job duration + memory delta into Prometheus + structured log.

    All observability here is best-effort: any failure (missing
    prometheus_client, no ``resource`` module on a stripped runtime) is
    swallowed so a broken metric never causes a job to look failed.
    """
    import logging
    import time

    duration = max(0.0, time.perf_counter() - start_ts)
    peak_rss_mb: float | None = None
    try:
        import resource

        # ru_maxrss is in kilobytes on macOS/BSD, bytes-per-page elsewhere; the
        # ru_ixrss et al. fields are unreliable across platforms so we use the
        # peak RSS delta from a stored baseline when available.
        peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:  # pragma: no cover — resource module is standard
        peak_rss_mb = None

    log = logging.getLogger("cquant.api")
    try:
        log.info(
            "job_completed",
            extra={
                "job_type": job_type,
                "status": status,
                "duration_s": round(duration, 3),
                "peak_rss_mb": round(peak_rss_mb, 2) if peak_rss_mb is not None else None,
            },
        )
    except Exception:  # pragma: no cover
        pass

    try:
        from cquant.api_server.routes.metrics import (
            backtest_duration_seconds,
            backtest_job_duration_seconds,
        )

        if backtest_duration_seconds is not None:
            backtest_duration_seconds.labels(job_type=job_type).observe(duration)
        if backtest_job_duration_seconds is not None:
            backtest_job_duration_seconds.labels(
                job_type=job_type, status=status
            ).observe(duration)
    except Exception:  # pragma: no cover — metrics are best-effort
        pass


@lru_cache(maxsize=1)
def _get_catalog() -> Catalog:
    cat = Catalog(db_path=settings.db_path)
    cat.initialize()
    return cat


@lru_cache(maxsize=1)
def _get_kb_service() -> KnowledgeBaseService:
    return KnowledgeBaseService.create(
        db_path=settings.db_path,
        kb_root=settings.storage.knowledge_root,
        vector_path=f"{settings.storage.knowledge_root}/vector/lancedb",
    )


def get_catalog() -> Catalog:
    return _get_catalog()


def reset_catalog() -> None:
    """Drop the cached Catalog so the next ``get_catalog()`` creates a fresh one.

    Does NOT close the underlying connection — pair with ``close_catalog()``
    when the caller owns the lifecycle (e.g. app shutdown).
    """
    _get_catalog.cache_clear()


def close_catalog() -> bool:
    """Close the cached catalog if one was ever created; clear the cache.

    Probes the lru_cache so a shutdown path never *creates* a connection just
    to close it (which would also run DDL via ``initialize()``).

    Returns
    -------
    bool
        True if a catalog existed and was closed (WAL checkpointed), False if
        no catalog had been created in this process.
    """
    if _get_catalog.cache_info().currsize == 0:
        return False
    catalog = _get_catalog()
    catalog.close()
    reset_catalog()
    return True


def get_kb_service() -> KnowledgeBaseService:
    return _get_kb_service()


CatalogDep = Annotated[Catalog, Depends(get_catalog)]
KBServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]

_logger = logging.getLogger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)
_auth_warned = False


def _is_trading_endpoint(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/trading/")


def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Verify Bearer token against CQUANT_API_KEY env var.

    Default-deny: when CQUANT_API_KEY is not set, ALL authenticated endpoints
    (including /trading/*) are rejected with 503. Set the key via
    ``cquant auth generate-key`` or the CQUANT_API_KEY environment variable.

    Escape hatch: ``CQUANT_AUTH_MODE=dev`` restores the historical permissive
    behavior for local development (non-trading endpoints pass with a one-time
    warning; trading endpoints still require a key).

    Credential sources (first match wins):
    1. ``Authorization: Bearer <key>`` header — preferred for all fetch/XHR.
    2. ``?api_key=<key>`` query parameter — fallback for ``EventSource`` SSE
       connections (browser API cannot set request headers). Prefer the
       header whenever the client supports it.
    """
    global _auth_warned
    mode = os.getenv("CQUANT_AUTH_MODE", "strict")
    api_key = os.environ.get("CQUANT_API_KEY", "")
    if not api_key:
        if mode != "dev" or _is_trading_endpoint(request):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "API key not configured. Run 'cquant auth generate-key' "
                    "or set CQUANT_API_KEY. See docs/security.md"
                ),
            )
        if not _auth_warned:
            _logger.warning(
                "CQUANT_API_KEY is not set and CQUANT_AUTH_MODE=dev — API "
                "authentication is DISABLED. Never use dev mode in production."
            )
            _auth_warned = True
        return
    presented = (
        credentials.credentials
        if credentials is not None
        else request.query_params.get("api_key", "")
    )
    if not presented or not hmac.compare_digest(presented, api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Use Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "Bearer"},
        )
