"""cquant.scheduler.data_scheduler — APScheduler-based data pipeline scheduler.

Runs recurring jobs (all times CST):
  1.  price_ingest             — daily at 16:35
  2.  fundamentals             — daily at 17:00
  3.  valuation_daily          — daily at 17:30
  4.  alerts                   — daily at 18:00
  5.  health                   — daily at 08:00
  6.  daily_prediction         — daily at 18:05
  7.  weekly_retrain           — Sunday 20:00 (full ML pipeline)
  8.  gold_cleanup             — daily at 03:00
  9.  daily_prices             — daily at 18:00 (post-close price ingest)
  10. daily_valuation          — daily at 18:30 (post-close valuation update)
  11. quarterly_fundamentals   — daily at 19:00 (end-of-day fundamentals refresh)
  12. ext_indicator_refresh    — daily at 18:15 (builtin external indicators)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 2.0,
    **kwargs: Any,
) -> Any:
    """Call *fn* with exponential-backoff retry.

    Parameters
    ----------
    fn
        Callable to invoke.
    *args, **kwargs
        Forwarded to *fn*.
    max_retries
        Maximum number of retry attempts (default 3).
    base_delay
        Initial delay in seconds; doubled on each retry.

    Returns
    -------
    Any
        Return value of *fn* on success.

    Raises
    ------
    Exception
        Re-raises the last exception after all retries exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Attempt %d/%d failed (%s), retrying in %.1fs ...",
                attempt, max_retries, exc, delay,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

def _job_price_ingest(catalog: Any) -> None:
    """Incremental price ingestion: latest silver date -> today."""
    from cquant.core.enums import Frequency, Market
    from cquant.datahub.ingest import IngestionSpec, MarketIngestionOrchestrator
    from cquant.datahub.connectors.akshare_connector import AKShareConnector

    latest_df = catalog.query("SELECT MAX(trade_date) as last_date FROM silver_prices_1d")
    if latest_df.is_empty() or latest_df["last_date"][0] is None:
        from datetime import date, timedelta
        start_date = date.today() - timedelta(days=30)
    else:
        from datetime import date, timedelta
        start_date = latest_df["last_date"][0] + timedelta(days=1)

    from datetime import date
    end_date = date.today()
    if start_date > end_date:
        logger.info("DataScheduler: data is up to date, skipping price ingest")
        return

    logger.info("DataScheduler: price ingest %s -> %s", start_date, end_date)
    orchestrator = MarketIngestionOrchestrator(catalog, [AKShareConnector()])
    spec = IngestionSpec(
        market=Market.CN,
        symbols=[],  # empty = ingest all available
        start_date=start_date,
        end_date=end_date,
        frequency=Frequency.D1,
    )
    orchestrator.ingest(spec)
    logger.info("DataScheduler: price ingest completed")


def _job_fundamentals(catalog: Any) -> None:
    """Update fundamentals from the configured data source."""
    from cquant.datahub.pipelines.fundamentals_updater import update_fundamentals

    count = update_fundamentals(catalog, source="akshare")
    logger.info("DataScheduler: fundamentals updated (%d rows)", count)


def _job_valuation_daily(catalog: Any) -> None:
    """Update silver_valuation_daily for all assets (latest trade date)."""
    from datetime import date

    from cquant.datahub.connectors.tushare_connector import TushareConnector
    from cquant.datahub.pipelines.valuation_daily_updater import ValuationDailyUpdater

    asset_ids = _load_asset_ids(catalog)
    if not asset_ids:
        logger.info("DataScheduler: no assets found, skipping valuation daily update")
        return

    connector = TushareConnector()
    updater = ValuationDailyUpdater(catalog, connector)
    trade_date = date.today().isoformat()
    count = updater.update(asset_ids, trade_date)
    logger.info("DataScheduler: valuation daily updated (%d assets)", count)


def _load_asset_ids(catalog: Any) -> list[str]:
    """Load all asset IDs from silver_assets."""
    try:
        df = catalog.query("SELECT DISTINCT asset_id FROM silver_assets LIMIT 5000")
        return df["asset_id"].to_list() if not df.is_empty() else []
    except Exception:
        return []


def _job_alerts(catalog: Any) -> None:
    """Check for alert conditions (price limits, drawdown, etc.)."""
    logger.info("DataScheduler: alert check started")
    # Placeholder — integrate with riskguard alerts when available
    try:
        # Example: check for any positions exceeding risk thresholds
        df = catalog.query(
            "SELECT COUNT(*) AS cnt FROM gold_portfolio_snapshots "
            "WHERE trade_date = CURRENT_DATE"
        )
        if not df.is_empty():
            logger.info("DataScheduler: alert check completed, %d snapshots today", df["cnt"][0])
        else:
            logger.info("DataScheduler: alert check completed, no snapshots today")
    except Exception:
        logger.info("DataScheduler: alert check completed (no snapshot table)")


def _job_daily_prediction(catalog: Any) -> None:
    """Run daily prediction using the production model from the registry."""
    logger.info("DataScheduler: daily prediction started")
    try:
        from cquant.ml_lab.model_registry import ModelRegistry

        registry = ModelRegistry(catalog)

        # Find all production models
        prod_models = registry.list_models(stage="production")
        if not prod_models:
            logger.info("DataScheduler: no production models registered, skipping prediction")
            return

        from cquant.ml_lab.predict_service import run_online_prediction

        for model in prod_models:
            model_id = model["model_id"]
            model_version = model["model_version"]
            logger.info("DataScheduler: running prediction for %s (%s)", model_id, model_version)
            try:
                result = run_online_prediction(
                    catalog=catalog,
                    model_version=model_version,
                    top_n=50,
                )
                logger.info(
                    "DataScheduler: prediction completed for %s — %d assets, date=%s",
                    model_id, result.get("total_assets", 0), result.get("date", "unknown"),
                )
            except Exception as exc:
                logger.error("DataScheduler: prediction failed for %s: %s", model_id, exc)
    except Exception as exc:
        logger.error("DataScheduler: daily prediction job failed: %s", exc)


def _job_health(catalog: Any) -> None:
    """Daily health check: verify data freshness and table integrity."""
    logger.info("DataScheduler: health check started")
    checks: dict[str, str] = {}

    # Check silver_prices_1d freshness
    try:
        df = catalog.query("SELECT MAX(trade_date) AS latest FROM silver_prices_1d")
        if not df.is_empty() and df["latest"][0] is not None:
            checks["silver_prices_1d"] = f"latest={df['latest'][0]}"
        else:
            checks["silver_prices_1d"] = "EMPTY"
    except Exception as exc:
        checks["silver_prices_1d"] = f"ERROR: {exc}"

    # Check silver_fundamentals
    try:
        df = catalog.query("SELECT COUNT(*) AS cnt FROM silver_fundamentals")
        checks["silver_fundamentals"] = f"{df['cnt'][0]} rows"
    except Exception:
        checks["silver_fundamentals"] = "table missing"

    for table, status in checks.items():
        logger.info("  %-25s %s", table, status)

    logger.info("DataScheduler: health check completed")


def _job_gold_cleanup(catalog: Any) -> None:
    """Daily gold-layer retention cleanup.

    - Cascade-delete run-scoped gold data older than the retention window
      (default 90 days), keyed off ``gold_backtest_runs.completed_at``.
    - Evict the oldest ``feature_set_version`` slices from the shared factor
      cache (gold_factor_values), keeping the N newest.
    """
    from cquant.scheduler.cleanup import GoldTableCleaner

    cleaner = GoldTableCleaner(catalog)

    run_summary = cleaner.cleanup_run_scoped(retention_days=90)
    if run_summary.get("_expired_runs", 0) > 0:
        logger.info(
            "DataScheduler: gold run-scoped cleanup — %s",
            {k: v for k, v in run_summary.items() if not k.startswith("_")},
        )
    else:
        logger.info("DataScheduler: gold run-scoped cleanup — no expired runs")

    cache_summary = cleaner.cleanup_factor_cache(keep_versions=10)
    if cache_summary.get("versions_evicted", 0) > 0:
        logger.info(
            "DataScheduler: gold factor-cache cleanup — evicted %d versions, %d rows",
            cache_summary["versions_evicted"], cache_summary["rows_deleted"],
        )
    else:
        logger.info("DataScheduler: gold factor-cache cleanup — nothing evicted")


def _job_strategy_optimization(catalog: Any) -> None:
    """Weekly strategy optimization: run StrategyOptimizationJob for every
    configured strategy and notify via registered channels when a report
    requires human review. Reports are persisted by the job itself; this
    scheduler NEVER applies new parameters automatically.
    """
    import json as _json

    logger.info("DataScheduler: strategy optimization started")
    from cquant.scheduler.strategy_optimizer import (
        STATUS_NEEDS_REVIEW,
        StrategyOptimizationJob,
    )

    df = catalog.query(
        "SELECT strategy_id, parsed_config FROM meta_strategy_configs"
    )
    if df.is_empty():
        logger.info("DataScheduler: no strategies configured, skipping optimization")
        return

    job = StrategyOptimizationJob(catalog)
    needs_review: list[str] = []
    for row in df.to_dicts():
        strategy_id = row["strategy_id"]
        raw = row.get("parsed_config")
        if isinstance(raw, str):
            try:
                config = _json.loads(raw)
            except (TypeError, ValueError):
                config = {}
        elif isinstance(raw, dict):
            config = raw
        else:
            config = {}
        try:
            report = job.run(strategy_id, config)
        except Exception as exc:  # defensive: job.run already catches
            logger.error("Strategy optimization crashed for %s: %s", strategy_id, exc)
            continue
        if report.status == STATUS_NEEDS_REVIEW:
            needs_review.append(strategy_id)

    if needs_review:
        logger.info(
            "DataScheduler: %d strategies need optimization review: %s",
            len(needs_review), ", ".join(needs_review),
        )
        _notify_needs_review(catalog, needs_review)
    else:
        logger.info("DataScheduler: strategy optimization completed — no reviews needed")


def _notify_needs_review(catalog: Any, strategy_ids: list[str]) -> None:
    """Send an alert through all enabled notification channels (best-effort)."""
    import json as _json

    from cquant.api_server.notification_channel import get_channel

    try:
        df = catalog.query(
            "SELECT channel_type, config_json FROM meta_notification_channels "
            "WHERE enabled = TRUE"
        )
    except Exception:
        df = None

    channels: list[Any] = []
    if df is not None and not df.is_empty():
        for row in df.to_dicts():
            try:
                ch = get_channel(row["channel_type"], _json.loads(row["config_json"]))
                if ch is not None:
                    channels.append(ch)
            except Exception:
                continue
    if not channels:
        logger.info("DataScheduler: no notification channels configured, log-only alert")
        return

    title = "策略优化建议待审核"
    message = (
        f"以下 {len(strategy_ids)} 个策略的自动寻优报告需要人工审核：\n"
        + "\n".join(f"- {sid}" for sid in strategy_ids)
        + "\n请前往 策略页面 → 优化报告 查看详情并确认是否应用新参数。"
    )
    for ch in channels:
        try:
            ch.send(title, message, severity="warning")
        except Exception as exc:
            logger.warning("Notification via %s failed: %s", ch.channel_type, exc)


def _job_ext_indicator_refresh(catalog: Any) -> None:
    """Refresh builtin external indicators via adapters (akshare/tushare)."""
    from cquant.datahub.pipelines.indicator_sources.refresh import (
        run_external_indicator_refresh,
    )

    summary = run_external_indicator_refresh(catalog, trigger="scheduled")
    logger.info(
        "DataScheduler: ext indicator refresh completed — %d ok / %d failed",
        summary.ok_count, summary.error_count,
    )


def _job_weekly_retrain(catalog: Any) -> None:
    """Weekly retrain: run the full automated ML pipeline."""
    logger.info("DataScheduler: weekly retrain started")
    try:
        from cquant.pipeline.config import PipelineConfig
        from cquant.pipeline.orchestrator import PipelineOrchestrator

        config = PipelineConfig()
        orchestrator = PipelineOrchestrator(catalog, config)
        result = orchestrator.run_full_pipeline()

        logger.info(
            "DataScheduler: weekly retrain completed — run_id=%s, status=%s, duration=%.1fs",
            result.get("run_id", "?"),
            result.get("status", "?"),
            result.get("duration_seconds", 0),
        )
    except Exception as exc:
        logger.error("DataScheduler: weekly retrain failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# DataScheduler
# ---------------------------------------------------------------------------

class DataScheduler:
    """APScheduler-based scheduler for cQuant data pipelines.

    Parameters
    ----------
    catalog
        An initialised ``Catalog`` (DuckDB) instance.
    timezone
        IANA timezone string (default ``"Asia/Shanghai"``).
    """

    def __init__(self, catalog: Any, timezone: str = "Asia/Shanghai") -> None:
        self._catalog = catalog
        self._tz = timezone
        self._scheduler: Any = None
        self._running = False

        # Ensure run-tracking table exists
        try:
            self._catalog.execute(
                "CREATE TABLE IF NOT EXISTS _scheduler_runs "
                "(job_id VARCHAR, last_run TIMESTAMP, status VARCHAR)"
            )
        except Exception as exc:
            logger.warning("Failed to create _scheduler_runs table: %s", exc)

    # ---- public API -------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler (blocking call)."""
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        sched = BlockingScheduler(timezone=self._tz)

        # 1. Price ingest — daily at 16:35
        sched.add_job(
            self._run_price_ingest,
            CronTrigger(hour=16, minute=35, timezone=self._tz),
            id="price_ingest",
            name="Price Ingest",
            replace_existing=True,
        )

        # 2. Fundamentals — daily at 17:00
        sched.add_job(
            self._run_fundamentals,
            CronTrigger(hour=17, minute=0, timezone=self._tz),
            id="fundamentals",
            name="Fundamentals Update",
            replace_existing=True,
        )

        # 3. Valuation daily — daily at 17:30 (after fundamentals)
        sched.add_job(
            self._run_valuation_daily,
            CronTrigger(hour=17, minute=30, timezone=self._tz),
            id="valuation_daily",
            name="Valuation Daily Update",
            replace_existing=True,
        )

        # 4. Alerts — daily at 18:00 (placeholder: logging only until riskguard integration)
        sched.add_job(
            self._run_alerts,
            CronTrigger(hour=18, minute=0, timezone=self._tz),
            id="alerts",
            name="Alert Check",
            replace_existing=True,
        )

        # 5. Health — daily at 08:00
        sched.add_job(
            self._run_health,
            CronTrigger(hour=8, minute=0, timezone=self._tz),
            id="health",
            name="Health Check",
            replace_existing=True,
        )

        # 6. Daily Prediction — daily at 18:05 (after market close + data ingest)
        sched.add_job(
            self._run_daily_prediction,
            CronTrigger(hour=18, minute=5, timezone=self._tz),
            id="daily_prediction",
            name="Daily Prediction",
            replace_existing=True,
        )

        # 7. Weekly Retrain — Sunday 20:00 (full ML pipeline)
        sched.add_job(
            self._run_weekly_retrain,
            CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=self._tz),
            id="weekly_retrain",
            name="Weekly Retrain",
            replace_existing=True,
        )

        # 8. Gold-layer cleanup — daily at 03:00 (off-peak retention sweep)
        sched.add_job(
            self._run_gold_cleanup,
            CronTrigger(hour=3, minute=0, timezone=self._tz),
            id="gold_cleanup",
            name="Gold Table Cleanup",
            replace_existing=True,
        )

        # 9. Strategy optimization — Sunday 20:30 (after weekly retrain,
        #    clear of the 18-19h data-pipeline window)
        sched.add_job(
            self._run_strategy_optimization,
            CronTrigger(day_of_week="sun", hour=20, minute=30, timezone=self._tz),
            id="strategy_optimization",
            name="Strategy Optimization",
            replace_existing=True,
        )

        # 12. External indicator refresh — daily at 18:15 (clear of the
        #     16:35-18:05 data-pipeline window, before 18:30 valuation)
        sched.add_job(
            self._run_ext_indicator_refresh,
            CronTrigger(hour=18, minute=15, timezone=self._tz),
            id="ext_indicator_refresh",
            name="External Indicator Refresh",
            replace_existing=True,
        )

        self._scheduler = sched
        self._running = True
        logger.info("DataScheduler started with %d jobs (tz=%s)", len(sched.get_jobs()), self._tz)

        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("DataScheduler stopped")

    def status(self) -> dict[str, Any]:
        """Return a snapshot of scheduler state and job info."""
        jobs_info: list[dict[str, Any]] = []
        if self._scheduler is not None:
            for job in self._scheduler.get_jobs():
                jobs_info.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                })
        return {
            "running": self._running,
            "timezone": self._tz,
            "jobs": jobs_info,
        }

    def run_task(self, task_name: str) -> None:
        """Manually trigger a single task by name.

        Parameters
        ----------
        task_name
            One of ``price-ingest``, ``fundamentals``, ``alerts``, ``health``.
        """
        dispatch = {
            "price-ingest": self._run_price_ingest,
            "fundamentals": self._run_fundamentals,
            "valuation-daily": self._run_valuation_daily,
            "alerts": self._run_alerts,
            "health": self._run_health,
            "daily-prediction": self._run_daily_prediction,
            "weekly-retrain": self._run_weekly_retrain,
            "gold-cleanup": self._run_gold_cleanup,
            "daily-prices": self._run_daily_prices,
            "daily-valuation": self._run_daily_valuation,
            "quarterly-fundamentals": self._run_quarterly_fundamentals,
            "strategy-optimization": self._run_strategy_optimization,
            "ext-ind-refresh": self._run_ext_indicator_refresh,
        }
        fn = dispatch.get(task_name)
        if fn is None:
            raise ValueError(
                f"Unknown task {task_name!r}. "
                f"Available: {', '.join(dispatch)}"
            )
        fn()

    # ---- internal runners -------------------------------------------------

    def _run_price_ingest(self) -> None:
        logger.info("Running price ingest ...")
        try:
            _with_retry(_job_price_ingest, self._catalog)
            self._record_run("price_ingest")
        except Exception as exc:
            logger.error("Price ingest failed after retries: %s", exc)
            self._record_run("price_ingest", "failure")

    def _run_fundamentals(self) -> None:
        logger.info("Running fundamentals update ...")
        try:
            _with_retry(_job_fundamentals, self._catalog)
            self._record_run("fundamentals")
        except Exception as exc:
            logger.error("Fundamentals update failed after retries: %s", exc)
            self._record_run("fundamentals", "failure")

    def _run_valuation_daily(self) -> None:
        logger.info("Running valuation daily update ...")
        try:
            _with_retry(_job_valuation_daily, self._catalog)
            self._record_run("valuation_daily")
        except Exception as exc:
            logger.error("Valuation daily update failed after retries: %s", exc)
            self._record_run("valuation_daily", "failure")

    def _run_alerts(self) -> None:
        logger.info("Running alert check ...")
        try:
            _with_retry(_job_alerts, self._catalog)
            self._record_run("alerts")
        except Exception as exc:
            logger.error("Alert check failed after retries: %s", exc)
            self._record_run("alerts", "failure")

    def _run_health(self) -> None:
        logger.info("Running health check ...")
        try:
            _with_retry(_job_health, self._catalog)
            self._record_run("health")
        except Exception as exc:
            logger.error("Health check failed after retries: %s", exc)
            self._record_run("health", "failure")

    def _run_daily_prediction(self) -> None:
        logger.info("Running daily prediction ...")
        try:
            _with_retry(_job_daily_prediction, self._catalog)
            self._record_run("daily_prediction")
        except Exception as exc:
            logger.error("Daily prediction failed after retries: %s", exc)
            self._record_run("daily_prediction", "failure")

    def _run_weekly_retrain(self) -> None:
        logger.info("Running weekly retrain ...")
        try:
            _with_retry(_job_weekly_retrain, self._catalog)
            self._record_run("weekly_retrain")
        except Exception as exc:
            logger.error("Weekly retrain failed after retries: %s", exc)
            self._record_run("weekly_retrain", "failure")

    def _run_gold_cleanup(self) -> None:
        logger.info("Running gold-layer cleanup ...")
        try:
            _with_retry(_job_gold_cleanup, self._catalog)
            self._record_run("gold_cleanup")
        except Exception as exc:
            logger.error("Gold cleanup failed after retries: %s", exc)
            self._record_run("gold_cleanup", "failure")

    def _run_daily_prices(self) -> None:
        """Post-close daily price ingest — reuses the price-ingest pipeline."""
        logger.info("Running daily prices ingest ...")
        try:
            _with_retry(_job_price_ingest, self._catalog)
            self._record_run("daily_prices")
        except Exception as exc:
            logger.error("Daily prices ingest failed after retries: %s", exc)
            self._record_run("daily_prices", "failure")

    def _run_daily_valuation(self) -> None:
        """Post-close daily valuation update — reuses the valuation pipeline."""
        logger.info("Running daily valuation update ...")
        try:
            _with_retry(_job_valuation_daily, self._catalog)
            self._record_run("daily_valuation")
        except Exception as exc:
            logger.error("Daily valuation update failed after retries: %s", exc)
            self._record_run("daily_valuation", "failure")

    def _run_quarterly_fundamentals(self) -> None:
        """End-of-day fundamentals refresh — reuses the fundamentals pipeline."""
        logger.info("Running quarterly fundamentals update ...")
        try:
            _with_retry(_job_fundamentals, self._catalog)
            self._record_run("quarterly_fundamentals")
        except Exception as exc:
            logger.error("Quarterly fundamentals update failed after retries: %s", exc)
            self._record_run("quarterly_fundamentals", "failure")

    def _run_strategy_optimization(self) -> None:
        logger.info("Running strategy optimization ...")
        try:
            _with_retry(_job_strategy_optimization, self._catalog)
            self._record_run("strategy_optimization")
        except Exception as exc:
            logger.error("Strategy optimization failed after retries: %s", exc)
            self._record_run("strategy_optimization", "failure")

    def _run_ext_indicator_refresh(self) -> None:
        logger.info("Running external indicator refresh ...")
        try:
            _with_retry(_job_ext_indicator_refresh, self._catalog)
            self._record_run("ext_indicator_refresh")
        except Exception as exc:
            logger.error("External indicator refresh failed after retries: %s", exc)
            self._record_run("ext_indicator_refresh", "failure")

    def _record_run(self, job_id: str, status: str = "success") -> None:
        """Persist last-run metadata into the catalog (best-effort)."""
        try:
            self._catalog.execute(
                "INSERT INTO _scheduler_runs VALUES (?, ?, ?)",
                [job_id, datetime.now(tz=timezone.utc), status],
            )
        except Exception:
            logger.debug("Failed to record scheduler run for %s", job_id, exc_info=True)
