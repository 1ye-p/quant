"""Job management routes — cancel and delete background jobs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from cquant.api_server.deps import CatalogDep, job_queue_stats, request_job_cancel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])

# Tables to check for jobs, in priority order
_JOB_TABLES = [
    ("_api_jobs", "job_id", "status"),
    ("meta_ml_jobs", "job_id", "status"),
    ("gold_backtest_runs", "run_id", "status"),
    ("meta_factor_analytics", "job_id", "status"),
]


def _find_job(catalog, job_id: str) -> tuple[str, str, str] | None:
    """Find a job across all tables. Returns (table, id_col, status) or None."""
    for table, id_col, status_col in _JOB_TABLES:
        try:
            df = catalog.query(
                f"SELECT {id_col}, {status_col} FROM {table} WHERE {id_col} = ?",
                [job_id],
            )
            if not df.is_empty():
                row = df.to_dicts()[0]
                return table, id_col, row[status_col]
        except Exception as exc:
            logger.debug("Table %s not accessible: %s", table, exc)
            continue
    return None


#: Job types whose bodies contain cooperative-cancel checkpoints (engine /
#: fill day loops, sensitivity sweep, validation suite backtests). Types NOT
#: here (ic, ic_matrix, ml, pipeline, scoring, report, ingest) run to natural
#: completion; cancel only flips their DB status.
_CHECKPOINTABLE_JOB_TYPES = {"backtest", "sensitivity", "validation_suite"}


def _job_type(job_id: str) -> str | None:
    from cquant.api_server.deps import JOB_REGISTRY

    handle = JOB_REGISTRY.get(job_id)
    return handle.job_type if handle is not None else None


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, catalog: CatalogDep) -> dict:
    """Cancel a running or pending job."""
    result = _find_job(catalog, job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    table, id_col, status = result
    if status not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Job '{job_id}' has status '{status}' and cannot be cancelled",
        )

    catalog.execute(
        f"UPDATE {table} SET status = 'cancelled' WHERE {id_col} = ?",
        [job_id],
    )
    # P5: also fire the cooperative cancel event so the job's engine/fill
    # day-loop checkpoints raise JobCancelledError and the worker thread
    # exits at the next loop boundary. `cooperative` is only True for job
    # types that actually run those checkpoints — ic / ic_matrix / ML /
    # pipeline / scoring / report have no checkpoints: their threads finish
    # naturally and keep this DB-level 'cancelled' status (T7 review I2:
    # the field must not overpromise).
    cooperative = request_job_cancel(job_id) and _job_type(job_id) in _CHECKPOINTABLE_JOB_TYPES
    return {"job_id": job_id, "status": "cancelled", "cooperative": cooperative}


@router.delete("/{job_id}")
async def delete_job(job_id: str, catalog: CatalogDep) -> dict:
    """Delete a completed or failed job."""
    result = _find_job(catalog, job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    table, id_col, status = result
    if status in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Job '{job_id}' is still {status}. Cancel it first.",
        )

    catalog.execute(
        f"DELETE FROM {table} WHERE {id_col} = ?",
        [job_id],
    )
    return {"job_id": job_id, "status": "deleted"}


@router.get("/queue")
async def get_queue_status() -> dict:
    """Return heavy-job queue concurrency state for frontend display.

    Exposes the global ``JOB_SEMAPHORE`` capacity plus how many heavy jobs are
    currently waiting vs. running, and lifetime submit/complete totals. Poll
    this to render a queue-position / concurrency indicator in the UI.
    """
    return {"queue": job_queue_stats.snapshot()}

