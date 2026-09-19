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
