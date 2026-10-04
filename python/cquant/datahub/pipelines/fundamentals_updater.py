"""cquant.datahub.pipelines.fundamentals_updater — 基本面数据定时更新。

Usage::

    from cquant.datahub.pipelines.fundamentals_updater import register_fundamentals_job
    from cquant.scheduler import StrategyScheduler

    scheduler = StrategyScheduler()
    register_fundamentals_job(scheduler, catalog)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING

from cquant.datahub.pipelines.announce_backfill import derive_conservative_announce

if TYPE_CHECKING:
    from cquant.datahub.catalog import Catalog
    from cquant.scheduler import StrategyScheduler

logger = logging.getLogger(__name__)


def update_fundamentals(
    catalog: "Catalog",
    source: str = "tushare",
    asset_ids: list[str] | None = None,
) -> int:
    """从指定数据源拉取基本面数据并写入 silver_fundamentals。

    Parameters
    ----------
    catalog
        已初始化的 DuckDB Catalog 实例。
    source
        数据源：'tushare'（默认）或 'akshare'。
    asset_ids
        要更新的资产 ID 列表；传 None 时从 silver_assets 查询所有已有资产。

    Returns
    -------
    int
        成功写入/更新的行数。
    """
    if asset_ids is None:
        asset_ids = _load_asset_ids(catalog)

    if not asset_ids:
        logger.warning("fundamentals_updater: no assets found, skipping update")
        return 0

    today = date.today().isoformat()
    now = datetime.now(tz=timezone.utc).isoformat()

    if source == "tushare":
        rows_written = _update_from_tushare(catalog, asset_ids, today, now)
    elif source == "akshare":
        rows_written = _update_from_akshare(catalog, asset_ids, today, now)
    else:
        raise ValueError(f"Unsupported fundamentals source: {source!r}. Use 'tushare' or 'akshare'.")

    logger.info(
        "fundamentals_updater: updated %d rows from %s on %s",
        rows_written, source, today,
    )
    return rows_written


def _load_asset_ids(catalog: "Catalog") -> list[str]:
    """Load all asset IDs from silver_assets."""
    try:
        df = catalog.query("SELECT DISTINCT asset_id FROM silver_assets LIMIT 5000")
        return df["asset_id"].to_list() if not df.is_empty() else []
    except Exception as exc:
        logger.warning("fundamentals_updater: failed to load asset list: %s", exc)
        return []


def _update_from_tushare(
    catalog: "Catalog",
    asset_ids: list[str],
    report_date: str,
    updated_at: str,
) -> int:
    """Fetch fundamentals from Tushare and upsert into silver_fundamentals."""
    try:
        from cquant.datahub.connectors.tushare_connector import TushareConnector  # noqa: PLC0415
        connector = TushareConnector()
        records = connector.fetch_fundamentals(asset_ids=asset_ids, date=report_date)
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "fundamentals_updater: TushareConnector.fetch_fundamentals unavailable (%s)", exc
        )
        records = []

    if not records:
        return 0

    return _upsert_records(catalog, records, updated_at)


def _update_from_akshare(
    catalog: "Catalog",
    asset_ids: list[str],
    report_date: str,
    updated_at: str,
) -> int:
    """Fetch fundamentals from AKShare and upsert into silver_fundamentals."""
    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError:
        logger.warning("fundamentals_updater: akshare not installed")
        return 0

    records = []
    for asset_id in asset_ids[:100]:  # rate-limit: max 100 per run
        symbol = asset_id.split(":")[-1] if ":" in asset_id else asset_id
        try:
            df = ak.stock_financial_abstract(symbol=symbol)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        row = _parse_akshare_financial(df, report_date)
        if not row:
            continue
        row["asset_id"] = asset_id
        row["report_date"] = report_date
        row["source"] = "akshare"
        # akshare 的 stock_financial_abstract 不返回实际披露日 (ann_date)。
        # D4-A 决议：按法定披露截止日推导 announce_date（保守、无前视）。
        row["announce_date"] = derive_conservative_announce(
            date.fromisoformat(report_date)
        ).isoformat()
        records.append(row)

    if not records:
        return 0

    return _upsert_records(catalog, records, updated_at)


def _parse_akshare_financial(df, report_date: str) -> dict | None:
    """解析 akshare ``stock_financial_abstract`` 返回的财务摘要 DataFrame。

    akshare 该接口（新浪财经源）返回长表，列为 ``item``（中文指标名）和
    ``value``（指标值，字符串，可能含 ``%`` / ``亿`` / ``万`` 单位）。本函数
    将其映射到 silver_fundamentals 列结构。

    Parameters
    ----------
    df
        akshare 返回的 DataFrame，预期包含 ``item``/``value`` 两列。
    report_date
        报告期（由上层传入），写入 ``report_date`` 字段。

    Returns
    -------
    dict | None
        与 silver_fundamentals 列结构一致的 dict；无法提取任何数值时返回 None。
    """
    import pandas as pd  # noqa: PLC0415

    if df is None or getattr(df, "empty", True):
        return None

    # 兼容宽表（字段在列名）与长表（item/value 两列）两种布局
    if "item" in df.columns and "value" in df.columns:
        lookup = dict(zip(df["item"].astype(str), df["value"]))
    else:  # 宽表：直接以列名作为指标名
        lookup = {str(c): df[c].iloc[0] for c in df.columns}

    def _pick(*keywords: str):
        """返回第一个指标名包含任一关键字的原始值。"""
        for name, raw in lookup.items():
            if any(kw in name for kw in keywords):
                return raw
        return None

    def _ratio(*keywords: str) -> float | None:
        """提取百分比类指标（毛利率/净利率/ROE/增长率），统一化为小数。"""
        return _to_float(_pick(*keywords), is_percent=True)

    def _amount(*keywords: str) -> float | None:
        """提取金额类指标（总资产/总负债），按 亿/万 还原为元。"""
        return _to_float(_pick(*keywords))

    row = {
        "report_date": report_date,
        "roe": _ratio("净资产收益率", "ROE", "净资产报酬率"),
        "roa": _ratio("总资产收益率", "总资产报酬率", "ROA", "资产回报率"),
        "gross_margin": _ratio("毛利率"),
        "net_margin": _ratio("净利率", "销售净利率", "净利润率"),
        "revenue_growth_yoy": _ratio("营业收入同比增长", "营业总收入同比增长", "营收同比增长"),
        "earnings_growth_yoy": _ratio("净利润同比增长", "归属母公司净利润同比增长", "净利同比增长"),
        "total_assets": _amount("总资产", "资产总计"),
        "total_debt": _amount("总负债", "负债合计"),
    }

    # 全部为 NULL 时视为解析失败，避免写入空 metadata 行
    if not any(v is not None for k, v in row.items() if k != "report_date"):
        return None

    # 静默消除未使用导入告警（pd 仅用于类型探测，保留以便扩展）
    _ = pd
    return row


def _to_float(value, is_percent: bool = False) -> float | None:
    """将 akshare 字符串指标值安全转换为 float。

    处理 ``%``、``亿``、``万``、``--``、空白与千分位逗号；解析失败返回 None。
    百分比模式 (``is_percent=True``) 下 ``12.3%`` / ``12.3`` → ``0.123``。
    金额模式下 ``1.23亿`` → ``1.23e8``，``4.5万`` → ``45000``。
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "null", "None", "NaN", "nan"}:
        return None

    percent = is_percent
    multiplier = 1.0
    if text.endswith("%"):
        percent = True
        text = text[:-1].strip()
    elif text.endswith("亿"):
        multiplier = 1e8
        text = text[:-1].strip()
    elif text.endswith("万"):
        multiplier = 1e4
        text = text[:-1].strip()

    try:
        num = float(text)
    except (TypeError, ValueError):
        return None

    num *= multiplier
    if percent:
        num /= 100.0
    return num


def _upsert_records(catalog: "Catalog", records: list[dict], updated_at: str) -> int:
    """Upsert records into silver_fundamentals via INSERT OR REPLACE."""
    if not records:
        return 0

    written = 0
    for rec in records:
        try:
            catalog.execute(
                """
                INSERT OR REPLACE INTO silver_fundamentals
                    (asset_id, report_date, pe_ttm, pb, ps_ttm, ev_ebitda, dividend_yield,
                     roe, roa, gross_margin, net_margin, revenue_growth_yoy,
                     earnings_growth_yoy, market_cap, announce_date, total_assets,
                     total_debt, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    rec.get("asset_id"),
                    rec.get("report_date"),
                    rec.get("pe_ttm"),
                    rec.get("pb"),
                    rec.get("ps_ttm"),
                    rec.get("ev_ebitda"),
                    rec.get("dividend_yield"),
                    rec.get("roe"),
                    rec.get("roa"),
                    rec.get("gross_margin"),
                    rec.get("net_margin"),
                    rec.get("revenue_growth_yoy"),
                    rec.get("earnings_growth_yoy"),
                    rec.get("market_cap"),
                    rec.get("announce_date"),
                    rec.get("total_assets"),
                    rec.get("total_debt"),
                    rec.get("source", "unknown"),
                    updated_at,
                ],
            )
            written += 1
        except Exception as exc:
            logger.warning(
                "fundamentals_updater: failed to upsert %s: %s",
                rec.get("asset_id"), exc,
            )

    return written


def register_fundamentals_job(
    scheduler: "StrategyScheduler",
    catalog: "Catalog",
    source: str = "tushare",
    run_hour: int = 18,
    run_minute: int = 0,
) -> None:
    """Register a daily fundamentals update job with the scheduler.

    Parameters
    ----------
    scheduler
        StrategyScheduler instance.
    catalog
        DuckDB Catalog instance.
    source
        Data source ('tushare' or 'akshare').
    run_hour
        Hour (24h) for daily run (default 18 — after market close).
    run_minute
        Minute for daily run (default 0).
    """
    from cquant.scheduler.scheduler import ScheduleConfig, ScheduleFrequency  # noqa: PLC0415

    config = ScheduleConfig(
        job_id="daily_fundamentals_update",
        strategy_id="__system__",
        frequency=ScheduleFrequency.DAILY,
        run_time=time(run_hour, run_minute),
        enabled=True,
        metadata={"source": source, "type": "data_update"},
    )

    def _callback() -> None:
        update_fundamentals(catalog, source=source)

    scheduler.add_job(config, _callback)
    logger.info(
        "fundamentals_updater: registered daily job at %02d:%02d using source '%s'",
        run_hour, run_minute, source,
    )
