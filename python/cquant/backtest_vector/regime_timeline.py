"""cquant.backtest_vector.regime_timeline — Regime state-interval timeline helpers.

Pure functions that turn the per-day ``regime_scale_history`` produced by
VectorBacktestEngine into:
- merged state intervals (change points on desired scale),
- per-interval stats (dates / duration / portfolio return),
- cycle count (state transitions, threshold-shared with validation suite),
- contribution split (held-period cumulative return vs out-of-market
  avoided/missed benchmark return).

No I/O — the API route feeds persisted series in. Kept importable so unit
tests can exercise the math without FastAPI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Keep in sync with api_server/routes/backtests.py MIN_REGIME_CYCLES.
MIN_REGIME_CYCLES = 6

# Scale thresholds for state labels.
_FULL_SCALE = 0.999
_FLAT_SCALE = 0.001

STATE_FULL = "full"
STATE_REDUCED = "reduced"
STATE_FLAT = "flat"


def state_label(scale: float) -> str:
    """Map a desired position scale to a coarse state label."""
    if scale >= _FULL_SCALE:
        return STATE_FULL
    if scale <= _FLAT_SCALE:
        return STATE_FLAT
    return STATE_REDUCED


@dataclass
class Interval:
    """One contiguous regime state interval."""

    state: str
    scale: float
    start: str
    end: str
    days: int
    interval_return: float | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "scale": self.scale,
            "start": self.start,
            "end": self.end,
            "days": self.days,
            "interval_return": self.interval_return,
        }


def _compound(returns: list[float]) -> float:
    out = 1.0
    for r in returns:
        out *= 1.0 + r
    return out - 1.0


def build_regime_intervals(
    dates: list[str],
    scales: list[float],
    nav_values: list[float] | None = None,
) -> list[Interval]:
    """Merge consecutive days with identical desired scale into intervals.

    Args:
        dates: ISO date strings, ascending.
        scales: desired position scale per day.
        nav_values: optional NAV per day (same length); when given, each
            interval gets its cumulative portfolio return.

    Returns:
        List of intervals keyed on exact scale change points (state label is
        derived, so full(1.0)→reduced(0.5)→full(1.0) is 3 intervals even
        though only 2 distinct labels appear).
    """
    if len(dates) != len(scales) or not dates:
        return []

    def nav_return(i_start: int, i_end_inclusive: int) -> float | None:
        if not nav_values or len(nav_values) < len(dates):
            return None
        # Return over the interval: NAV at the next interval's first day vs
        # NAV at the previous interval's last day (own start NAV when first).
        base = nav_values[i_start - 1] if i_start > 0 else nav_values[0]
        tip_idx = min(i_end_inclusive + 1, len(dates) - 1)
        tip = nav_values[tip_idx]
        return (tip / base - 1.0) if base and base > 0 else None

    intervals: list[Interval] = []
    start_idx = 0
    for i in range(1, len(dates) + 1):
        changed = i == len(dates) or scales[i] != scales[start_idx]
        if not changed:
            continue
        end_idx = i - 1
        intervals.append(Interval(
            state=state_label(float(scales[start_idx])),
            scale=float(scales[start_idx]),
            start=dates[start_idx],
            end=dates[end_idx],
            days=end_idx - start_idx + 1,
            interval_return=nav_return(start_idx, end_idx),
        ))
        start_idx = i
    return intervals


def count_cycles(intervals: list[Interval]) -> int:
    """Count regime cycles as state transitions between adjacent intervals.

    Mirrors the validation-suite definition (transitions of (state, scale)).
    """
    if len(intervals) < 2:
        return 0
    transitions = 0
    prev = (intervals[0].state, round(intervals[0].scale, 6))
    for itv in intervals[1:]:
        key = (itv.state, round(itv.scale, 6))
        if key != prev:
            transitions += 1
        prev = key
    return transitions


def compute_contribution(
    dates: list[str],
    scales: list[float],
    portfolio_returns: list[float],
    benchmark_returns: list[float] | None = None,
) -> dict:
    """Split performance into held-period vs out-of-market contribution.

    Args:
        dates: ISO date strings, ascending.
        scales: desired position scale per day.
        portfolio_returns: daily portfolio return per day.
        benchmark_returns: daily benchmark return per day (same dates);
            ``None`` falls back to a mean-return approximation.

    Returns:
        Dict with:
        - held_return: compounded portfolio return over days with scale>0.
        - out_of_market: {avoided, missed, benchmark_source}
          avoided = compounded benchmark return over flat days where it was
          negative (losses dodged, reported as positive magnitude);
          missed = compounded benchmark return over flat days where it was
          positive (upside forgone).
        - flat_days / held_days counts.
    """
    n = min(len(dates), len(scales), len(portfolio_returns))
    if n == 0:
        return {
            "held_return": 0.0,
            "held_days": 0,
            "flat_days": 0,
            "out_of_market": {
                "avoided": 0.0,
                "missed": 0.0,
                "benchmark_source": "none",
            },
        }

    is_flat = [float(scales[i]) <= _FLAT_SCALE for i in range(n)]

    held_returns = [portfolio_returns[i] for i in range(n) if not is_flat[i]]
    flat_count = sum(is_flat)

    if benchmark_returns is not None and len(benchmark_returns) >= n:
        bm_flat = [benchmark_returns[i] for i in range(n) if is_flat[i]]
        source = "benchmark"
    elif any(not f for f in is_flat) and flat_count > 0:
        # Approximation: out-of-market benchmark ≈ full-period mean daily
        # portfolio return extended over the flat days.
        mean_ret = sum(
            portfolio_returns[i] for i in range(n) if not is_flat[i]
        ) / max(1, n - flat_count)
        bm_flat = [mean_ret] * flat_count
        source = "mean_return_approx"
    else:
        bm_flat = []
        source = "none"

    neg = [r for r in bm_flat if r < 0]
    pos = [r for r in bm_flat if r > 0]
    return {
        "held_return": _compound(held_returns),
        "held_days": n - flat_count,
        "flat_days": flat_count,
        "out_of_market": {
            # Compound only the negative days → the loss magnitude dodged.
            "avoided": -_compound(neg) if neg else 0.0,
            "missed": _compound(pos) if pos else 0.0,
            "benchmark_source": source,
        },
    }


def is_meaningful_scale_history(scales: list[float]) -> bool:
    """A run 'has regime' when scale history exists and is not constant 1.0."""
    if not scales:
        return False
    return any(s is not None and not math.isclose(float(s), 1.0, abs_tol=1e-9) for s in scales)
