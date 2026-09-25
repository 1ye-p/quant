"""_job_ts: naive-UTC _api_jobs timestamps must export with explicit offset.

_api_jobs.created_at/updated_at are naive TIMESTAMP columns — DuckDB strips
the offset from the aware-UTC values written by _save_job. Without the +00:00
re-attached at export, frontend `new Date()` treats UTC wall-clock digits as
local time (8h skew on UTC+8 machines) in the backtests list, tasks page and
top-right running-task elapsed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from cquant.api_server.routes.backtests import _job_ts


class TestJobTs:
    def test_naive_datetime_gets_utc_offset(self) -> None:
        naive = datetime(2026, 9, 25, 12, 23, 41, 11847)
        assert _job_ts(naive) == "2026-09-25T12:23:41.011847+00:00"

    def test_aware_datetime_passthrough(self) -> None:
        aware = datetime(2026, 9, 25, 12, 23, 41, tzinfo=timezone.utc)
        assert _job_ts(aware) == "2026-09-25T12:23:41+00:00"

    def test_aware_non_utc_passthrough(self) -> None:
        from datetime import timedelta

        cst = timezone(timedelta(hours=8))
        aware = datetime(2026, 8, 18, 22, 19, 7, tzinfo=cst)
        assert _job_ts(aware) == "2026-08-18T22:19:07+08:00"

    def test_naive_string_gets_offset(self) -> None:
        assert _job_ts("2026-09-25T12:23:41.011847") == "2026-09-25T12:23:41.011847+00:00"

    def test_z_suffix_string_untouched(self) -> None:
        assert _job_ts("2026-09-25T12:23:41Z") == "2026-09-25T12:23:41Z"

    def test_none_returns_empty(self) -> None:
        assert _job_ts(None) == ""
