"""Regime indicator coverage precheck (P3-6).

Backtest-create-time *warn-level* coverage check for DSL strategies with a
``regime`` section: for every indicator key referenced by
``regime.indicators`` (dict alias → indicator_key), query
``silver_external_indicators`` (index ``idx_ext_ind_date``) and produce
Chinese-language warning strings — never raise, never gate creation.

Anchoring rule (B2, same as :mod:`indicator_catalog`): all date semantics —
"trading day" counts and the range anchor — come from the request's
``start``/``end`` and ``silver_prices_1d`` distinct trade dates.
``CURRENT_DATE`` is never referenced, so a stale prices store cannot produce
false warnings.
"""

from __future__ import annotations

from datetime import date

from cquant.datahub.catalog import Catalog

# 缺口/尾部滞后的 warn 阈值（交易日）
GAP_TOLERANCE_TRADING_DAYS = 5
TAIL_TOLERANCE_TRADING_DAYS = 5


def check_regime_indicator_coverage(
    catalog: Catalog,
    regime_indicators: dict[str, str],
    start: date,
    end: date,
) -> list[str]:
    """Per-indicator-key coverage warnings for a DSL regime section.

    Semantics (all trading days counted as actual distinct
    ``silver_prices_1d`` trade dates, anchored on the request range —
    never the wall clock):

    1. key has zero data rows → ``指标 '{key}' 无任何数据``
    2. ``min(available_date) > start`` → first visible day later than the
       range start; the head of the backtest keeps the initial regime state
    3. usable days in range (``trade_date`` in ``[start, end]`` with
       ``available_date <= end``, distinct) fall short of the expected
       trading-day count (prices distinct in ``[start, end]``) by more than
       ``GAP_TOLERANCE_TRADING_DAYS`` → in-range gap
    4. ``max(trade_date)`` lags ``end`` by more than
       ``TAIL_TOLERANCE_TRADING_DAYS`` trading days (prices distinct in
       ``(max_trade_date, end]``) → tail coverage stops early; regime keeps
       its last state (missing data never switches states)

    Returns ``[]`` when ``regime_indicators`` is empty, when the prices
    store offers no trading-day basis in the range (empty store → no basis
    to judge), or when every key is fully covered. Duplicate keys referenced
    through multiple aliases yield one warning each.
    """
    if not regime_indicators:
        return []

    expected = catalog.query(
        "SELECT COUNT(DISTINCT trade_date) AS n FROM silver_prices_1d "
        "WHERE trade_date >= ? AND trade_date <= ?",
        [start, end],
    ).item(0, "n")
    if not expected:
        # 空行情库 → 无交易日基准，不判（P1 catalog stale 先例）
        return []

    warnings: list[str] = []
    seen: set[str] = set()
    for key in regime_indicators.values():
        if key in seen:
            continue
        seen.add(key)
        warnings.extend(_check_key(catalog, key, start, end, expected))
    return warnings


def _check_key(
    catalog: Catalog, key: str, start: date, end: date, expected: int
) -> list[str]:
    n_rows = catalog.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicators "
        "WHERE indicator_key = ?",
        [key],
    ).item(0, "n")
    if not n_rows:
        return [f"指标 '{key}' 无任何数据"]

    out: list[str] = []

    first_visible = catalog.query(
        "SELECT min(available_date) AS d FROM silver_external_indicators "
        "WHERE indicator_key = ?",
        [key],
    ).item(0, "d")
    if first_visible is not None and first_visible > start:
        out.append(
            f"指标 '{key}' 首个可见日 {first_visible} 晚于区间起点，"
            f"前段 regime 将缺数据保持初始状态"
        )

    usable = catalog.query(
        "SELECT COUNT(DISTINCT trade_date) AS n FROM silver_external_indicators "
        "WHERE indicator_key = ? AND trade_date >= ? AND trade_date <= ? "
        "AND available_date <= ?",
        [key, start, end, end],
    ).item(0, "n")
    gap = expected - usable
    if gap > GAP_TOLERANCE_TRADING_DAYS:
        out.append(f"指标 '{key}' 区间内存在 {gap} 交易日缺口")

    max_td = catalog.query(
        "SELECT max(trade_date) AS d FROM silver_external_indicators "
        "WHERE indicator_key = ?",
        [key],
    ).item(0, "d")
    if max_td is not None and max_td < end:
        tail_behind = catalog.query(
            "SELECT COUNT(DISTINCT trade_date) AS n FROM silver_prices_1d "
            "WHERE trade_date > ? AND trade_date <= ?",
            [max_td, end],
        ).item(0, "n")
        if tail_behind > TAIL_TOLERANCE_TRADING_DAYS:
            out.append(
                f"指标 '{key}' 数据止于 {max_td}，"
                f"区间尾部 regime 将保持状态（缺数据不切换）"
            )

    return out
