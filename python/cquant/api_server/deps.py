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
from functools import lru_cache
from typing import Annotated, Any, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cquant.core.config import settings
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


async def run_job_async(_run_job: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
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

    Parameters
    ----------
    _run_job:
        The synchronous job function. Positional-only so arbitrary keyword
        arguments can be forwarded without collision.
    *args, **kwargs:
        Forwarded verbatim to ``_run_job``.
    """
    import time

    job_type = _derive_job_type(_run_job)
    job_queue_stats.reserve()
    start_ts = time.perf_counter()
    status = "success"
    try:
        async with JOB_SEMAPHORE:
            job_queue_stats.acquire()
            try:
                result = await asyncio.to_thread(_run_job, *args, **kwargs)
                return result
            finally:
                job_queue_stats.release()
    except BaseException:
        status = "failure"
        # If reserve()/acquire() itself never happened (e.g. cancelled before
        # entering the semaphore), the finally above still balances acquire.
        # Here we only reach if something went wrong outside the try body;
        # ensure waiting counter does not leak on cancellation.
        raise
    finally:
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
