"""Warmup semantics for walk-forward fold backtests.

WF fold specs restrict the backtest window to the fold's test period, but
strategies like BreakoutPullback require N trading days of history before
signalling (min_list_days + 60 = 180). Without a warmup prefix the fold's
price window (~50 rows) can never satisfy the gate and every fold comes back
empty — zero signals, zero fills, zero portfolio snapshots (run 0bef4152).

Contract under test:
- BacktestSpec.warmup_days: engine feeds the strategy pre-window history
  (ctx.prices) while the rebalance calendar, fills, and stats still start
  at spec.start_date — the warmup period never trades.
- The warmup amount is inferred from Strategy.required_history_days when
  the run spec doesn't set one explicitly.
- BreakoutPullback declares required_history_days = min_list_days + 60.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from cquant.backtest_vector.run import BacktestRunner
from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Continuous calendar days = trading days in this synthetic fixture, so the
# history gate maps 1:1 onto row counts.
N_DAYS = 240
BASE_DATE = date(2025, 1, 1)
DATES = [BASE_DATE + timedelta(days=i) for i in range(N_DAYS)]


class _HistoryGateStrategy(Strategy):
    """Signals long only once it can see >= required_history_days rows."""

    required_history_days = 180

    @property
    def strategy_id(self) -> str:
        return "history_gate_test"

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        hist = ctx.prices.filter(
            (pl.col("asset_id") == "SSE:600036")
            & (pl.col("trade_date") <= ctx.as_of_date)
        )
        if hist.height >= self.required_history_days:
            return pl.DataFrame({
                "asset_id": ["SSE:600036"],
                "signal_date": [ctx.as_of_date],
                "direction": ["long"],
                "strength": [1.0],
                "confidence": [1.0],
            })
        return pl.DataFrame(
            schema={"asset_id": pl.Utf8, "signal_date": pl.Date,
                    "direction": pl.Utf8, "strength": pl.Float64,
                    "confidence": pl.Float64}
        )


def _seed_prices(cat: Catalog) -> None:
    rng = np.random.default_rng(7)
    rows = []
    p = 50.0
    for d in DATES:
        p *= 1 + rng.normal(0.001, 0.01)
        rows.append({
            "asset_id": "SSE:600036", "trade_date": d,
            "open": p, "high": p * 1.01, "low": p * 0.99,
            "close": p, "volume": 1e6, "amount": p * 1e6,
            "adj_factor": 1.0, "adj_close": p, "is_suspended": False,
            "source": "test",
        })
    df = pl.DataFrame(rows)
    conn = cat._get_conn()
    conn.register("_s", df.to_arrow())
    conn.execute("""
        INSERT OR REPLACE INTO silver_prices_1d
            (asset_id, trade_date, open, high, low, close, volume, amount,
             adj_factor, adj_close, is_suspended, source)
        SELECT asset_id, trade_date, open, high, low, close, volume, amount,
               adj_factor, adj_close, is_suspended, source
        FROM _s
    """)
    conn.unregister("_s")


@pytest.fixture()
def catalog_with_history(tmp_path):
    cat = Catalog(db_path=tmp_path / "warmup_test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    _seed_prices(cat)
    return cat


def _fills_for(catalog: Catalog, run_id: str) -> pl.DataFrame:
    return catalog.query(
        "SELECT trade_date, asset_id, side FROM gold_fills WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )


class TestEngineWarmup:
    def test_history_gate_signals_with_warmup(self, catalog_with_history) -> None:
        """Window [d180, d200]: with 180d warmup the gate opens on day one."""
        runner = BacktestRunner(catalog_with_history)
        run_id = runner.run_engine(
            strategy=_HistoryGateStrategy(),
            start_date=DATES[180],
            end_date=DATES[200],
        )
        fills = _fills_for(catalog_with_history, run_id)
        assert fills.height > 0, "expected fills once warmup history is visible"

    def test_warmup_period_never_trades(self, catalog_with_history) -> None:
        """Fills must land inside [start_date, end_date] — warmup days are
        context-only, not tradable."""
        runner = BacktestRunner(catalog_with_history)
        start, end = DATES[180], DATES[200]
        run_id = runner.run_engine(
            strategy=_HistoryGateStrategy(),
            start_date=start,
            end_date=end,
        )
        fills = _fills_for(catalog_with_history, run_id)
        assert fills.height > 0
        assert fills["trade_date"].min() >= start
        assert fills["trade_date"].max() <= end

    def test_insufficient_warmup_history_completes_empty(self, catalog_with_history) -> None:
        """Window starting at d100: only 100 prior rows exist (<180), so the
        gate never opens. Engine.run catches the no-signals error and returns
        an empty-metrics result — the run completes with 0 fills (this is the
        0bef4152 signature: completed + all-zero metrics). Warmup must not
        fabricate history to force trades."""
        runner = BacktestRunner(catalog_with_history)
        run_id = runner.run_engine(
            strategy=_HistoryGateStrategy(),
            start_date=DATES[100],
            end_date=DATES[130],
        )
        fills = _fills_for(catalog_with_history, run_id)
        assert fills.height == 0

    def test_zero_requirement_strategy_unchanged(self, catalog_with_history) -> None:
        """required_history_days=0 strategies must behave exactly as before
        (no warmup fetch, signals from window data only)."""

        class _DayOneBuy(Strategy):
            required_history_days = 0

            @property
            def strategy_id(self) -> str:
                return "day_one_buy"

            def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
                return pl.DataFrame({
                    "asset_id": ["SSE:600036"],
                    "signal_date": [ctx.as_of_date],
                    "direction": ["long"],
                    "strength": [1.0],
                    "confidence": [1.0],
                })

        runner = BacktestRunner(catalog_with_history)
        run_id = runner.run_engine(
            strategy=_DayOneBuy(),
            start_date=DATES[10],
            end_date=DATES[30],
        )
        fills = _fills_for(catalog_with_history, run_id)
        assert fills.height > 0


class TestWalkForwardRefitWarmup:
    def test_fold_specs_carry_strategy_warmup(self) -> None:
        """WalkForwardRefit must pass required_history_days into both the
        train and test fold specs it hands to the engine."""
        from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
        from cquant.bt_analyzer.walk_forward_refit import WalkForwardRefit

        seen: list[BacktestSpec] = []

        class _RecordingEngine:
            def run(self, spec: BacktestSpec):
                seen.append(spec)
                # minimal result duck-type: refit only reads it via _extract_metrics
                class _R:
                    metrics = {"total_return": 0.0}
                    portfolio_returns = pl.DataFrame(
                        {"trade_date": [spec.start_date], "portfolio_return": [0.0]}
                    )
                return _R()

        n = 240
        prices = pl.DataFrame({
            "asset_id": ["SSE:600036"] * n,
            "trade_date": DATES[:n],
            "open": [50.0] * n, "high": [51.0] * n, "low": [49.0] * n,
            "close": [50.0] * n, "volume": [1e6] * n, "amount": [5e7] * n,
            "is_suspended": [False] * n,
        })

        base = BacktestSpec(
            strategy=_HistoryGateStrategy(),
            prices=prices,
            start_date=DATES[0],
            end_date=DATES[n - 1],
        )
        refit = WalkForwardRefit(
            base_spec=base,
            n_folds=2,
            train_ratio=0.7,
            gap_days=1,
            engine=_RecordingEngine(),
        )
        refit.run()
        assert seen, "recording engine saw no specs"
        assert all(s.warmup_days == 180 for s in seen), [
            s.warmup_days for s in seen
        ]


class TestStrategyDeclarations:
    def test_breakout_pullback_declares_history_need(self) -> None:
        from cquant.backtest_vector.strategies.breakout_pullback import (
            BreakoutPullbackConfig,
            BreakoutPullbackStrategy,
        )

        strat = BreakoutPullbackStrategy(
            "bp",
            BreakoutPullbackConfig(min_list_days=120),
        )
        assert strat.required_history_days == 120 + 60

        custom = BreakoutPullbackStrategy(
            "bp2",
            BreakoutPullbackConfig(min_list_days=60),
        )
        assert custom.required_history_days == 60 + 60
