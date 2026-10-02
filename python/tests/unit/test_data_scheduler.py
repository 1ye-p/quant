"""Tests for cquant.scheduler.data_scheduler."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from datetime import date as date_cls

from cquant.scheduler.data_scheduler import DataScheduler, _with_retry


# ---------------------------------------------------------------------------
# _with_retry
# ---------------------------------------------------------------------------

class TestWithRetry:
    def test_success_on_first_try(self):
        fn = MagicMock(return_value=42)
        result = _with_retry(fn, max_retries=3, base_delay=0.01)
        assert result == 42
        assert fn.call_count == 1

    def test_success_after_retries(self):
        fn = MagicMock(side_effect=[ValueError("fail"), ValueError("fail"), "ok"])
        result = _with_retry(fn, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert fn.call_count == 3

    def test_exhausted_retries_raises(self):
        fn = MagicMock(side_effect=ValueError("always fail"))
        with pytest.raises(ValueError, match="always fail"):
            _with_retry(fn, max_retries=2, base_delay=0.01)
        assert fn.call_count == 2

    def test_passes_args_and_kwargs(self):
        fn = MagicMock(return_value="result")
        _with_retry(fn, "a", "b", key="val", max_retries=1, base_delay=0.01)
        fn.assert_called_once_with("a", "b", key="val")


# ---------------------------------------------------------------------------
# DataScheduler — unit tests (no real APScheduler)
# ---------------------------------------------------------------------------

class TestDataScheduler:
    def _make_catalog(self) -> MagicMock:
        catalog = MagicMock()
        catalog.query.return_value = MagicMock(is_empty=MagicMock(return_value=True))
        return catalog

    def test_init(self):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog, timezone="Asia/Shanghai")
        assert sched._tz == "Asia/Shanghai"
        assert sched._running is False

    def test_status_not_running(self):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        info = sched.status()
        assert info["running"] is False
        assert info["timezone"] == "Asia/Shanghai"
        assert info["jobs"] == []

    def test_run_task_unknown_raises(self):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        with pytest.raises(ValueError, match="Unknown task"):
            sched.run_task("nonexistent")

    @patch("cquant.scheduler.data_scheduler._job_health")
    def test_run_task_health(self, mock_health):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("health")
        mock_health.assert_called_once_with(catalog)

    @patch("cquant.scheduler.data_scheduler._job_alerts")
    def test_run_task_alerts(self, mock_alerts):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("alerts")
        mock_alerts.assert_called_once_with(catalog)

    @patch("cquant.scheduler.data_scheduler._job_price_ingest")
    def test_run_task_price_ingest(self, mock_ingest):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("price-ingest")
        mock_ingest.assert_called_once_with(catalog)

    @patch("cquant.scheduler.data_scheduler._job_fundamentals")
    def test_run_task_fundamentals(self, mock_fund):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("fundamentals")
        mock_fund.assert_called_once_with(catalog)

    def test_record_run_creates_table(self):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched._record_run("test_job")
        # Should have called execute at least once (CREATE TABLE + INSERT)
        assert catalog.execute.call_count >= 2

    def test_record_run_handles_error_gracefully(self):
        catalog = self._make_catalog()
        catalog.execute.side_effect = Exception("db error")
        sched = DataScheduler(catalog)
        # Should not raise
        sched._record_run("test_job")


# ---------------------------------------------------------------------------
# Job implementations — integration-style tests with mocked dependencies
# ---------------------------------------------------------------------------

class TestExtIndicatorRefreshTask:
    """P2-5: 'ext-ind-refresh' task dispatch + job wiring (dual registration, CLI side)."""

    @patch("cquant.scheduler.data_scheduler._job_ext_indicator_refresh")
    def test_run_task_dispatches_ext_ind_refresh(self, mock_refresh):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("ext-ind-refresh")
        mock_refresh.assert_called_once_with(catalog)

    @patch("cquant.scheduler.data_scheduler._job_ext_indicator_refresh")
    def test_run_task_failure_records_run(self, mock_refresh):
        catalog = self._make_catalog()
        mock_refresh.side_effect = ValueError("boom")
        sched = DataScheduler(catalog)
        sched.run_task("ext-ind-refresh")  # must not raise (pattern of other runners)
        runs = [c.args[1] for c in catalog.execute.call_args_list
                if len(c.args) > 1 and isinstance(c.args[1], list)]
        assert any(r[0] == "ext_indicator_refresh" and r[2] == "failure" for r in runs)

    @patch("cquant.scheduler.data_scheduler._job_ext_indicator_refresh")
    def test_run_task_success_records_run(self, mock_refresh):
        catalog = self._make_catalog()
        sched = DataScheduler(catalog)
        sched.run_task("ext-ind-refresh")
        runs = [c.args[1] for c in catalog.execute.call_args_list
                if len(c.args) > 1 and isinstance(c.args[1], list)]
        assert any(r[0] == "ext_indicator_refresh" and r[2] == "success" for r in runs)

    def _make_catalog(self) -> MagicMock:
        catalog = MagicMock()
        catalog.query.return_value = MagicMock(is_empty=MagicMock(return_value=True))
        return catalog


class TestJobExtIndicatorRefresh:
    @patch(
        "cquant.datahub.pipelines.indicator_sources.refresh.run_external_indicator_refresh"
    )
    def test_job_calls_refresh_with_scheduled_trigger(self, mock_refresh):
        from cquant.scheduler.data_scheduler import _job_ext_indicator_refresh

        catalog = MagicMock()
        _job_ext_indicator_refresh(catalog)
        mock_refresh.assert_called_once_with(catalog, trigger="scheduled")


class TestApiServerSchedulerRegistration:
    """P2-5: api_server resident scheduler registers the 18:15 refresh job.

    APScheduler may be absent in the test env — fake the modules via sys.modules
    (mirrors the ImportError fallback inside start_data_scheduler).
    """

    def test_start_data_scheduler_registers_ext_indicator_refresh(self, monkeypatch):
        import sys
        import types

        added_jobs: list[dict] = []
        trigger_calls: list[dict] = []

        class FakeAsyncIOScheduler:
            def __init__(self, timezone=None):
                self.timezone = timezone

            def add_job(self, fn, trigger=None, **kwargs):
                added_jobs.append({"fn": fn, "trigger": trigger, **kwargs})

            def start(self):
                return None

            def get_job(self, job_id):
                return None

        class FakeCronTrigger:
            def __init__(self, **kwargs):
                trigger_calls.append(kwargs)

        mods = {
            "apscheduler": types.ModuleType("apscheduler"),
            "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
            "apscheduler.schedulers.asyncio": types.ModuleType(
                "apscheduler.schedulers.asyncio"
            ),
            "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
            "apscheduler.triggers.cron": types.ModuleType("apscheduler.triggers.cron"),
        }
        mods["apscheduler.schedulers.asyncio"].AsyncIOScheduler = FakeAsyncIOScheduler
        mods["apscheduler.triggers.cron"].CronTrigger = FakeCronTrigger
        for name, mod in mods.items():
            monkeypatch.setitem(sys.modules, name, mod)

        from cquant.api_server import data_scheduler as api_ds

        monkeypatch.setattr(api_ds, "_SCHEDULER_INSTANCE", None)
        catalog = MagicMock()
        api_ds.start_data_scheduler(catalog)

        assert {"hour": 18, "minute": 15, "timezone": "Asia/Shanghai"} in trigger_calls
        ids = [j.get("id") for j in added_jobs]
        assert "ext_indicator_refresh" in ids
        for j in added_jobs:
            if j.get("id") == "ext_indicator_refresh":
                assert j.get("args") == [catalog]
                assert j.get("replace_existing") is True


class TestJobPriceIngest:
    def test_skips_when_up_to_date(self, caplog):
        from cquant.scheduler.data_scheduler import _job_price_ingest

        catalog = MagicMock()
        mock_df = MagicMock()
        mock_df.is_empty.return_value = False
        mock_df.__getitem__ = lambda self, key: [date.today()]
        catalog.query.return_value = mock_df

        with caplog.at_level(logging.INFO):
            _job_price_ingest(catalog)

        assert "up to date" in caplog.text

    @patch("cquant.datahub.ingest.MarketIngestionOrchestrator")
    @patch("cquant.datahub.connectors.akshare_connector.AKShareConnector")
    def test_runs_ingest_when_stale(self, mock_connector_cls, mock_orch_cls):
        from cquant.scheduler.data_scheduler import _job_price_ingest

        catalog = MagicMock()
        mock_df = MagicMock()
        mock_df.is_empty.return_value = True
        catalog.query.return_value = mock_df

        mock_orch = MagicMock()
        mock_orch_cls.return_value = mock_orch

        _job_price_ingest(catalog)

        mock_orch.ingest.assert_called_once()


class TestJobFundamentals:
    @patch("cquant.datahub.pipelines.fundamentals_updater.update_fundamentals")
    def test_calls_update(self, mock_update):
        from cquant.scheduler.data_scheduler import _job_fundamentals

        mock_update.return_value = 10
        catalog = MagicMock()
        _job_fundamentals(catalog)
        mock_update.assert_called_once_with(catalog, source="akshare")


class TestJobAlerts:
    def test_runs_without_error(self, caplog):
        from cquant.scheduler.data_scheduler import _job_alerts

        catalog = MagicMock()
        mock_df = MagicMock()
        mock_df.is_empty.return_value = False
        mock_df.__getitem__ = lambda self, key: [5]
        catalog.query.return_value = mock_df

        with caplog.at_level(logging.INFO):
            _job_alerts(catalog)

        assert "alert check completed" in caplog.text

    def test_handles_missing_table(self, caplog):
        from cquant.scheduler.data_scheduler import _job_alerts

        catalog = MagicMock()
        catalog.query.side_effect = Exception("table not found")

        with caplog.at_level(logging.INFO):
            _job_alerts(catalog)

        assert "alert check completed" in caplog.text


class TestJobHealth:
    def test_reports_table_status(self, caplog):
        from cquant.scheduler.data_scheduler import _job_health

        catalog = MagicMock()
        # First query: silver_prices_1d
        df1 = MagicMock()
        df1.is_empty.return_value = False
        df1.__getitem__ = lambda self, key: [date_cls(2025, 6, 1)]
        # Second query: silver_fundamentals
        df2 = MagicMock()
        df2.__getitem__ = lambda self, key: [1000]
        catalog.query.side_effect = [df1, df2]

        with caplog.at_level(logging.INFO):
            _job_health(catalog)

        assert "health check completed" in caplog.text
