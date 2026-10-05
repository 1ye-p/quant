"""Tests for FactorAnalysisTool -- verifies the real FactorEvaluator call path.

Regression guard: the tool previously called the non-existent
``FactorEvaluator.ic_timeseries`` and the AttributeError was swallowed at
debug level, so IC analysis silently never appeared in tool output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest

from cquant.ai_advisor.policies import SafetyPolicy
from cquant.ai_advisor.tools import ToolContext
from cquant.ai_advisor.tools.factor_analysis import FactorAnalysisTool


def _make_catalog(factor_df: pl.DataFrame, ret_df: pl.DataFrame) -> MagicMock:
    catalog = MagicMock()

    def query(sql: str, params: list | None = None) -> pl.DataFrame:
        if "fwd_return_1d" in sql:
            return ret_df
        return factor_df

    catalog.query.side_effect = query
    return catalog


def _datasets(n_dates: int = 4, n_assets: int = 5) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Monotonically related factor/return data -> per-date rank IC == 1.0."""
    rows: list[dict] = []
    for d in range(n_dates):
        for a in range(n_assets):
            v = float(a + 1)
            rows.append(
                {
                    "factor_name": "test_factor",
                    "trade_date": f"2026-01-0{d + 5}",
                    "asset_id": f"stock_{a}",
                    "factor_value": v + d * 0.1,
                    "fwd_return_1d": 0.01 * v,
                }
            )
    full = pl.DataFrame(rows)
    factor_df = full.select("factor_name", "trade_date", "asset_id", "factor_value")
    ret_df = full.select("asset_id", "trade_date", "fwd_return_1d")
    return factor_df, ret_df


def _tool_context(catalog: MagicMock) -> ToolContext:
    return ToolContext(
        kb_service=MagicMock(),
        catalog=catalog,
        safety=SafetyPolicy(),
    )


@pytest.mark.asyncio
async def test_factor_analysis_tool_computes_ic_via_evaluator() -> None:
    factor_df, ret_df = _datasets()
    ctx = _tool_context(_make_catalog(factor_df, ret_df))

    result = await FactorAnalysisTool().invoke({"factor_name": "test_factor"}, ctx)

    assert result.success is True
    # IC section present => the real FactorEvaluator.ic_series call succeeded
    assert "## IC Analysis (Rank)" in result.content
    assert "Mean IC: 1.0000" in result.content  # monotone factor/return -> IC = 1
    assert "IC IR:" in result.content
    assert "IC > 0 ratio: 100.00%" in result.content
    # Summary sections still intact
    assert "## Factor: test_factor" in result.content
    assert "## Factor Distribution by Quantile" in result.content


@pytest.mark.asyncio
async def test_factor_analysis_tool_no_returns_reports_unavailable() -> None:
    factor_df, _ = _datasets()
    ctx = _tool_context(_make_catalog(factor_df, pl.DataFrame()))

    result = await FactorAnalysisTool().invoke({"factor_name": "test_factor"}, ctx)

    assert result.success is True
    assert "## Factor: test_factor" in result.content
    assert "IC Analysis" not in result.content


@pytest.mark.asyncio
async def test_factor_analysis_tool_requires_factor_name() -> None:
    ctx = _tool_context(_make_catalog(*_datasets()))
    result = await FactorAnalysisTool().invoke({}, ctx)
    assert result.success is False
    assert "factor_name" in result.content
