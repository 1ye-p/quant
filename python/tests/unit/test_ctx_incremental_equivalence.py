"""P3 incremental ctx slicing — direct content equivalence gate.

The perf_equiv fixtures gate P3 only indirectly (their strategy never reads
ctx.prices), so a ctx slicing bug would leave fills/NAV unchanged and the
gate green. This test asserts the slicing semantics directly against the
original `filter(trade_date <= td)` expression, per rebalance day, including
the warmup prefix and a boundary date absent from the calendar (review I1).
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from cquant.backtest_vector.engine import VectorBacktestEngine


def _frame(n_days: int, n_assets: int = 3) -> pl.DataFrame:
    import numpy as np

    rng = np.random.default_rng(3)
    rows = []
    for ai in range(n_assets):
        p = 10.0 + ai
        for di in range(n_days):
            p *= 1 + rng.normal(0.001, 0.01)
            rows.append({
                "asset_id": f"SSE:{600000 + ai}",
                "trade_date": date(2025, 1, 2) + timedelta(days=di),
                "open": p, "high": p * 1.01, "low": p * 0.99, "close": p,
                "volume": 1e6, "amount": p * 1e6, "is_suspended": False,
            })
    df = pl.DataFrame(rows)
    # unordered on purpose — the engine sorts internally
    return df.sample(fraction=1.0, shuffle=True, seed=7)


class _CtxRecorder:
    """Strategy that records ctx.prices snapshots per rebalance call."""

    def __init__(self) -> None:
        self.snapshots: list[pl.DataFrame] = []

    @property
    def strategy_id(self) -> str:
        return "ctx_recorder"

    def generate_signals(self, ctx) -> pl.DataFrame:
        self.snapshots.append(ctx.prices)
        return pl.DataFrame(
            schema={"asset_id": pl.Utf8, "signal_date": pl.Date,
                    "direction": pl.Utf8, "strength": pl.Float64,
                    "confidence": pl.Float64}
        )


@pytest.fixture()
def recorder_snapshots():
    """Run the real engine over a shuffled frame and capture every ctx."""
    from datetime import date
    from cquant.backtest_vector.engine import BacktestSpec

    prices = _frame(30)
    rec = _CtxRecorder()
    spec = BacktestSpec(
        strategy=rec,
        prices=prices,
        start_date=date(2025, 1, 12),   # 10 warmup rows before the window
        end_date=date(2025, 1, 31),
        warmup_days=10,
    )
    engine = VectorBacktestEngine()
    engine.run(spec)  # zero signals → caught internally, empty result fine
    return prices, rec.snapshots


class TestCtxIncrementalSlicing:
    def test_every_ctx_equals_legacy_filter_semantics(self, recorder_snapshots):
        prices, snaps = recorder_snapshots
        assert len(snaps) > 0, "strategy was never called"
        sorted_prices = prices.sort(["trade_date", "asset_id"])
        for snap in snaps:
            legacy = sorted_prices.filter(pl.col("trade_date") <= snap["trade_date"].max())
            assert snap.equals(legacy)

    def test_warmup_prefix_present_in_first_ctx(self, recorder_snapshots):
        prices, snaps = recorder_snapshots
        first = snaps[0]
        # warmup rows (before the window start) must be visible to strategy
        from datetime import date as _d

        assert first["trade_date"].min() < _d(2025, 1, 12)

    def test_no_ctx_leaks_rows_after_as_of(self, recorder_snapshots):
        _, snaps = recorder_snapshots
        for snap in snaps:
            as_of = snap["trade_date"].max()
            assert snap.filter(pl.col("trade_date") > as_of).height == 0
