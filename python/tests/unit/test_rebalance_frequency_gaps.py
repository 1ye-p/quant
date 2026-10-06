"""Documented gap: weekly rebalance misses the first trading day after a
full-week holiday (P0' review I2).

`_is_rebalance_date` derives "first trading day of the week" from
`weekday(cur) < weekday(prev)`. Across CN Golden Week the calendar jumps
Tue Sep-30 → Wed Oct-8 (2 < 1 is False), so the first trading day of the
new week is NOT a rebalance day and the week is silently skipped. This is
pre-existing engine semantics that P0' exposed to end users for the first
time; the xfail below pins the gap so the semantic batch can flip it to a
real assertion when a calendar-aware derivation lands. Recorded in
docs/research/limit-rules-divergence.md.
"""
from __future__ import annotations

from datetime import date

import pytest

from cquant.backtest_vector.engine import VectorBacktestEngine


@pytest.mark.xfail(
    reason="weekday-jump derivation skips weeks after full-week holidays; "
    "flip when calendar-aware first-trading-day lands",
    strict=True,
)
def test_weekly_rebalance_first_day_after_golden_week() -> None:
    engine = VectorBacktestEngine()
    # Tue Sep-30 2025 → Wed Oct-8 2025 (Golden Week: Oct 1-7 closed)
    assert engine._is_rebalance_date(
        date(2025, 10, 8), date(2025, 9, 30), "1w"
    )


def test_weekly_rebalance_normal_week_boundary() -> None:
    engine = VectorBacktestEngine()
    # Mon after Fri — the case the derivation handles correctly
    assert engine._is_rebalance_date(date(2025, 10, 13), date(2025, 10, 10), "1w")
    assert not engine._is_rebalance_date(date(2025, 10, 14), date(2025, 10, 13), "1w")
