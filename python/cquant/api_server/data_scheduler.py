"""DataScheduler — 每日自动增量摄取行情数据。"""
from __future__ import annotations
from cquant.core.errors import IngestError

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SCHEDULER_STATE: dict[str, Any] = {
    "enabled": True,
    "last_run": None,      # ISO datetime str
    "last_status": None,   # "success" | "error" | "running"
    "last_error": None,
}
_SCHEDULER_INSTANCE: Any = None  # set by start_data_scheduler()


def mark_scheduler_running() -> None:
    """Mark the scheduler as running (called before enqueuing a trigger job)."""
    _SCHEDULER_STATE["last_status"] = "running"


def get_scheduler_state() -> dict:
    state = dict(_SCHEDULER_STATE)
    # Dynamically read next_run_time so it stays fresh after each job fires
    try:
        if _SCHEDULER_INSTANCE is not None:
            job = _SCHEDULER_INSTANCE.get_job("daily_ingest")
            state["next_run"] = job.next_run_time.isoformat() if job and job.next_run_time else None
        else:
            state["next_run"] = None
    except Exception:
        state["next_run"] = None
    return state


def run_incremental_ingest(catalog) -> None:
    """执行增量摄取：从 silver_prices_1d 找最新日期，拉取到今日。"""
    _SCHEDULER_STATE["last_status"] = "running"
    _SCHEDULER_STATE["last_run"] = datetime.now(tz=timezone.utc).isoformat()
    _SCHEDULER_STATE["connectors"] = {}
    _SCHEDULER_STATE["progress_total"] = 0
    try:
        from cquant.datahub.ingest import MarketIngestionOrchestrator, IngestionSpec
        from cquant.datahub.connectors.akshare_connector import AKShareConnector
        from datetime import date

        # 找上次摄取的最新日期
        try:
            latest_df = catalog.query("SELECT MAX(trade_date) as last_date FROM silver_prices_1d")
            if latest_df.is_empty() or "last_date" not in latest_df.columns or latest_df["last_date"][0] is None:
                start_date = date.today() - timedelta(days=30)
            else:
                start_date = latest_df["last_date"][0] + timedelta(days=1)
        except Exception:
            start_date = date.today() - timedelta(days=30)

        end_date = date.today()
        if start_date > end_date:
            logger.info("DataScheduler: data is up to date, skipping ingest")
            _SCHEDULER_STATE["last_status"] = "success"
            return

        logger.info("DataScheduler: ingesting %s ~ %s", start_date, end_date)
        from cquant.core.enums import Market, Frequency
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # 构建可用连接器列表
        connectors = []
        
        # 1. Tushare（如果配置了 token）
        try:
            from cquant.datahub.connectors.tushare_connector import TushareConnector
            import os
            if os.environ.get("TUSHARE_TOKEN"):
                connectors.append(("tushare", TushareConnector()))
                logger.info("DataScheduler: Tushare connector available")
        except Exception as e:
            logger.debug("DataScheduler: Tushare not available: %s", e)
        
        # 2. AKShare
        try:
            from cquant.datahub.connectors.akshare_connector import AKShareConnector
            connectors.append(("akshare", AKShareConnector()))
            logger.info("DataScheduler: AKShare connector available")
        except Exception as e:
            logger.debug("DataScheduler: AKShare not available: %s", e)
        
        if not connectors:
            raise IngestError("No data connectors available")
        
        # 获取资产列表
        try:
            asset_df = catalog.query(
                "SELECT asset_id FROM silver_assets WHERE status = 'active' AND exchange IN ('SSE', 'SZSE', 'BSE') ORDER BY asset_id"
            )
            all_symbols = asset_df["asset_id"].to_list() if not asset_df.is_empty() else []
        except Exception:
            all_symbols = []
        
        if not all_symbols:
            raise IngestError("No symbols found in silver_assets")
        
        _SCHEDULER_STATE["progress_total"] = len(all_symbols)
        
        # 将股票列表分配给各连接器（不重复）
        num_connectors = len(connectors)
        symbols_per_connector = len(all_symbols) // num_connectors
        remainder = len(all_symbols) % num_connectors
        
        symbol_assignments = []
        start_idx = 0
        for i, (name, connector) in enumerate(connectors):
            count = symbols_per_connector + (1 if i < remainder else 0)
            assigned_symbols = all_symbols[start_idx:start_idx + count]
            symbol_assignments.append((name, connector, assigned_symbols))
            start_idx += count
            logger.info("DataScheduler: %s assigned %d symbols", name, len(assigned_symbols))
        
        # 并行执行各连接器（各自负责不同股票）
        def try_connector_with_symbols(args):
            name, connector, symbols = args
            _SCHEDULER_STATE["connectors"][name] = {
                "status": "running", "current": 0, "total": len(symbols),
                "last_symbol": "", "error": "",
            }
            try:
                logger.info("DataScheduler: %s starting (%d symbols)", name, len(symbols))
                from cquant.datahub.ingest import MarketIngestionOrchestrator, IngestionSpec
                orchestrator = MarketIngestionOrchestrator(catalog, [connector])
                spec = IngestionSpec(
                    market=Market.CN, symbols=symbols,
                    start_date=start_date, end_date=end_date,
                    frequency=Frequency.D1,
                )
                orchestrator.ingest(spec)
                _SCHEDULER_STATE["connectors"][name]["status"] = "success"
                logger.info("DataScheduler: %s completed (%d symbols)", name, len(symbols))
                return (name, True, None)
            except Exception as e:
                _SCHEDULER_STATE["connectors"][name]["status"] = "failed"
                _SCHEDULER_STATE["connectors"][name]["error"] = str(e)[:200]
                logger.warning("DataScheduler: %s failed: %s", name, e)
                return (name, False, e)
        
        with ThreadPoolExecutor(max_workers=num_connectors) as executor:
            futures = [executor.submit(try_connector_with_symbols, args) for args in symbol_assignments]
            results = [f.result() for f in as_completed(futures)]
        
        successes = [r for r in results if r[1]]
        failures = [r for r in results if not r[1]]
        
        if successes:
            logger.info("DataScheduler: %d/%d connectors succeeded", len(successes), num_connectors)
        if failures and not successes:
            raise IngestError(f"All connectors failed. Last error: {failures[0][2]}")
        _SCHEDULER_STATE["last_status"] = "success"
        _SCHEDULER_STATE["last_error"] = None
        logger.info("DataScheduler: ingest completed")
    except Exception as e:
        logger.exception("DataScheduler: ingest failed")
        _SCHEDULER_STATE["last_status"] = "error"
        _SCHEDULER_STATE["last_error"] = repr(e)[:200]


def run_ext_indicator_refresh_job(catalog) -> None:
    """刷新 builtin 外部指标（调度触发，trigger='scheduled'）。"""
    from cquant.datahub.pipelines.indicator_sources.refresh import (
        run_external_indicator_refresh,
    )

    run_external_indicator_refresh(catalog, trigger="scheduled")


def start_data_scheduler(catalog) -> Any:
    """启动 APScheduler，注册每日 16:35 的摄取任务与 18:15 的外部指标刷新任务。"""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; data auto-scheduler disabled")
        return None

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        run_incremental_ingest,
        CronTrigger(hour=16, minute=35, timezone="Asia/Shanghai"),
        id="daily_ingest",
        args=[catalog],
        replace_existing=True,
    )
    # 外部指标刷新 — 每日 18:15（错开 16:35/17:00/17:30/18:00/18:05 既有 job）
    scheduler.add_job(
        run_ext_indicator_refresh_job,
        CronTrigger(hour=18, minute=15, timezone="Asia/Shanghai"),
        id="ext_indicator_refresh",
        args=[catalog],
        replace_existing=True,
    )
    scheduler.start()
    global _SCHEDULER_INSTANCE
    _SCHEDULER_INSTANCE = scheduler

    job = scheduler.get_job("daily_ingest")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    logger.info("DataScheduler started, next run: %s", next_run)
    return scheduler
