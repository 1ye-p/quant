"""_forward_fill_long_prices must preserve every original column.

The reindex used to rebuild the frame from [asset_id, trade_date, close]
only, silently dropping open/high/low/volume/amount/is_suspended — every
OHLCV consumer downstream broke (BreakoutPullback's per-asset evaluation
died with ColumnNotFoundError("open") for the whole universe).
"""
from __future__ import annotations

from datetime import date

import polars as pl

from cquant.backtest_vector.run import _forward_fill_long_prices

_COLS = ["asset_id", "trade_date", "open", "high", "low", "close",
         "volume", "amount", "is_suspended"]


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").str.to_date())


class TestForwardFillColumns:
    def test_all_columns_survive_reindex_with_gaps(self) -> None:
        # Asset A skips 2024-01-03 (other assets have it) — the gap row is
        # reindexed into the grid; close carries forward, open stays NULL on
        # the synthetic row, but the COLUMN must exist with real values on
        # real rows.
        df = _frame([
            {"asset_id": "SSE:600000", "trade_date": "2024-01-02",
             "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
             "volume": 1000.0, "amount": 10500.0, "is_suspended": False},
            {"asset_id": "SSE:600000", "trade_date": "2024-01-04",
             "open": 10.8, "high": 11.2, "low": 10.3, "close": 11.0,
             "volume": 1100.0, "amount": 12100.0, "is_suspended": False},
            {"asset_id": "SSE:600036", "trade_date": "2024-01-03",
             "open": 40.0, "high": 41.0, "low": 39.5, "close": 40.5,
             "volume": 2000.0, "amount": 81000.0, "is_suspended": False},
        ])
        out = _forward_fill_long_prices(df)

        for col in _COLS:
            assert col in out.columns, f"column lost by reindex: {col}"

        # real rows keep their OHLCV values
        a_real = out.filter(
            (pl.col("asset_id") == "SSE:600000")
            & (pl.col("trade_date") == date(2024, 1, 2))
        )
        assert a_real["open"][0] == 10.0

        # gap row: close forward-filled, open left NULL (synthetic row)
        gap = out.filter(
            (pl.col("asset_id") == "SSE:600000")
            & (pl.col("trade_date") == date(2024, 1, 3))
        )
        assert gap.height == 1
        assert gap["close"][0] == 10.5
        assert gap["open"][0] is None

    def test_no_gaps_passthrough_unchanged_values(self) -> None:
        df = _frame([
            {"asset_id": "SSE:600000", "trade_date": "2024-01-02",
             "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
             "volume": 1000.0, "amount": 10500.0, "is_suspended": False},
            {"asset_id": "SSE:600000", "trade_date": "2024-01-03",
             "open": 10.2, "high": 11.1, "low": 10.0, "close": 10.8,
             "volume": 900.0, "amount": 9700.0, "is_suspended": False},
        ])
        out = _forward_fill_long_prices(df)
        assert out.height == 2
        assert sorted(out.columns) == sorted(_COLS)
        assert out["open"].to_list() == [10.0, 10.2]
