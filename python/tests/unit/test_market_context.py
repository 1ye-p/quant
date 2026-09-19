"""MarketSeriesContext tests — spike A wrapper layer (P3S-2 T3).

Covers: PIT filtering (available_date <= as_of), sentinel pseudo-panel
shaping, extra_columns whitelist, pct_change/zscore convenience functions,
missing-day (late arrival) semantics, and multi-indicator panel join.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from cquant.factorlab.dsl_evaluator import DSLError, compile_expression
from cquant.strategy_dsl.market_context import MarketSeriesContext

START = date(2025, 1, 1)
N = 30
# spike synthetic series: vals[i] = 100 + 2i + (i%7==0 ? 3 : 0) + (i%3)*0.5
VALS = [100.0 + 2.0 * i + (3.0 if i % 7 == 0 else 0.0) + (i % 3) * 0.5 for i in range(N)]


class FakeCatalog:
    """Stand-in for datahub Catalog serving silver_external_indicators rows.

    table: indicator_key -> [(trade_date, available_date, value)]
    ``query`` mimics load_external_series' SQL filter (available_date <= as_of).
    """

    def __init__(self, table: dict[str, list[tuple[date, date, float]]]) -> None:
        self.table = table
        self.queries: list[tuple[str, date]] = []

    def query(self, sql: str, params: list) -> pl.DataFrame:
        indicator_key, _asset_id, as_of = params
        self.queries.append((indicator_key, as_of))
        rows = [
            (d, v) for (d, avail, v) in self.table.get(indicator_key, [])
            if avail <= as_of
        ]
        return pl.DataFrame({
            "trade_date": [r[0] for r in rows],
            "value": [r[1] for r in rows],
        })


def _d(i: int) -> date:
    return START + timedelta(days=i)


def _full_table(key: str = "active_cap", lag: int = 0) -> dict:
    """All rows arrived same-day (lag=0) or with a fixed publication lag."""
    return {key: [(_d(i), _d(i + lag), VALS[i]) for i in range(N)]}


class TestSeries:
    def test_series_rename_and_sentinel(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog(_full_table()))
        out = ctx.series("active_cap", _d(10), alias="breadth")
        assert out.columns == ["trade_date", "breadth", "asset_id"]
        assert set(out["asset_id"].unique().to_list()) == {"__MARKET__"}
        assert out.height == 11  # rows 0..10 inclusive
        assert out["breadth"].to_list() == VALS[:11]

    def test_series_pit_cutoff(self) -> None:
        # rows 5..9 arrive 3 days late; at as_of=10 only i<=7 of that block
        # have arrived (i+3<=10) — rows 8/9 are invisible
        table = {"k": [(_d(i), _d(i + 3 if 5 <= i <= 9 else i), 1.0 * i) for i in range(N)]}
        ctx = MarketSeriesContext(FakeCatalog(table))
        got = set(ctx.series("k", _d(10))["trade_date"].to_list())
        assert _d(7) in got and _d(10) in got
        assert _d(8) not in got and _d(9) not in got

    def test_series_empty_indicator(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog({}))
        out = ctx.series("nope", _d(10))
        assert out.is_empty()


class TestEvaluate:
    def test_pct_change_registered_and_matches_hand_calc(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog(_full_table()))
        got = ctx.evaluate("pct_change(active_cap, 1)", _d(6), {"active_cap": "active_cap"})
        assert abs(got - (VALS[6] - VALS[5]) / VALS[5]) < 1e-12

    def test_zscore_equivalent_to_ma_std_composition(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog(_full_table()))
        ind = {"active_cap": "active_cap"}
        a = ctx.evaluate("zscore(active_cap, 20)", _d(25), ind)
        b = ctx.evaluate(
            "(active_cap - ma(active_cap, 20)) / std(active_cap, 20)", _d(25), ind
        )
        assert abs(a - b) < 1e-12

    def test_ma5_window_boundary(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog(_full_table()))
        with pytest.raises(ValueError):  # idx 3 → window not full → null last row
            ctx.evaluate("ma(active_cap, 5)", _d(3), {"active_cap": "active_cap"})
        got = ctx.evaluate("ma(active_cap, 5)", _d(4), {"active_cap": "active_cap"})
        assert abs(got - sum(VALS[0:5]) / 5) < 1e-12

    def test_pit_invisible_to_future_row_changes(self) -> None:
        """Rows after the as_of cutoff cannot influence the result (no leakage)."""
        ind = {"active_cap": "active_cap"}
        table_a = {"active_cap": [(_d(i), _d(i), VALS[i]) for i in range(N)]}
        shifted = [(_d(i), _d(i), VALS[i] if i <= 19 else VALS[i] * 5.0) for i in range(N)]
        table_b = {"active_cap": shifted}
        a = MarketSeriesContext(FakeCatalog(table_a)).evaluate("ma(active_cap, 5)", _d(19), ind)
        b = MarketSeriesContext(FakeCatalog(table_b)).evaluate("ma(active_cap, 5)", _d(19), ind)
        assert abs(a - b) < 1e-12

    def test_missing_day_rolls_over_arrived_rows(self) -> None:
        """迟到行被 PIT 过滤后，滚动窗口在已到位连续行上计算（最近可得观测）。"""
        # indicator arrives with 1-day lag for ALL rows
        ctx = MarketSeriesContext(FakeCatalog(_full_table(lag=1)))
        got = ctx.evaluate("ma(active_cap, 5)", _d(10), {"active_cap": "active_cap"})
        # panel at as_of=10 contains rows 0..9; ma5 last row = mean(vals[5:10])
        assert abs(got - sum(VALS[5:10]) / 5) < 1e-12

    def test_partial_late_rows_nearest_available(self) -> None:
        # rows 8 and 9 are late (arrive day 11); at as_of=9 the last arrived
        # row is 7 → ma2 over rows 6..7 (nearest available observations)
        table = {"k": [(_d(i), _d(11 if i in (8, 9) else i), 10.0 + i) for i in range(N)]}
        ctx = MarketSeriesContext(FakeCatalog(table))
        got = ctx.evaluate("ma(k, 2)", _d(9), {"k": "k"})
        assert abs(got - ((16.0 + 17.0) / 2)) < 1e-12

    def test_unknown_column_still_rejected_without_extra(self) -> None:
        with pytest.raises(DSLError):
            compile_expression("pct_change(active_cap, 1)")

    def test_extra_columns_do_not_pollute_global_whitelist(self) -> None:
        compile_expression("pct_change(x1, 1)", extra_columns={"x1"})
        with pytest.raises(DSLError):
            compile_expression("pct_change(x1, 1)")  # second call: no extras

    def test_evaluate_empty_panel_raises(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog({}))
        with pytest.raises(ValueError, match="empty"):
            ctx.evaluate("ma(active_cap, 5)", _d(10), {"active_cap": "active_cap"})

    def test_boolean_rule_expression(self) -> None:
        ctx = MarketSeriesContext(FakeCatalog(_full_table()))
        ind = {"active_cap": "active_cap"}
        # idx0: VALS[0]=103 (i%7 bonus) → idx1 pct = 102.5/103-1 < 0 → hit;
        # idx2 pct = (105-102.5)/102.5 > 0 → miss
        hit = ctx.evaluate("pct_change(active_cap, 1) < 0", _d(1), ind)
        miss = ctx.evaluate("pct_change(active_cap, 1) < 0", _d(2), ind)
        assert hit == 1.0 and miss == 0.0


class TestMultiIndicatorPanel:
    def test_panel_join_and_evaluate(self) -> None:
        table = {
            "cap": [(_d(i), _d(i), VALS[i]) for i in range(N)],
            "turn": [(_d(i), _d(i), 50.0 + i) for i in range(N)],
        }
        ctx = MarketSeriesContext(FakeCatalog(table))
        out = ctx.evaluate(
            "(cap > 100) + (turn > 50)", _d(20), {"cap": "cap", "turn": "turn"}
        )
        # idx20: cap≈140>100 → 1, turn=70>50 → 1 → Int8 sum = 2
        assert out == 2.0
