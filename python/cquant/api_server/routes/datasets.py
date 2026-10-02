"""Dataset catalog routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from cquant.api_server.deps import CatalogDep, run_job_async
from cquant.api_server.schemas.common import UniverseCreateBody

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/datasets", tags=["datasets"])

_UNIVERSE_DDL = """
CREATE TABLE IF NOT EXISTS meta_custom_universes (
    universe_id VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    asset_ids   VARCHAR NOT NULL,
    filter_type VARCHAR DEFAULT 'custom',
    filter_value VARCHAR DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
_universe_table_ensured = False


def _ensure_universe_table(catalog) -> None:
    global _universe_table_ensured
    if _universe_table_ensured:
        return
    try:
        catalog.execute(_UNIVERSE_DDL)
        _universe_table_ensured = True
    except Exception as exc:
        logger.debug("_ensure_universe_table: %s", exc)


@router.get("")
async def list_datasets(catalog: CatalogDep, limit: int = 50) -> dict:
    """List registered dataset versions."""
    df = catalog.query(
        "SELECT version_id, dataset_name, frequency, start_date, end_date, "
        "asset_count, row_count, source, created_at, is_current "
        "FROM silver_dataset_versions "
        "ORDER BY created_at DESC LIMIT ?",
        [limit],
    )
    return {"items": df.to_dicts(), "total": df.height}


@router.get("/quality")
async def get_dataset_quality(
    catalog: CatalogDep,
    version: str = "",
    sample_assets: int = 20,
) -> dict:
    """返回数据集的数据质量报告。"""
    import polars as pl

    has_version_col = False
    if version:
        try:
            catalog.execute("SELECT dataset_version FROM silver_prices_1d LIMIT 1")
            has_version_col = True
        except Exception:
            pass

    ver_cond = "AND dataset_version = ?" if has_version_col and version else ""
    ver_params = [version] if has_version_col and version else []

    # Anchor all recency windows to the data's own latest trade_date, not
    # CURRENT_DATE — stale-but-healthy datasets must still report coverage.
    anchor_df = catalog.query(
        f"SELECT MAX(trade_date) as anchor FROM silver_prices_1d WHERE 1=1 {ver_cond}",
        ver_params,
    )
    anchor = (
        anchor_df.to_dicts()[0].get("anchor") if not anchor_df.is_empty() else None
    )
    if anchor is None:
        from datetime import date as _date
        anchor = _date.today()
    anchor_30 = str(anchor)[:10]
    from datetime import datetime as _dt, timedelta as _td
    a30 = (_dt.strptime(anchor_30, "%Y-%m-%d") - _td(days=30)).strftime("%Y-%m-%d")
    a90 = (_dt.strptime(anchor_30, "%Y-%m-%d") - _td(days=90)).strftime("%Y-%m-%d")

    basic_df = catalog.query(
        f"SELECT COUNT(DISTINCT asset_id) as n_assets, "
        f"MIN(trade_date) as min_date, "
        f"MAX(trade_date) as max_date, "
        f"COUNT(*) as total_rows "
        f"FROM silver_prices_1d "
        f"WHERE asset_id NOT LIKE '%:88%' {ver_cond}",
        ver_params,
    )
    stats = basic_df.to_dicts()[0] if not basic_df.is_empty() else {}

    recent_df = catalog.query(
        f"SELECT COUNT(DISTINCT asset_id) as recent_assets "
        f"FROM silver_prices_1d "
        f"WHERE asset_id NOT LIKE '%:88%' "
        f"AND trade_date >= ? {ver_cond}",
        [a30] + ver_params,
    )
    stats["recent_assets"] = (
        recent_df.to_dicts()[0].get("recent_assets", 0) if not recent_df.is_empty() else 0
    )

    null_df = catalog.query(
        f"SELECT "
        f"  CAST(SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) as null_rate "
        f"FROM silver_prices_1d WHERE 1=1 {ver_cond}",
        ver_params,
    )
    stats["null_rate"] = (
        null_df.to_dicts()[0].get("null_rate") or 0.0 if not null_df.is_empty() else 0.0
    )

    try:
        outlier_df = catalog.query(
            f"SELECT COUNT(*) as n_outliers FROM ("
            f"  SELECT ABS(close / LAG(close) OVER (PARTITION BY asset_id ORDER BY trade_date) - 1) as dr "
            f"  FROM silver_prices_1d WHERE 1=1 {ver_cond}"
            f") t WHERE dr > 0.25",
            ver_params,
        )
        stats["outlier_count"] = (
            outlier_df.to_dicts()[0].get("n_outliers", 0) if not outlier_df.is_empty() else 0
        )
    except Exception:
        stats["outlier_count"] = 0

    daily_df = catalog.query(
        f"SELECT trade_date, COUNT(DISTINCT asset_id) as n_assets "
        f"FROM silver_prices_1d "
        f"WHERE asset_id NOT LIKE '%:88%' "
        f"AND trade_date >= ? {ver_cond} "
        f"GROUP BY trade_date ORDER BY trade_date",
        [a30] + ver_params,
    )
    daily_coverage = (
        [{"trade_date": str(r["trade_date"]), "n_assets": r["n_assets"]}
         for r in daily_df.to_dicts()]
        if not daily_df.is_empty()
        else []
    )

    bottom_df = catalog.query(
        f"SELECT asset_id, COUNT(*) as valid_days "
        f"FROM silver_prices_1d "
        f"WHERE asset_id NOT LIKE '%:88%' "
        f"AND trade_date >= ? {ver_cond} "
        f"GROUP BY asset_id ORDER BY valid_days ASC LIMIT ?",
        [a90] + ver_params + [sample_assets],
    )
    bottom_assets = bottom_df.to_dicts() if not bottom_df.is_empty() else []

    return {
        "version": version or "all",
        "stats": stats,
        "daily_coverage": daily_coverage,
        "bottom_assets": bottom_assets,
    }


@router.get("/universes")
async def list_universes(catalog: CatalogDep) -> dict:
    """列出可用的股票池。"""
    predefined = [
        {"id": "all", "name": "全部股票", "description": "不限制股票池"},
        {"id": "sse", "name": "沪市主板", "description": "仅上海证券交易所主板"},
        {"id": "szse", "name": "深市主板", "description": "仅深圳证券交易所主板"},
        {"id": "cyb", "name": "创业板", "description": "深圳创业板 (300xxx)"},
        {"id": "kcb", "name": "科创板", "description": "上海科创板 (688xxx)"},
        {"id": "bse", "name": "北交所", "description": "北京证券交易所 (8xxxxx)"},
        {"id": "idx_sse", "name": "上证指数成分股", "description": "上海证券交易所综合指数成分股"},
        {"id": "idx_szse", "name": "深证成指成分股", "description": "深圳证券交易所成份指数成分股"},
        {"id": "idx_hs300", "name": "沪深300", "description": "沪深300指数成分股（大盘蓝筹）"},
        {"id": "idx_zz500", "name": "中证500", "description": "中证500指数成分股（中盘成长）"},
        {"id": "idx_zz1000", "name": "中证1000", "description": "中证1000指数成分股（小盘）"},
        {"id": "idx_cyb", "name": "创业板指", "description": "创业板指数成分股 (399006)"},
        {"id": "idx_kcb50", "name": "科创50", "description": "上证科创板50成分指数"},
    ]
    try:
        # Anchor to the data's latest trade_date (not CURRENT_DATE) and exclude
        # sector indices ('88' symbol prefix) from the stock count.
        count_df = catalog.query(
            "SELECT COUNT(DISTINCT asset_id) AS n FROM silver_prices_1d "
            "WHERE asset_id NOT LIKE '%:88%' "
            "AND trade_date >= (SELECT MAX(trade_date) FROM silver_prices_1d) - INTERVAL '30 days'"
        )
        total_assets = int(count_df["n"][0]) if not count_df.is_empty() else 0
    except Exception:
        total_assets = 0
    return {
        "predefined": predefined,
        "total_assets": total_assets,
    }


@router.post("/universes")
async def create_universe(body: UniverseCreateBody, catalog: CatalogDep) -> dict:
    """创建自定义股票池。"""
    import json
    import uuid

    _ensure_universe_table(catalog)
    universe_id = f"custom_{uuid.uuid4().hex[:8]}"
    catalog.execute(
        "INSERT INTO meta_custom_universes (universe_id, name, asset_ids, filter_type, filter_value) "
        "VALUES (?, ?, ?, ?, ?)",
        [universe_id, body.name, json.dumps(body.asset_ids), body.filter_type, body.filter_value],
    )
    return {"universe_id": universe_id, "name": body.name}


@router.get("/schedule")
async def get_schedule_status(catalog: CatalogDep) -> dict:
    """返回数据调度状态。"""
    from cquant.api_server.data_scheduler import get_scheduler_state
    state = get_scheduler_state()
    # 同时返回 freshness（最后一次 silver 数据日期）
    try:
        df = catalog.query("SELECT MAX(trade_date) as d FROM silver_prices_1d")
        last_data = str(df["d"][0]) if not df.is_empty() and df["d"][0] else None
    except Exception:
        last_data = None
    return {**state, "last_data_date": last_data}


@router.post("/schedule/trigger")
async def trigger_ingest(background_tasks: BackgroundTasks, catalog: CatalogDep) -> dict:
    """手动触发一次增量摄取。"""
    from cquant.api_server.data_scheduler import run_incremental_ingest, get_scheduler_state, mark_scheduler_running
    if get_scheduler_state().get("last_status") == "running":
        return {"status": "already_running"}
    # Set running BEFORE enqueuing to prevent TOCTOU race on concurrent requests
    mark_scheduler_running()
    background_tasks.add_task(run_job_async, run_incremental_ingest, catalog)
    return {"status": "triggered"}


@router.get("/freshness")
async def get_data_freshness(catalog: CatalogDep) -> dict:
    """返回最近一次数据更新时间和距今天数。"""
    try:
        df = catalog.query("SELECT MAX(trade_date) as last_date FROM silver_prices_1d")
        if df.is_empty() or df["last_date"][0] is None:
            return {"last_updated": None, "days_stale": -1}
        last_date = str(df["last_date"][0])
        from datetime import date
        days_stale = (date.today() - date.fromisoformat(last_date)).days
        return {"last_updated": last_date, "days_stale": days_stale}
    except Exception as exc:
        logger.debug("get_data_freshness failed: %s", exc)
        return {"last_updated": None, "days_stale": -1}


@router.get("/corporate-actions")
async def get_corporate_actions(
    catalog: CatalogDep,
    asset_id: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """返回单只资产的公司行为历史（分红/除权），按 ex_date 升序。

    表可能为空或不存在——统一返回空列表不报错（数据浏览器/前端
    直接渲染）。``asset_id`` 为空时返回全表最近 ``limit`` 条。
    """
    try:
        if asset_id:
            df = catalog.query(
                "SELECT action_id, asset_id, action_type, ex_date, record_date, "
                "pay_date, ratio, cash_amount, currency, description, source "
                "FROM silver_corporate_actions "
                "WHERE asset_id = ? ORDER BY ex_date ASC LIMIT ?",
                [asset_id, limit],
            )
        else:
            df = catalog.query(
                "SELECT action_id, asset_id, action_type, ex_date, record_date, "
                "pay_date, ratio, cash_amount, currency, description, source "
                "FROM silver_corporate_actions "
                "ORDER BY ex_date DESC LIMIT ?",
                [limit],
            )
    except Exception as exc:
        logger.debug("get_corporate_actions failed: %s", exc)
        return {"items": [], "total": 0, "asset_id": asset_id}

    items = [
        {
            **row,
            "ex_date": str(row.get("ex_date") or ""),
            "record_date": str(row.get("record_date") or ""),
            "pay_date": str(row.get("pay_date") or ""),
        }
        for row in df.to_dicts()
    ] if not df.is_empty() else []
    return {"items": items, "total": len(items), "asset_id": asset_id}


@router.get("/backtest-trend")
async def get_backtest_trend(catalog: CatalogDep, days: int = 30) -> dict:
    """返回近 N 天每日回测数量趋势。"""
    try:
        df = catalog.query(
            "SELECT DATE(started_at) as date, COUNT(*) as count "
            "FROM gold_backtest_runs "
            "WHERE started_at >= CURRENT_DATE - ? * INTERVAL '1 DAY' "
            "GROUP BY DATE(started_at) "
            "ORDER BY date",
            [days],
        )
        items = [
            {"date": str(r["date"]), "count": r["count"]}
            for r in df.to_dicts()
        ] if not df.is_empty() else []
        return {"items": items, "days": days}
    except Exception as exc:
        logger.debug("get_backtest_trend failed: %s", exc)
        return {"items": [], "days": days}


@router.get("/compare")
async def compare_datasets(
    catalog: CatalogDep,
    version_a: str = "",
    version_b: str = "",
) -> dict:
    """Compare two dataset versions.

    Returns row-level and field-level differences between two versions,
    including per-field statistics when the data table supports versioning.
    """
    if not version_a or not version_b:
        raise HTTPException(
            status_code=400,
            detail="Both version_a and version_b query parameters are required.",
        )
    if version_a == version_b:
        raise HTTPException(
            status_code=400,
            detail="version_a and version_b must be different.",
        )

    # -- Fetch metadata for both versions ------------------------------------
    meta_a = catalog.query(
        "SELECT * FROM silver_dataset_versions WHERE version_id = ?",
        [version_a],
    )
    meta_b = catalog.query(
        "SELECT * FROM silver_dataset_versions WHERE version_id = ?",
        [version_b],
    )
    if meta_a.is_empty():
        raise HTTPException(status_code=404, detail=f"Version '{version_a}' not found")
    if meta_b.is_empty():
        raise HTTPException(status_code=404, detail=f"Version '{version_b}' not found")

    a = meta_a.to_dicts()[0]
    b = meta_b.to_dicts()[0]

    row_changes = {
        "version_a_count": int(a.get("row_count") or 0),
        "version_b_count": int(b.get("row_count") or 0),
        "added": max(0, int(b.get("row_count") or 0) - int(a.get("row_count") or 0)),
        "removed": max(0, int(a.get("row_count") or 0) - int(b.get("row_count") or 0)),
    }

    # -- Detect whether the data table has a dataset_version column -----------
    has_version_col = False
    try:
        catalog.execute("SELECT dataset_version FROM silver_prices_1d LIMIT 1")
        has_version_col = True
    except Exception:
        logger.debug("silver_prices_1d has no dataset_version column")

    # -- Field schema comparison (from silver_prices_1d columns) ---------------
    field_changes: dict = {
        "added_fields": [],
        "removed_fields": [],
        "common_fields": [],
    }
    field_stats: list[dict] = []

    if has_version_col:
        try:
            cols_a_df = catalog.query(
                "SELECT DISTINCT COLUMNS(*) FROM silver_prices_1d "
                "WHERE dataset_version = ? LIMIT 0",
                [version_a],
            )
            cols_a = set(cols_a_df.columns)
        except Exception:
            cols_a = set()

        try:
            cols_b_df = catalog.query(
                "SELECT DISTINCT COLUMNS(*) FROM silver_prices_1d "
                "WHERE dataset_version = ? LIMIT 0",
                [version_b],
            )
            cols_b = set(cols_b_df.columns)
        except Exception:
            cols_b = set()

        if cols_a or cols_b:
            field_changes["added_fields"] = sorted(cols_b - cols_a)
            field_changes["removed_fields"] = sorted(cols_a - cols_b)
            field_changes["common_fields"] = sorted(cols_a & cols_b)

        # -- Per-field numeric statistics ------------------------------------
        numeric_types = {"BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "SMALLINT", "TINYINT"}
        try:
            describe_df = catalog.query("DESCRIBE silver_prices_1d")
            all_cols = [
                r["column_name"]
                for r in describe_df.to_dicts()
                if r.get("column_type", "").upper().split("(")[0].strip() in numeric_types
            ]
        except Exception:
            all_cols = []

        non_stat_cols = {"asset_id", "trade_date", "dataset_version", "ingestion_id"}
        stat_cols = [c for c in all_cols if c not in non_stat_cols]

        for col in stat_cols:
            try:
                stats_a_df = catalog.query(
                    f"SELECT "
                    f"  MIN({col}) as col_min, "
                    f"  MAX({col}) as col_max, "
                    f"  AVG(CAST({col} AS DOUBLE)) as col_mean, "
                    f"  CAST(SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS DOUBLE) "
                    f"    / COUNT(*) as null_rate "
                    f"FROM silver_prices_1d WHERE dataset_version = ?",
                    [version_a],
                )
                stats_b_df = catalog.query(
                    f"SELECT "
                    f"  MIN({col}) as col_min, "
                    f"  MAX({col}) as col_max, "
                    f"  AVG(CAST({col} AS DOUBLE)) as col_mean, "
                    f"  CAST(SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS DOUBLE) "
                    f"    / COUNT(*) as null_rate "
                    f"FROM silver_prices_1d WHERE dataset_version = ?",
                    [version_b],
                )

                sa = stats_a_df.to_dicts()[0] if not stats_a_df.is_empty() else {}
                sb = stats_b_df.to_dicts()[0] if not stats_b_df.is_empty() else {}

                mean_a = float(sa.get("col_mean") or 0)
                mean_b = float(sb.get("col_mean") or 0)
                mean_diff = mean_b - mean_a
                mean_pct = (mean_diff / mean_a * 100) if mean_a != 0 else 0.0

                field_stats.append({
                    "field": col,
                    "version_a": {
                        "min": float(sa.get("col_min") or 0),
                        "max": float(sa.get("col_max") or 0),
                        "mean": mean_a,
                        "null_rate": float(sa.get("null_rate") or 0),
                    },
                    "version_b": {
                        "min": float(sb.get("col_min") or 0),
                        "max": float(sb.get("col_max") or 0),
                        "mean": mean_b,
                        "null_rate": float(sb.get("null_rate") or 0),
                    },
                    "change": {
                        "mean_diff": round(mean_diff, 6),
                        "mean_pct_change": round(mean_pct, 4),
                    },
                })
            except Exception:
                continue

    return {
        "version_a": version_a,
        "version_b": version_b,
        "row_changes": row_changes,
        "field_changes": field_changes,
        "field_stats": field_stats,
    }


@router.get("/{version_id}")
async def get_dataset(version_id: str, catalog: CatalogDep) -> dict:
    """Get a specific dataset version."""
    df = catalog.query(
        "SELECT * FROM silver_dataset_versions WHERE version_id = ?",
        [version_id],
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"Dataset version '{version_id}' not found")
    return df.to_dicts()[0]


@router.get("/quality/{table_name}")
async def get_data_quality(
    table_name: str,
    catalog: CatalogDep,
    start_date: str = "2024-01-01",
    end_date: str = "2025-12-31",
) -> dict:
    """Data quality scoring for a market data table."""
    from cquant.datahub.quality_scorer import DataQualityScorer, QualityQueryError

    # Sanitize table name to prevent SQL injection (real tables only —
    # silver_daily / silver_stock_info / bronze_daily never existed).
    allowed_tables = {"silver_prices_1d", "silver_fundamentals", "silver_assets"}
    if table_name not in allowed_tables:
        raise HTTPException(status_code=400, detail=f"Table '{table_name}' not in allowed list")

    scorer = DataQualityScorer(catalog)
    try:
        report = scorer.score(table_name, start_date, end_date)
    except QualityQueryError as exc:
        # Surface the failure instead of silently returning an all-zero report.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return report.to_dict()


@router.get("/universe/pit")
async def get_point_in_time_universe(
    catalog: CatalogDep,
    as_of_date: str = "2025-06-30",
) -> dict:
    """Get the point-in-time stock universe for a given date."""
    from cquant.datahub.universe import PointInTimeUniverse

    universe = PointInTimeUniverse(catalog)
    stocks = universe.get_universe(as_of_date)
    return {
        "date": as_of_date,
        "count": len(stocks),
        "stocks": [{"asset_id": s.asset_id, "list_date": s.list_date, "delist_date": s.delist_date} for s in stocks[:500]],
    }


@router.get("/universe/stats")
async def get_universe_stats(
    catalog: CatalogDep,
    start_date: str = "2024-01-01",
    end_date: str = "2025-06-30",
) -> dict:
    """Universe statistics: new listings, delistings, survivorship rate."""
    from cquant.datahub.universe import PointInTimeUniverse

    universe = PointInTimeUniverse(catalog)
    return universe.get_universe_stats(start_date, end_date)


@router.post("/bootstrap")
async def bootstrap_assets(catalog: CatalogDep) -> dict:
    """从 silver_prices_1d 中提取资产列表并填充 silver_assets 表。"""
    from cquant.datahub.bootstrap_from_prices import bootstrap_assets_from_prices
    
    try:
        count = bootstrap_assets_from_prices(catalog)
        return {"status": "success", "assets_populated": count}
    except Exception as exc:
        logger.exception("Bootstrap failed")
        raise HTTPException(status_code=500, detail=f"Bootstrap failed: {exc}")


@router.get("/{version_id}/preview")
async def get_dataset_preview(
    version_id: str, catalog: CatalogDep, offset: int = 0, limit: int = 50
) -> dict:
    """获取数据集预览（前 N 行）。"""
    # 获取总数
    count_df = catalog.query("SELECT COUNT(*) as total FROM silver_prices_1d")
    total = count_df.to_dicts()[0]["total"] if not count_df.is_empty() else 0
    
    df = catalog.query(
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount "
        "FROM silver_prices_1d ORDER BY trade_date DESC, asset_id LIMIT ? OFFSET ?",
        [limit, offset],
    )
    if df.is_empty():
        return {"columns": [], "rows": [], "total": 0, "offset": offset, "limit": limit}
    
    columns = df.columns
    rows = df.to_dicts()
    return {"columns": columns, "rows": rows, "total": total, "offset": offset, "limit": limit}


@router.get("/{version_id}/field-stats")
async def get_field_stats(version_id: str, catalog: CatalogDep) -> dict:
    """获取数据集字段统计信息。"""
    fields = []
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        try:
            df = catalog.query(
                f"SELECT COUNT(*) as count, COUNT({col}) as non_null, "
                f"MIN({col}) as min, MAX({col}) as max, AVG({col}) as mean, "
                f"STDDEV({col}) as std FROM silver_prices_1d"
            )
            if not df.is_empty():
                row = df.to_dicts()[0]
                count = row["count"]
                non_null = row["non_null"]
                fields.append({
                    "name": col,
                    "type": "DOUBLE",
                    "count": non_null,
                    "null_count": count - non_null,
                    "null_rate": round((count - non_null) / count, 4) if count > 0 else 0,
                    "unique_count": 0,
                    "min": row["min"],
                    "max": row["max"],
                    "mean": round(row["mean"], 4) if row["mean"] else None,
                    "std": round(row["std"], 4) if row["std"] else None,
                })
        except Exception:
            fields.append({"name": col, "type": "unknown", "count": 0, "null_count": 0, "null_rate": 0, "unique_count": 0, "min": None, "max": None, "mean": None, "std": None})
    return {"fields": fields}


@router.get("/{version_id}/quality-report")
async def get_quality_report(version_id: str, catalog: CatalogDep) -> dict:
    """获取数据质量报告。"""
    try:
        # 获取基本信息
        count_df = catalog.query("SELECT COUNT(*) as total FROM silver_prices_1d")
        total_rows = count_df.to_dicts()[0]["total"] if not count_df.is_empty() else 0
        
        # 获取字段数
        cols_df = catalog.query("SELECT column_name FROM information_schema.columns WHERE table_name = 'silver_prices_1d'")
        total_fields = len(cols_df) if not cols_df.is_empty() else 0
        
        # 简单质量检查
        issues = []
        for col in ["open", "high", "low", "close", "volume"]:
            null_df = catalog.query(f"SELECT COUNT(*) as nulls FROM silver_prices_1d WHERE {col} IS NULL")
            nulls = null_df.to_dicts()[0]["nulls"] if not null_df.is_empty() else 0
            if nulls > 0:
                issues.append({"field": col, "type": "null_values", "count": nulls, "percentage": round(nulls / total_rows * 100, 2)})
        
        score = max(0, 100 - len(issues) * 5)
        return {
            "score": score,
            "total_rows": total_rows,
            "total_fields": total_fields,
            "issues": issues,
            "suggestions": ["Run data quality scorer for detailed analysis"],
        }
    except Exception as exc:
        return {"score": 0, "total_rows": 0, "total_fields": 0, "issues": [], "suggestions": [str(exc)]}


@router.get("/{version_id}/anomalies")
async def get_anomalies(version_id: str, catalog: CatalogDep, limit: int = 20) -> dict:
    """获取数据异常标记（涨跌幅 > 25%）。默认排除 88 开头板块指数。"""
    df = catalog.query(
        "SELECT asset_id, trade_date, close, "
        "LAG(close) OVER (PARTITION BY asset_id ORDER BY trade_date) as prev_close "
        "FROM silver_prices_1d "
        "WHERE asset_id NOT LIKE '%:88%' "
        "ORDER BY trade_date DESC LIMIT 10000"
    )
    if df.is_empty():
        return {"items": []}
    
    anomalies = []
    for row in df.to_dicts():
        if row["prev_close"] and row["close"] and row["prev_close"] > 0:
            change = abs(row["close"] / row["prev_close"] - 1)
            if change > 0.25:
                anomalies.append({
                    "asset_id": row["asset_id"],
                    "trade_date": str(row["trade_date"]),
                    "close": row["close"],
                    "prev_close": row["prev_close"],
                    "change_pct": round(change * 100, 2),
                })
    return {"items": anomalies[:limit]}


# ── External indicators (CSV import, D1-A __MARKET__ sentinel) ───────────────

import json
import os
import tempfile
from pathlib import Path

from fastapi import File, Form, UploadFile

from cquant.datahub.pipelines.external_indicator_importer import (
    ExternalIndicatorImporter,
    ImportConfig,
    preview_csv,
)
from cquant.datahub.pipelines.indicator_catalog import (
    CatalogEntryInput,
    delete_catalog_entry,
    get_catalog_entry,
    list_catalog,
    upsert_catalog_entry,
)
from pydantic import BaseModel

import re as _re


class CatalogPatchBody(BaseModel):
    """PATCH /external-indicators/catalog/{key} 白名单请求体。

    P1 白名单：display_name / unit / description / frequency / enabled /
    available_date_rule / backfill_start。
    P3-4 起 ``source_config``（dict，CustomHTTPConfig 字段）仅对
    ``source_type='custom_http'`` 行开放，提供时走同一套
    model_validate + 防护预检（坏配置 400 带 stage）。
    ``pinned_source`` 仍为保留字段——出现在请求中直接 400（而非静默忽略）。
    """

    display_name: str | None = None
    unit: str | None = None
    description: str | None = None
    frequency: str | None = None
    enabled: bool | None = None
    available_date_rule: str | None = None
    backfill_start: str | None = None
    # P3-4：custom_http 行专用（见上方说明）；csv/builtin 行提供 → 400。
    # 接受 str 以便对非对象 payload 也走行类型检查给 400（而非 pydantic 422）
    source_config: dict | str | None = None
    # 保留字段：仅为了能给出明确的 400 提示，不会被写入
    pinned_source: str | None = None


_IND_KEY_RE = _re.compile(r"^[a-z_0-9]+$")
_FREQ_ALLOWED = {"daily", "weekly", "monthly"}
_RULE_ALLOWED = {"A", "B"}


def _validate_indicator_key(indicator_key: str) -> None:
    if not _IND_KEY_RE.match(indicator_key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"indicator_key 非法：'{indicator_key}'，"
                "仅允许小写字母/数字/下划线 [a-z_0-9]+"
            ),
        )


@router.get("/external-indicators/catalog")
async def list_external_indicator_catalog(catalog: CatalogDep) -> dict:
    """外部指标目录列表（live 新鲜度：latest_trade_date / stale 实时计算）。

    custom_http 行的 source_config 回显一律过 ``redact_config``（脱敏）。"""
    items = [_redact_source_config(item) for item in list_catalog(catalog)]
    return {"items": items, "total": len(items)}


@router.get("/external-indicators/catalog/{indicator_key}")
async def get_external_indicator_catalog_entry(
    catalog: CatalogDep, indicator_key: str
) -> dict:
    """目录详情 + preview（该 key 数据尾部 30 行，升序）。"""
    _validate_indicator_key(indicator_key)
    entry = get_catalog_entry(catalog, indicator_key)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"indicator_key 不存在：'{indicator_key}'"
        )
    return _redact_source_config(entry)


@router.patch("/external-indicators/catalog/{indicator_key}")
async def patch_external_indicator_catalog_entry(
    catalog: CatalogDep, indicator_key: str, body: CatalogPatchBody
) -> dict:
    """部分更新目录行（白名单字段；None 字段保留原值）。

    校验：frequency ∈ {daily, weekly}；available_date_rule ∈ {A, B}；
    indicator_key 沿 [a-z_0-9]+。``pinned_source`` / ``source_config`` 为
    P2/P3 保留字段，请求中出现即 400。
    """
    _validate_indicator_key(indicator_key)
    if body.frequency is not None and body.frequency not in _FREQ_ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"frequency 非法：'{body.frequency}'，允许 {sorted(_FREQ_ALLOWED)}",
        )
    if (
        body.available_date_rule is not None
        and body.available_date_rule not in _RULE_ALLOWED
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"available_date_rule 非法：'{body.available_date_rule}'，"
                f"允许 {sorted(_RULE_ALLOWED)}"
            ),
        )
    if body.pinned_source is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "字段 ['pinned_source'] 为保留字段，不支持通过 PATCH 修改；"
                "请从请求中移除"
            ),
        )
    entry = get_catalog_entry(catalog, indicator_key)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"indicator_key 不存在：'{indicator_key}'"
        )
    source_config_json: str | None = None
    if body.source_config is not None:
        if entry.get("source_type") != "custom_http":
            raise HTTPException(
                status_code=400,
                detail=(
                    "source_config 仅支持 source_type='custom_http' 的行"
                    f"（当前 source_type={entry.get('source_type')!r}）"
                ),
            )
        if not isinstance(body.source_config, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "stage": "config_invalid",
                    "message": "source_config 必须是对象（CustomHTTPConfig 字段）",
                },
            )
        source_config_json = _precheck_custom_http_config(
            body.source_config
        ).model_dump_json()
    upsert_catalog_entry(
        catalog,
        CatalogEntryInput(
            indicator_key=indicator_key,
            display_name=body.display_name,
            unit=body.unit,
            description=body.description,
            frequency=body.frequency,
            enabled=body.enabled,
            available_date_rule=body.available_date_rule,
            backfill_start=body.backfill_start,
            source_config=source_config_json,
        ),
    )
    # 与 create/GET 一致：回显前脱敏 source_config（含 token 等敏感值）
    return _redact_source_config(get_catalog_entry(catalog, indicator_key))


@router.delete("/external-indicators/catalog/{indicator_key}")
async def delete_external_indicator_catalog_entry(
    catalog: CatalogDep, indicator_key: str, purge_data: bool = False
) -> dict:
    """删除目录行。``purge_data=true`` 时连 ``silver_external_indicators``
    中该 key 的全部数据行一并删除（默认 false 保留数据）。"""
    _validate_indicator_key(indicator_key)
    if get_catalog_entry(catalog, indicator_key) is None:
        raise HTTPException(
            status_code=404, detail=f"indicator_key 不存在：'{indicator_key}'"
        )
    delete_catalog_entry(catalog, indicator_key, purge_data=purge_data)
    return {
        "deleted": indicator_key,
        "purged_data": purge_data,
        "detail": (
            "目录行已删除，且数据行已一并清除"
            if purge_data
            else "目录行已删除；数据行保留（如需清除请传 purge_data=true）。"
            "注意：数据行未删，下次服务启动迁移会自动重建该目录行"
        ),
    }


# ── custom_http 创建 + 连接测试（P3-4）──────────────────────────────────────

from datetime import date as _hc_date  # noqa: E402

from pydantic import ValidationError as _ValidationError  # noqa: E402

from cquant.datahub.pipelines.indicator_sources.http_config import (  # noqa: E402
    CustomHTTPConfig,
    redact_config as _redact_config,
)
from cquant.datahub.pipelines.indicator_sources.http_guard import (  # noqa: E402
    GuardError as _GuardError,
    guarded_fetch as _guarded_fetch,
    validate_url as _validate_url,
)


def _render_sample_url(cfg: CustomHTTPConfig, anchor: _hc_date) -> str:
    """渲染 {date} 后的样例 URL（date_param_style='none' 时原样返回）。"""
    if cfg.date_param_style == "none":
        return cfg.url_template
    rendered = (
        anchor.strftime("%Y%m%d")
        if cfg.date_param_style == "yyyymmdd"
        else anchor.isoformat()
    )
    return cfg.url_template.replace("{date}", rendered)


def _precheck_custom_http_config(raw: dict) -> CustomHTTPConfig:
    """保存前防护预检（设计 §13）：model_validate（含 jsonpath 预解析）+
    ``validate_url``（渲染 {date} 后的 URL）。失败统一 400 带 stage 标签。"""
    try:
        cfg = CustomHTTPConfig.model_validate(raw)
    except _ValidationError as exc:
        compact = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise HTTPException(
            status_code=400,
            detail={
                "stage": "config_invalid",
                "message": f"source_config 校验失败：{compact}",
            },
        ) from exc
    try:
        _validate_url(
            _render_sample_url(cfg, _hc_date.today()),
            allow_insecure=cfg.allow_insecure_http,
        )
    except _GuardError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "stage": exc.stage,
                "message": f"保存前防护预检失败：{exc}",
            },
        ) from exc
    return cfg


def _redact_source_config(entry: dict) -> dict:
    """custom_http 行的 source_config 回显脱敏（存原文，脱敏只在回显）。

    非 custom_http 行 / 空 source_config 原样返回（csv 行的 config 无敏感语义，
    解析为对象回显以便前端统一处理）。"""
    raw = entry.get("source_config")
    if not raw:
        return entry
    if entry.get("source_type") == "custom_http":
        try:
            cfg = CustomHTTPConfig.model_validate(
                json.loads(raw) if isinstance(raw, str) else raw
            )
        except Exception:
            entry["source_config"] = {"error": "unparseable_source_config"}
            return entry
        entry["source_config"] = _redact_config(cfg)
    elif isinstance(raw, str):
        try:
            entry["source_config"] = json.loads(raw)
        except ValueError:
            pass
    return entry


class CatalogCreateBody(BaseModel):
    """POST /external-indicators/catalog（custom_http 创建）请求体。

    source_config 为完整 CustomHTTPConfig 字段（dict），保存前过
    ``_precheck_custom_http_config``。``pinned_source`` 为保留字段 → 400。
    """

    indicator_key: str
    display_name: str | None = None
    unit: str | None = None
    description: str | None = None
    frequency: str = "daily"
    available_date_rule: str = "B"
    backfill_start: str | None = None
    source_name: str | None = None
    source_config: dict
    # 保留字段：仅为了能给出明确的 400 提示，不会被写入
    pinned_source: str | None = None


def _anchor_date_or_today(catalog):
    """锚定日：silver_prices_1d max(trade_date)，缺省 today。"""
    a = catalog.query("SELECT max(trade_date) AS a FROM silver_prices_1d").item(0, "a")
    if isinstance(a, str):
        a = _hc_date.fromisoformat(str(a)[:10])
    return a or _hc_date.today()


@router.post("/external-indicators/catalog", status_code=201)
def create_external_indicator_catalog_entry(
    catalog: CatalogDep, body: CatalogCreateBody
) -> dict:
    """创建 custom_http 目录行（保存前防护预检：坏 URL/配置在保存时暴露，
    不等首次刷新）。已存在 key → 409（创建语义，修改走 PATCH）。"""
    _validate_indicator_key(body.indicator_key)
    if body.pinned_source is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "字段 ['pinned_source'] 为保留字段，custom_http 创建不支持；"
                "请从请求中移除"
            ),
        )
    if body.frequency not in _FREQ_ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"frequency 非法：'{body.frequency}'，允许 {sorted(_FREQ_ALLOWED)}",
        )
    if body.available_date_rule not in _RULE_ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"available_date_rule 非法：'{body.available_date_rule}'，"
                f"允许 {sorted(_RULE_ALLOWED)}"
            ),
        )
    if get_catalog_entry(catalog, body.indicator_key) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"indicator_key 已存在：'{body.indicator_key}'"
                "（创建语义；修改请走 PATCH）"
            ),
        )
    cfg = _precheck_custom_http_config(body.source_config)
    upsert_catalog_entry(
        catalog,
        CatalogEntryInput(
            indicator_key=body.indicator_key,
            display_name=body.display_name,
            unit=body.unit,
            description=body.description,
            source_type="custom_http",
            source_name=body.source_name or "custom_http",
            source_config=cfg.model_dump_json(),  # 存原文，脱敏只在回显
            available_date_rule=body.available_date_rule,
            frequency=body.frequency,
            backfill_start=body.backfill_start,
            enabled=True,
        ),
    )
    return _redact_source_config(get_catalog_entry(catalog, body.indicator_key))


class CustomHTTPTestBody(BaseModel):
    """POST /external-indicators/test 请求体（完整 config，不入库）。"""

    source_config: dict


@router.post("/external-indicators/test")
def test_custom_http_source(catalog: CatalogDep, body: CustomHTTPTestBody) -> dict:
    """连接测试（配置调试闭环）：``guarded_fetch`` 单次拉取（date_param_style≠none
    时 dates=[锚定日]，none 时 []），**不写任何库表**。

    成功 → ``{sample: [{trade_date, value}] ≤10 行, diagnostics}``；
    GuardError → 400 带 stage 标签与环节信息（前端文案映射依赖 stage，
    直接读 GuardError 本体属性，不从 catalog last_error 字符串解析）。"""
    cfg = _precheck_custom_http_config(body.source_config)
    anchor = _anchor_date_or_today(catalog)
    dates: list[_hc_date] = (
        [] if cfg.date_param_style == "none" else [anchor]
    )
    try:
        rows = _guarded_fetch(cfg, dates)
    except _GuardError as exc:
        raise HTTPException(
            status_code=400,
            detail={"stage": exc.stage, "message": str(exc)},
        ) from exc
    return {
        "sample": rows[:10],
        "diagnostics": {
            "resolved_url": _render_sample_url(cfg, anchor),
            "status": "ok",
            "rows_parsed": len(rows),
            "field_map_hit": bool(rows),
        },
    }


_ALLOWED_SUFFIXES = {".csv"}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


async def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "upload.csv").suffix.lower() or ".csv"
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 '{suffix}'，仅支持 CSV（.csv）",
        )
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="ext_ind_")
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        tmp.close()
        os.unlink(tmp.name)
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(content) / 1024 / 1024:.1f}MB），上限 50MB",
        )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


@router.post("/external-indicators/preview")
async def preview_external_indicators_csv(file: UploadFile = File(...)) -> dict:
    """上传 CSV 预览：返回列名与前 10 行，供导入向导做列映射。"""
    path = await _save_upload(file)
    try:
        return preview_csv(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"CSV 解析失败：{exc}") from exc
    finally:
        path.unlink(missing_ok=True)


@router.post("/external-indicators/import")
async def import_external_indicators(
    catalog: CatalogDep,
    file: UploadFile = File(...),
    config: str = Form(...),
) -> dict:
    """导入外部指标 CSV 到 silver_external_indicators。

    config JSON: {source, indicator_key, column_map: {csv_col: schema_col},
    available_date_rule: 'A'|'B'}（默认 B=次日可查，保守 PIT）。
    """
    try:
        cfg_dict = json.loads(config)
        cfg = ImportConfig(
            source=str(cfg_dict["source"]),
            indicator_key=str(cfg_dict["indicator_key"]),
            column_map=dict(cfg_dict["column_map"]),
            available_date_rule=str(cfg_dict.get("available_date_rule", "B")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"config JSON 非法（需 source/indicator_key/column_map）：{exc}",
        ) from exc

    path = await _save_upload(file)
    try:
        importer = ExternalIndicatorImporter(catalog)
        report = importer.import_csv(path, cfg)
        return report.as_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导入失败：{exc}") from exc
    finally:
        path.unlink(missing_ok=True)


# ── Builtin indicator registry (P2-1) ────────────────────────────────────────

from datetime import date as _date, timedelta as _timedelta

from cquant.datahub.pipelines.indicator_sources.builtin_registry import (
    BUILTIN_BY_KEY as _BUILTIN_BY_KEY,
    BUILTIN_CATALOG as _BUILTIN_CATALOG,
    source_ready as _source_ready,
)


class BuiltinEnableBody(BaseModel):
    """POST /external-indicators/builtins/{key}/enable 请求体。"""

    backfill_start: str | None = None


def _default_backfill_start(catalog, years: int) -> str:
    """锚定日（silver_prices_1d max(trade_date)，缺省 today）− years 年。"""
    df = catalog.query("SELECT max(trade_date) AS a FROM silver_prices_1d")
    anchor = df.item(0, "a") or _date.today()
    if isinstance(anchor, str):
        anchor = _date.fromisoformat(str(anchor)[:10])
    try:
        return anchor.replace(year=anchor.year - years).isoformat()
    except ValueError:  # 02-29 → 02-28
        return anchor.replace(year=anchor.year - years, day=28).isoformat()


def _run_builtin_backfill(catalog, indicator_key: str):
    """同步触发回填（late-import：refresh 模块 P2-3 交付）。

    模块尚未落地时 ImportError → 返回 pending 标记而非失败；P2-3 合入后
    自然接通，P2-7 e2e 验证真实链路。
    """
    try:
        from cquant.datahub.pipelines.indicator_sources.refresh import (
            run_external_indicator_refresh,
        )
    except ImportError:
        return "pending"
    return run_external_indicator_refresh(
        catalog, keys=[indicator_key], backfill=True, trigger="manual"
    )


@router.get("/external-indicators/builtins")
async def list_builtin_indicators(catalog: CatalogDep) -> dict:
    """内置指标目录（spike 锁定 12 条）：候选源就绪态 + enabled 对齐目录行。"""
    enabled_by_key = {
        r["indicator_key"]: r["enabled"]
        for r in catalog.query(
            "SELECT indicator_key, enabled FROM silver_external_indicator_catalog "
            "WHERE indicator_key IN ("
            + ",".join(f"'{d.indicator_key}'" for d in _BUILTIN_CATALOG)
            + ")"
        ).rows(named=True)
    }
    items = [
        {
            "indicator_key": d.indicator_key,
            "display_name": d.display_name,
            "unit": d.unit,
            "description": d.description,
            "available_date_rule": d.available_date_rule,
            "frequency": d.frequency,
            "default_backfill_years": d.default_backfill_years,
            "candidates": [
                {"name": name, "ready": _source_ready(name)} for name in d.candidates
            ],
            "enabled": bool(enabled_by_key.get(d.indicator_key, False)),
        }
        for d in _BUILTIN_CATALOG
    ]
    return {"items": items, "total": len(items)}


@router.post("/external-indicators/builtins/{indicator_key}/enable")
def enable_builtin_indicator(
    catalog: CatalogDep, indicator_key: str, body: BuiltinEnableBody | None = None
) -> dict:
    """启用内置指标：写目录行（source_type='builtin'）+ 同步触发回填。

    幂等：重复 enable 仅刷新目录行并重触发回填，不报错。回填模块（P2-3）
    未就绪时返回 ``backfill: "pending"``。key 不在注册表 → 404。
    """
    _validate_indicator_key(indicator_key)
    defn = _BUILTIN_BY_KEY.get(indicator_key)
    if defn is None:
        raise HTTPException(
            status_code=404, detail=f"内置指标不存在：'{indicator_key}'"
        )
    payload = body or BuiltinEnableBody()
    backfill_start = payload.backfill_start or _default_backfill_start(
        catalog, defn.default_backfill_years
    )
    upsert_catalog_entry(
        catalog,
        CatalogEntryInput(
            indicator_key=defn.indicator_key,
            display_name=defn.display_name,
            unit=defn.unit,
            description=defn.description,
            source_type="builtin",
            source_name="builtin",
            available_date_rule=defn.available_date_rule,
            frequency=defn.frequency,
            backfill_start=backfill_start,
            enabled=True,
        ),
    )
    backfill = _run_builtin_backfill(catalog, indicator_key)
    entry = get_catalog_entry(catalog, indicator_key)
    return {**entry, "backfill": backfill}


# ── 手动刷新 + 运行历史（P2-4）───────────────────────────────────────────────

from datetime import datetime as _datetime, timezone as _timezone  # noqa: E402

#: refresh_log 中 status='running' 且 started_at 超过该时长的视为陈旧
#: （进程中断未落 finished_at）→ 渲染层标注 interrupted，不改库
_STALE_RUNNING_AFTER = _timedelta(hours=1)


class ExternalIndicatorRefreshBody(BaseModel):
    """POST /external-indicators/refresh 请求体。"""

    keys: list[str] | None = None
    backfill: bool = False


@router.post("/external-indicators/refresh")
def refresh_external_indicators(
    catalog: CatalogDep, body: ExternalIndicatorRefreshBody | None = None
) -> dict:
    """手动触发外部指标刷新（同步执行返回 RefreshSummary，日线量级可接受）。

    - 空 body / keys 缺省 → 全量 due 刷新（enabled 且到期，daily 每日 /
      weekly ≥6 天 / monthly ≥25 天）
    - ``keys`` 指定 → 仅刷这些 key。未知 key **不 404**：逐 key 以
      ``status='error'``（"not in catalog"）进 summary，其余 key 继续
    - ``backfill=True`` → 强制回填窗口（backfill_start..锚定日），否则增量
      （max(trade_date)−4 .. 锚定日，5 日重叠吃近端修订）

    sync ``def``：刷新含网络 IO 与 inter_source_delay（szse 回填 ~4 分钟），
    走 FastAPI 线程池执行，不阻塞 event loop。
    """
    payload = body or ExternalIndicatorRefreshBody()
    for k in payload.keys or []:
        _validate_indicator_key(k)
    from cquant.datahub.pipelines.indicator_sources.refresh import (
        run_external_indicator_refresh,
    )
    summary = run_external_indicator_refresh(
        catalog, keys=payload.keys, backfill=payload.backfill, trigger="manual"
    )
    return summary.as_dict() if hasattr(summary, "as_dict") else summary


@router.get("/external-indicators/runs")
def list_external_indicator_runs(
    catalog: CatalogDep, key: str = "", limit: int = Query(50, ge=1, le=500)
) -> dict:
    """refresh_log 运行历史：started_at 倒序 LIMIT，``key`` 精确过滤。

    ``interrupted`` 为渲染层标注（不改库）：status='running' 且 started_at
    超过 1 小时视为进程中断遗留的陈旧条目。
    """
    sql = (
        "SELECT run_id, indicator_key, source_name, started_at, finished_at, "
        "status, trigger, range_start, range_end, rows_fetched, rows_upserted, "
        "error FROM silver_external_indicator_refresh_log"
    )
    params: list = []
    if key:
        sql += " WHERE indicator_key = ?"
        params.append(key)
    sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
    params.append(limit)

    now = _datetime.now(_timezone.utc)
    items = []
    for r in catalog.query(sql, params).rows(named=True):
        d = dict(r)
        started = d.get("started_at")
        if isinstance(started, str):
            started = _datetime.fromisoformat(started)
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=_timezone.utc)
        d["interrupted"] = bool(
            d.get("status") == "running"
            and started is not None
            and (now - started) > _STALE_RUNNING_AFTER
        )
        d["started_at"] = started.isoformat() if started is not None else None
        fin = d.get("finished_at")
        d["finished_at"] = fin.isoformat() if fin is not None else None
        for f in ("range_start", "range_end"):
            d[f] = str(d[f]) if d.get(f) is not None else None
        items.append(d)
    return {"items": items, "total": len(items)}
