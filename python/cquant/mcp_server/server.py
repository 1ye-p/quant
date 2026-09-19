"""cquant.mcp_server.server — FastMCP server with three cQuant DuckDB tools.

Run via:  python -m cquant.mcp_server
Or:       fastmcp run python/cquant/mcp_server/server.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastmcp import FastMCP

from cquant.datahub.catalog import Catalog

mcp = FastMCP("cQuant Data Tools")

# Database path: prefer env var, fall back to default location
_DB_PATH: str = os.environ.get(
    "CQUANT_DB",
    str(Path.home() / ".cquant" / "data" / "cquant.duckdb"),
)


def _get_catalog() -> Catalog:
    """Return a Catalog instance for read-only queries."""
    return Catalog(_DB_PATH, read_only=True)


@mcp.tool()
def query_backtest_result(run_id: str) -> str:
    """Return summary statistics for a completed backtest run.

    Args:
        run_id: UUID of the backtest run (from gold_backtest_runs table).
    """
    try:
        catalog = _get_catalog()
        try:
            df = catalog.query(
                "SELECT run_id, engine, strategy_id, dataset_version, "
                "started_at, completed_at, status, metrics_uri, error_message "
                "FROM gold_backtest_runs WHERE run_id = ? LIMIT 1",
                [run_id],
            )
        finally:
            catalog.close()
    except Exception as exc:
        return json.dumps({"error": f"Database error: {exc}"})

    if df.is_empty():
        return json.dumps({"error": f"Backtest run '{run_id}' not found."})

    row = df.row(0, named=True)
    return json.dumps(row, default=str, ensure_ascii=False)


@mcp.tool()
def query_factor_ic(factor_name: str, feature_set_version: str = "") -> str:
    """Return IC analysis summary for a factor.

    Args:
        factor_name: Name of the factor (e.g., 'momentum_20d').
        feature_set_version: Optional feature set version filter.
    """
    try:
        catalog = _get_catalog()
        try:
            if feature_set_version:
                df = catalog.query(
                    "SELECT job_id, factor_name, feature_set_version, status, "
                    "summary_json, created_at "
                    "FROM meta_factor_analytics "
                    "WHERE factor_name = ? AND feature_set_version = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    [factor_name, feature_set_version],
                )
            else:
                df = catalog.query(
                    "SELECT job_id, factor_name, feature_set_version, status, "
                    "summary_json, created_at "
                    "FROM meta_factor_analytics "
                    "WHERE factor_name = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    [factor_name],
                )
        finally:
            catalog.close()
    except Exception as exc:
        return json.dumps({"error": f"Database error: {exc}"})

    if df.is_empty():
        return json.dumps({"error": f"No IC analysis found for factor '{factor_name}'."})

    row = df.row(0, named=True)
    if row.get("summary_json"):
        try:
            row["summary_json"] = json.loads(row["summary_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return json.dumps(row, default=str, ensure_ascii=False)


@mcp.tool()
def query_risk_snapshot(run_id: str = "", strategy_id: str = "") -> str:
    """Return the latest risk snapshot for a run or strategy.

    Args:
        run_id: UUID of the backtest run. Takes priority over strategy_id.
        strategy_id: Strategy ID to look up if run_id is not provided.
    """
    if not run_id and not strategy_id:
        return json.dumps({"error": "Provide either run_id or strategy_id."})

    try:
        catalog = _get_catalog()
        try:
            if run_id:
                df = catalog.query(
                    "SELECT run_id, snapshot_ts, strategy_id, gross_leverage, net_leverage, "
                    "beta, drawdown, var_95, cvar_95 "
                    "FROM gold_risk_snapshots WHERE run_id = ? "
                    "ORDER BY snapshot_ts DESC LIMIT 1",
                    [run_id],
                )
            else:
                df = catalog.query(
                    "SELECT run_id, snapshot_ts, strategy_id, gross_leverage, net_leverage, "
                    "beta, drawdown, var_95, cvar_95 "
                    "FROM gold_risk_snapshots WHERE strategy_id = ? "
                    "ORDER BY snapshot_ts DESC LIMIT 1",
                    [strategy_id],
                )
        finally:
            catalog.close()
    except Exception as exc:
        return json.dumps({"error": f"Database error: {exc}"})

    if df.is_empty():
        ref = run_id or strategy_id
        return json.dumps({"error": f"No risk snapshot found for '{ref}'."})

    row = df.row(0, named=True)
    return json.dumps(row, default=str, ensure_ascii=False)


from cquant.mcp_server.tools.market_data import get_stock_history as _get_stock_history


@mcp.tool()
def get_stock_history(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
    adjust: str = "hfq",
) -> str:
    """获取 A 股历史 OHLCV 行情（来源：AKShare）。

    Args:
        symbol: A 股 6 位代码（如 '600036'）。
        start_date: 开始日期 YYYYMMDD。
        end_date: 结束日期 YYYYMMDD。
        period: 'daily' / 'weekly' / 'monthly'，默认 daily。
        adjust: 'hfq'（后复权）/ 'qfq'（前复权）/ ''（不复权），默认 hfq。
    """
    return _get_stock_history(symbol, start_date, end_date, period, adjust)
