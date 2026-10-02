"""P3-6 单测：regime 指标覆盖预检（四类 warn 级 warning + 锚定证明）。

warn 不是 gate —— check_regime_indicator_coverage 只产出文案列表，
绝不抛错；所有日期语义锚定 ``silver_prices_1d`` distinct trade_date
（B2 规则：禁止 CURRENT_DATE，见 test_anchored_..._not_current_date）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from cquant.api_server.routes.indicator_coverage import (
    check_regime_indicator_coverage,
)
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

# 2026-01-05 是周一；连续 20 个工作日（周一~周五）作为"交易日"集合
D0 = date(2026, 1, 5)


def _biz_days(n: int) -> list[date]:
    out, d = [], D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


TDAYS = _biz_days(20)
START, END = TDAYS[0], TDAYS[-1]


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "precheck.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _seed_prices(catalog: Catalog, days: list[date]) -> None:
    catalog.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, source) "
        f"SELECT 'SSE:600036', d, 10, 11, 9, 10, 1000, 'test' "
        f"FROM (VALUES {', '.join(f"(DATE '{d}')" for d in days)}) AS t(d)"
    )


def _seed_indicator(
    catalog: Catalog,
    key: str,
    days: list[date],
    lag: int = 0,
) -> None:
    """available_date = trade_date + lag 个日历日（lag=0 即当日可见）。"""
    catalog.execute(
        "INSERT INTO silver_external_indicators "
        "(source, indicator_key, asset_id, trade_date, value, available_date) "
        f"SELECT 'test_src', '{key}', '__MARKET__', d, 1.0, "
        f"d + INTERVAL ({lag}) DAY "
        f"FROM (VALUES {', '.join(f"(DATE '{d}')" for d in days)}) AS t(d)"
    )


# ── 1. 无任何数据 ────────────────────────────────────────────────────────────


def test_warning_no_data(catalog):
    _seed_prices(catalog, TDAYS)
    warns = check_regime_indicator_coverage(
        catalog, {"north": "ghost_key"}, START, END
    )
    assert warns == ["指标 'ghost_key' 无任何数据"]


# ── 2. 首个可见日晚于区间起点 ────────────────────────────────────────────────


def test_warning_late_first_visible(catalog):
    _seed_prices(catalog, TDAYS)
    _seed_indicator(catalog, "north_net_buy", TDAYS[5:])  # 首可见日 = START+5 交易日
    warns = check_regime_indicator_coverage(
        catalog, {"north": "north_net_buy"}, START, END
    )
    late = [w for w in warns if "首个可见日" in w]
    assert len(late) == 1
    assert str(TDAYS[5]) in late[0]
    assert "north_net_buy" in late[0]


def test_boundary_first_visible_equals_start_no_warning(catalog):
    _seed_prices(catalog, TDAYS)
    _seed_indicator(catalog, "north_net_buy", TDAYS)  # 首可见日 == START
    warns = check_regime_indicator_coverage(
        catalog, {"north": "north_net_buy"}, START, END
    )
    assert not [w for w in warns if "首个可见日" in w]


# ── 3. 区间内交易日缺口 ──────────────────────────────────────────────────────


def test_warning_gap_six_trading_days(catalog):
    _seed_prices(catalog, TDAYS)
    # 覆盖头 6 日 + 尾 8 日，中段缺 [6, 12) 共 6 交易日 > 5
    _seed_indicator(catalog, "margin_balance", TDAYS[:6] + TDAYS[12:])
    warns = check_regime_indicator_coverage(
        catalog, {"m": "margin_balance"}, START, END
    )
    gap = [w for w in warns if "交易日缺口" in w]
    assert len(gap) == 1
    assert "6" in gap[0]
    assert "margin_balance" in gap[0]


def test_boundary_gap_exactly_five_no_warning(catalog):
    _seed_prices(catalog, TDAYS)
    # 中段缺 [7, 12) 恰 5 交易日 —— 不警告
    _seed_indicator(catalog, "margin_balance", TDAYS[:7] + TDAYS[12:])
    warns = check_regime_indicator_coverage(
        catalog, {"m": "margin_balance"}, START, END
    )
    assert not [w for w in warns if "交易日缺口" in w]


# ── 4. 数据止于区间尾部之前 ──────────────────────────────────────────────────


def test_warning_tail_stops_before_end(catalog):
    _seed_prices(catalog, TDAYS)
    _seed_indicator(catalog, "gdp_yoy", TDAYS[:13])  # 止于 END−7 交易日
    warns = check_regime_indicator_coverage(catalog, {"g": "gdp_yoy"}, START, END)
    tail = [w for w in warns if "数据止于" in w]
    assert len(tail) == 1
    assert str(TDAYS[12]) in tail[0]
    assert "gdp_yoy" in tail[0]


# ── 健康路径 ────────────────────────────────────────────────────────────────


def test_healthy_full_coverage_returns_empty(catalog):
    _seed_prices(catalog, TDAYS)
    _seed_indicator(catalog, "north_net_buy", TDAYS)
    _seed_indicator(catalog, "gdp_yoy", TDAYS)
    warns = check_regime_indicator_coverage(
        catalog,
        {"north": "north_net_buy", "g": "gdp_yoy"},
        START,
        END,
    )
    assert warns == []


def test_no_regime_indicators_short_circuits(catalog):
    # 空指标表 + 空 regime.indicators → []（不触达任何查询也安全）
    assert check_regime_indicator_coverage(catalog, {}, START, END) == []


def test_empty_prices_store_no_basis_returns_empty(catalog):
    # 行情库空 → 无交易日基准，不判（与 catalog stale 先例一致）
    _seed_indicator(catalog, "north_net_buy", TDAYS)
    assert (
        check_regime_indicator_coverage(
            catalog, {"north": "north_net_buy"}, START, END
        )
        == []
    )


# ── 锚定证明：禁止 CURRENT_DATE ─────────────────────────────────────────────


def test_anchored_to_request_range_not_current_date(catalog):
    """B2 回归：end 取自请求而非 wall clock。

    行情库 stale 30 天（anchor = today−30 的 10 个交易日）；指标完整覆盖
    该 10 日；请求 end = today。正确实现锚定请求区间内的行情交易日 →
    尾部滞后 0 交易日、无缺口；任何引用当前日期的实现都会把 ~30 天
    日历滞后计为尾部缺口而误报。
    """
    tdays = _biz_days(10)
    anchor = date.today() - timedelta(days=30)
    shifted = [anchor + (d - tdays[0]) for d in tdays]
    _seed_prices(catalog, shifted)
    _seed_indicator(catalog, "north_net_buy", shifted)
    warns = check_regime_indicator_coverage(
        catalog,
        {"north": "north_net_buy"},
        shifted[0],
        date.today(),  # 请求区间远超行情锚定日
    )
    assert warns == []
