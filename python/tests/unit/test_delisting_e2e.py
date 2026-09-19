"""End-to-end delisting verification: a mid-window delisted asset must stop
trading (no fills after its last data date) and be removed from the NAV
(mark-to-zero write-off at its last valid price's disappearance), while
surviving assets keep a normal NAV path.

Engine behaviour under test (see ``run._handle_delisting``):
- Forward-fill does NOT tail-fill delisted assets — rows after the last valid
  ``close`` are dropped, so the price stream simply ends for that asset.
- With no price row after the last data date, ``AShareFillSimulator`` can
  neither buy nor sell the asset: no fill is emitted, and the position drops
  out of ``_calculate_nav`` (price lookup = 0) — an immediate, conservative
  write-off of the delisted position rather than a settlement fill.

Placed under ``unit`` (in-memory engine data, no catalog) per the task note.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import polars as pl

from cquant.backtest_vector.costs import CostModel
from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.run import _forward_fill_long_prices, _handle_delisting
from cquant.backtest_vector.strategy import Strategy, StrategyContext

START = date(2025, 1, 2)
N_DAYS = 30
DELIST_AFTER = 15  # asset B's last data day (index)
END = START + timedelta(days=N_DAYS - 1)
B_LAST = START + timedelta(days=DELIST_AFTER)


class _BuyAndHold(Strategy):
    def __init__(self, asset_ids: list[str]) -> None:
        self._asset_ids = asset_ids

    @property
    def strategy_id(self) -> str:
        return "delisting_e2e_buy_hold"

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        return pl.DataFrame({
            "asset_id": self._asset_ids,
            "signal_date": [ctx.as_of_date] * len(self._asset_ids),
            "direction": ["long"] * len(self._asset_ids),
            "strength": [1.0] * len(self._asset_ids),
            "confidence": [1.0] * len(self._asset_ids),
        })


def _three_asset_prices() -> pl.DataFrame:
    """A: normal throughout; B: delists after day 15; C: normal throughout.

    B's final observation (day ``DELIST_AFTER``) precedes the backtest end
    date, which is the delisting signature recognised by ``_handle_delisting``.
    """
    rows: list[dict] = []
    for i in range(N_DAYS):
        d = START + timedelta(days=i)
        for asset, base in (("A", 10.0), ("B", 20.0), ("C", 30.0)):
            if asset == "B" and i > DELIST_AFTER:
                continue  # B stops reporting — delisted
            p = base * (1 + 0.001 * i)
            rows.append({
                "trade_date": d, "asset_id": asset,
                "open": p, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1_000_000.0, "amount": p * 1_000_000,
                "is_suspended": False,
            })
    return pl.DataFrame(rows)


def _run(prices: pl.DataFrame | None = None):
    """Weekly rebalance (no risk policies → no fired_tiers interference);
    prices pass through the same forward-fill + delisting trim as run.py."""
    raw = prices if prices is not None else _three_asset_prices()
    processed = _handle_delisting(_forward_fill_long_prices(raw), end_date=END)
    engine = VectorBacktestEngine()
    spec = BacktestSpec(
        strategy=_BuyAndHold(["A", "B", "C"]),
        prices=processed,
        start_date=START,
        end_date=END,
        initial_cash=Decimal("1_000_000"),
        cost_model=CostModel.for_cn(),
        rebalance_frequency="1w",
    )
    return engine.run(spec)


class TestDelistingEndToEnd:
    def test_delisted_asset_no_fills_after_last_data_date(self) -> None:
        result = _run()
        b_fills = result.fills.filter(pl.col("asset_id") == "B")
        assert b_fills.height > 0, "B must have traded before delisting"
        late = b_fills.filter(pl.col("trade_date") > B_LAST)
        assert late.height == 0, (
            f"B must have no fills after its last data date {B_LAST}; "
            f"found {late.height}"
        )
        # And the only fill is the initial buy — no settlement/liquidation
        # fill is invented at a synthetic price.
        assert set(b_fills["side"].to_list()) <= {"buy"}

    def test_surviving_assets_nav_normal(self) -> None:
        """NAV stays positive and reaches the backtest end despite B's exit."""
        result = _run()
        assert result.error is None, f"engine error: {result.error}"
        nav = result.portfolio_returns
        assert nav.height >= 4, "weekly NAV snapshots must span the window"
        nav_vals = nav["nav"].to_list()
        assert all(v > 0 for v in nav_vals), "NAV must stay positive throughout"
        last_date = nav["trade_date"].max()
        assert last_date >= START + timedelta(days=N_DAYS - 7), (
            "NAV series must extend to (near) the backtest end date"
        )
        # A and C trade across the whole window
        a_fills = result.fills.filter(pl.col("asset_id") == "A")
        c_fills = result.fills.filter(pl.col("asset_id") == "C")
        assert a_fills.height > 0 and c_fills.height > 0

    def test_delisted_position_written_off_not_frozen(self) -> None:
        """B's position exits the NAV at the first snapshot after delisting.

        The engine's semantics are a conservative mark-to-zero write-off: with
        no price row after B's last data date, the position contributes 0 to
        NAV (no frozen last-price carry, no synthetic settlement fill). The
        write-off is bounded by B's pre-delist market share (~1/3).
        """
        result = _run()
        nav = result.portfolio_returns.sort("trade_date")
        rows = nav.to_dicts()
        pre = [r for r in rows if r["trade_date"] <= B_LAST][-1]
        post = [r for r in rows if r["trade_date"] > B_LAST][0]
        write_off = pre["nav"] - post["nav"]
        assert write_off > 0, (
            "NAV must drop at the first snapshot after delisting (write-off)"
        )
        # B was ~1/3 of the book; the write-off must not exceed ~40%
        assert write_off < 0.40 * pre["nav"], (
            f"write-off {write_off:.0f} exceeds B's ~1/3 book share — "
            "surviving assets would be impaired too"
        )
        # Post-write-off NAV keeps evolving on A/C (not frozen)
        tail = [r["nav"] for r in rows if r["trade_date"] > post["trade_date"]]
        assert any(abs(v - post["nav"]) > 1e-6 for v in tail), (
            "NAV after B's write-off must continue to move with A/C"
        )

    def test_price_matrix_stops_at_delisting(self) -> None:
        """Unit-level guarantee the e2e rests on: no tail-filled prices for B."""
        df = _three_asset_prices()
        filled = _forward_fill_long_prices(df)
        result = _handle_delisting(filled, end_date=END)

        b_dates = result.filter(pl.col("asset_id") == "B")["trade_date"].to_list()
        assert max(b_dates) == B_LAST, (
            f"B price stream must stop at {B_LAST}, got {max(b_dates)}"
        )
        a_dates = result.filter(pl.col("asset_id") == "A")["trade_date"].to_list()
        assert max(a_dates) == END, "A price stream must reach the backtest end"
