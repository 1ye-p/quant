"""cquant.core.jobs — shared cooperative-cancel primitives.

Layer-neutral home for :class:`JobCancelledError` so the job layer
(``api_server.deps``), the vector engine, and the fill simulator can raise /
catch the same exception without the engine importing anything from the API
layer (Strategy ABC untouched — cancellation travels via ``BacktestSpec``
fields, never through strategy code).
"""

from __future__ import annotations


class JobCancelledError(RuntimeError):
    """Raised at a cooperative checkpoint when a job is cancelled/timed out.

    ``reason`` is ``"timeout"`` when the deadline watcher fired, or
    ``"cancelled"`` for a user-requested cancel. ``status`` maps to the
    persisted job status: ``failed`` for timeouts (test contract:
    ``failed(timeout)``), ``cancelled`` for user cancels.
    """

    def __init__(self, reason: str = "cancelled", job_id: str | None = None) -> None:
        label = f"job {job_id}" if job_id else "job"
        super().__init__(f"{label} aborted: {reason}")
        self.reason = reason
        self.job_id = job_id

    @property
    def status(self) -> str:
        return "failed" if self.reason == "timeout" else "cancelled"
