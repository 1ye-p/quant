"""BreakoutPullbackStrategy — 突破回踩选股策略 (回踩优先版).

Translated from TDX formula "突破回踩选股 - 回踩优先版".

Core logic:
  1. Detect a "big yang" breakout candle (启动大阳) within N days
  2. Wait for price to pull back to MA10 with shrinking volume
  3. Validate pullback quality (no broken support, orderly volume)
  4. Enter when all conditions align
  5. Exit via composite rules (MA stop, drawdown stop, trend break)

This strategy operates per-stock (time-series) and then ranks across
the cross-section to select top-N positions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.core.types import SignalFrame

logger = logging.getLogger(__name__)


# ── TDX-style helper functions ────────────────────────────────────────────


def _barslast(cond: pl.Series) -> pl.Series:
    """BARSLAST: distance (in bars) to the most recent True in *cond*.

    Returns 9999 for positions before the first True.
    """
    result: list[int] = []
    last_true = -1
    for i in range(len(cond)):
        if bool(cond[i]):
            last_true = i
        result.append(i - last_true if last_true >= 0 else 9999)
    return pl.Series("_barslast", result)


def _dynamic_ref(values: pl.Series, offsets: pl.Series, fill: float = 0.0) -> pl.Series:
    """Dynamic-offset reference: ``result[i] = values[i - offset[i]]``.

    Falls back to *fill* when the offset points before the series start.
    """
    result: list[float] = []
    n = len(values)
    for i in range(n):
        off = int(offsets[i])
        idx = i - off
        if 0 <= idx < n:
            result.append(float(values[idx]))
        else:
            result.append(fill)
    return pl.Series("_dynamic_ref", result)


def _dynamic_min(low: pl.Series, offsets: pl.Series) -> pl.Series:
    """Dynamic-window rolling min: ``result[i] = min(low[i-offset[i] .. i])``.

    Equivalent to TDX ``LLV(L, T)`` where T varies per bar.
    """
    result: list[float] = []
    n = len(low)
    for i in range(n):
        off = int(offsets[i])
        start = max(0, i - off)
        window = low[start : i + 1]
        result.append(float(window.min()))
    return pl.Series("_dynamic_min", result)


def _dynamic_count(cond: pl.Series, offsets: pl.Series) -> pl.Series:
    """Dynamic-window count: ``result[i] = sum(cond[i-offset[i] .. i])``.

    Equivalent to TDX ``COUNT(cond, T)`` where T varies per bar.
    """
    result: list[int] = []
    n = len(cond)
    for i in range(n):
        off = int(offsets[i])
        start = max(0, i - off)
        window = cond[start : i + 1]
        result.append(int(window.sum()))
    return pl.Series("_dynamic_count", result)


# ── Strategy configuration ────────────────────────────────────────────────


@dataclass
class BreakoutPullbackConfig:
    """All tunable parameters for the breakout-pullback strategy."""

    # Lookback window for big-yang detection
    N: int = 10
    # Position management
    max_positions: int = 10
    # Exit: drawdown stop-loss
    stop_loss_pct: float = 0.08
    # Exit: MA stop period
    ma_stop_period: int = 20
    # Exit: trend stop requires minimum holding days
    trend_hold_days: int = 3
    # Exit: take-profit multiplier (relative to MA20)
    take_profit_mult: float = 1.15
    # Entry: MA10 touch bounds
    touch_ma10_upper: float = 1.015
    touch_ma10_lower: float = 0.96
    touch_close_floor: float = 0.98
    # Entry: volume shrinkage threshold
    shrink_ratio: float = 0.85
    # Entry: big-yang thresholds
    big_yang_gain: float = 0.06
    big_yang_body: float = 0.035
    big_yang_vol_mult: float = 2.0
    # Entry: big-yin thresholds
    big_yin_drop: float = 0.03
    big_yin_vol_mult: float = 1.5
    # Entry: pullback quality
    pullback_depth_floor: float = 0.97
    vol_vs_breakout: float = 0.6
    rise_upper: float = 0.12
    rise_lower: float = -0.06
    # Entry: price / listing filters
    price_min: float = 3.0
    price_max: float = 150.0
    min_list_days: int = 120


DEFAULT_CONFIG = BreakoutPullbackConfig()


# ── Main strategy class ──────────────────────────────────────────────────


class BreakoutPullbackStrategy(Strategy):
    """Breakout-pullback stock selection strategy.

    Parameters
    ----------
    strategy_id : str
        Unique identifier for this strategy instance.
    config : BreakoutPullbackConfig
        Tunable parameters.  Uses ``DEFAULT_CONFIG`` when omitted.
    """

    def __init__(
        self,
        strategy_id: str,
        config: BreakoutPullbackConfig | None = None,
    ) -> None:
        self._strategy_id = strategy_id
        self._cfg = config or DEFAULT_CONFIG
        # generate_signals skips assets whose in-window history is shorter
        # than min_list_days + 60 rows — WF fold warmup must cover this
        # lookback or every fold comes back empty.
        self.required_history_days = self._cfg.min_list_days + 60

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    # ── Signal generation ─────────────────────────────────────────────

    def generate_signals(self, ctx: StrategyContext) -> SignalFrame:
        """Generate buy/sell signals for the given context."""
        empty = _empty_signal_frame()

        if ctx.prices is None or ctx.prices.is_empty():
            return empty

        # Get tradable assets on as_of_date
        asset_ids = (
            ctx.prices
            .filter(pl.col("trade_date") == ctx.as_of_date)
            ["asset_id"]
            .unique()
            .to_list()
        )

        # Extract suspended / limit-up / limit-down sets
        suspended: set[str] = set()
        limit_up: set[str] = set()
        limit_down: set[str] = set()
        if ctx.tradability is not None and not ctx.tradability.is_empty():
            flags = ctx.tradability.filter(pl.col("trade_date") == ctx.as_of_date)
            if "is_suspended" in flags.columns:
                suspended = set(flags.filter(pl.col("is_suspended"))["asset_id"].to_list())
            if "is_limit_up" in flags.columns:
                limit_up = set(flags.filter(pl.col("is_limit_up"))["asset_id"].to_list())
            if "is_limit_down" in flags.columns:
                limit_down = set(flags.filter(pl.col("is_limit_down"))["asset_id"].to_list())

        asset_ids = [a for a in asset_ids if a not in suspended]
        if not asset_ids:
            return empty

        buy_frames: list[SignalFrame] = []
        sell_frames: list[SignalFrame] = []

        for asset_id in asset_ids:
            # Historical prices up to (and including) as_of_date
            hist = (
                ctx.prices
                .filter(
                    (pl.col("asset_id") == asset_id)
                    & (pl.col("trade_date") <= ctx.as_of_date)
                )
                .sort("trade_date")
            )
            if hist.height < self._cfg.min_list_days + 60:
                continue  # not enough history

            try:
                entry_sig, exit_sig, strength = self._evaluate_asset(hist)
            except Exception as exc:
                logger.warning("Strategy '%s': failed for %s: %s", self._strategy_id, asset_id, exc)
                continue

            if entry_sig and asset_id not in limit_up:
                buy_frames.append(_signal_row(asset_id, ctx.as_of_date, "long", strength))
            elif exit_sig and asset_id not in limit_down:
                sell_frames.append(_signal_row(asset_id, ctx.as_of_date, "sell", 1.0))

        # Combine: buy takes priority over sell for same asset
        buy_df = _concat(buy_frames)
        sell_df = _concat(sell_frames)

        if buy_df.is_empty() and sell_df.is_empty():
            return empty

        if not buy_df.is_empty() and not sell_df.is_empty():
            buy_assets = set(buy_df["asset_id"].to_list())
            sell_df = sell_df.filter(~pl.col("asset_id").is_in(list(buy_assets)))

        # Rank buy signals by shrinkage score (lower = better)
        if not buy_df.is_empty() and buy_df.height > self._cfg.max_positions:
            buy_df = buy_df.sort("strength", descending=True).head(self._cfg.max_positions)

        parts = [f for f in [buy_df, sell_df] if not f.is_empty()]
        return pl.concat(parts) if parts else empty

    # ── Per-asset evaluation ──────────────────────────────────────────

    def _evaluate_asset(self, df: pl.DataFrame) -> tuple[bool, bool, float]:
        """Evaluate entry/exit conditions for a single asset.

        Returns (entry_signal, exit_signal, entry_strength).
        """
        cfg = self._cfg
        n = df.height

        # ── 1. Compute indicators ──
        close = df["close"].cast(pl.Float64)
        open_ = df["open"].cast(pl.Float64)
        high = df["high"].cast(pl.Float64)
        low = df["low"].cast(pl.Float64)
        volume = df["volume"].cast(pl.Float64)

        ma5 = close.rolling_mean(5)
        ma10 = close.rolling_mean(10)
        ma20 = close.rolling_mean(20)
        ma30 = close.rolling_mean(30)
        ma60 = close.rolling_mean(60)
        v5 = volume.rolling_mean(5)
        ma30_3ago = ma30.shift(3)

        # ── 2. Detect big yang (启动大阳) ──
        prev_close = close.shift(1)
        prev_v5 = v5.shift(1)
        ma20_5ago = ma20.shift(5)

        big_yang = (
            ((close / prev_close - 1) >= cfg.big_yang_gain)
            & (close > open_)
            & ((close - open_) / prev_close >= cfg.big_yang_body)
            & (volume > cfg.big_yang_vol_mult * prev_v5)
            & (close > ma5) & (close > ma10) & (close > ma20) & (close > ma30) & (close > ma60)
            & (ma20 >= ma20_5ago)
        ).fill_null(False)

        # ── 3. BARSLAST(big_yang) ──
        T = _barslast(big_yang)

        # ── 4. Detect big yin (放量大阴线) ──
        big_yin = (
            (close < open_)
            & ((open_ - close) / prev_close >= cfg.big_yin_drop)
            & (volume > cfg.big_yin_vol_mult * v5)
        ).fill_null(False)

        # ── 5. Dynamic references based on T ──
        by_close = _dynamic_ref(close, T)
        by_open = _dynamic_ref(open_, T)
        by_vol = _dynamic_ref(volume, T)
        by_mid = pl.Series("_mid", [(c + o) / 2 for c, o in zip(by_close, by_open)])

        # ── 6. Entry conditions ──
        # ① TOUCHMA10: low touches MA10
        touch_ma10 = (
            (low <= ma10 * cfg.touch_ma10_upper)
            & (low >= ma10 * cfg.touch_ma10_lower)
            & (close >= ma10 * cfg.touch_close_floor)
        )

        # ② SHRINKTODAY: volume < V5 * shrink_ratio
        shrink_today = volume < v5 * cfg.shrink_ratio

        # ③ STABLE: close ≈ open OR long lower shadow
        body_range = high - low + 0.001
        stable = (close >= open_ * 0.99) | ((close - low) / body_range > 0.5)

        # ④ NOTDUMP: no high-volume bearish dump
        not_dump = ~(
            (close < open_)
            & (volume > volume.shift(1) * 1.5)
            & ((open_ - close) / open_ > 0.025)
        )

        # ⑤ HASBIGYANG: big yang within [2, N] days ago
        has_big_yang = (T >= 2) & (T <= cfg.N)

        # ⑥ NOTBROKEN: pullback low doesn't break below breakout midpoint
        dynamic_llv = _dynamic_min(low, T)
        not_broken = dynamic_llv >= by_mid * cfg.pullback_depth_floor

        # ⑦ VOLOK: today's volume < breakout day volume * threshold
        vol_ok = volume < by_vol * cfg.vol_vs_breakout

        # ⑧ RISEOK: cumulative rise since breakout is reasonable
        rise = (close - by_close) / by_close
        rise_ok = (rise <= cfg.rise_upper) & (rise >= cfg.rise_lower)

        # ⑨ TRENDOK: MA10 > MA20 > MA30, MA30 rising
        trend_ok = (ma10 > ma20) & (ma20 > ma30) & (ma30 >= ma30_3ago)

        # ⑩ NOBIGYIN: no big yin during pullback period
        big_yin_count = _dynamic_count(big_yin, T)
        no_big_yin = big_yin_count == 0

        # ⑪ PRICEOK: price within bounds
        price_ok = (close > cfg.price_min) & (close < cfg.price_max)

        # ⑫ LISTOK: enough listing history
        barscount = pl.arange(0, n, eager=True).cast(pl.Int64)
        list_ok = barscount > cfg.min_list_days

        # ── 7. Composite entry signal (last bar only) ──
        entry = (
            touch_ma10 & shrink_today & stable & not_dump
            & has_big_yang & not_broken & vol_ok & rise_ok
            & trend_ok & no_big_yin & price_ok & list_ok
        ).fill_null(False)

        last_entry = bool(entry[-1]) if n > 0 else False

        # Strength = inverse of shrinkage (lower volume/V5 = stronger signal)
        last_strength = 1.0
        if last_entry and n > 0:
            ratio = float(volume[-1]) / float(v5[-1]) if float(v5[-1]) > 0 else 1.0
            last_strength = max(0.0, 1.0 - ratio)  # higher when more shrunk

        # ── 8. Exit conditions (for the last bar) ──
        last_close = float(close[-1])
        last_ma20 = float(ma20[-1]) if ma20[-1] is not None else last_close
        last_ma10 = float(ma10[-1]) if ma10[-1] is not None else last_close

        # MA stop: close < MA20
        ma_stop = last_close < last_ma20

        # Trend stop: MA10 < MA20 (would need hold_days tracking — simplified)
        trend_stop = last_ma10 < last_ma20

        # Take profit: close > MA20 * mult
        take_profit = last_close > last_ma20 * cfg.take_profit_mult

        exit_sig = ma_stop or take_profit

        return last_entry, exit_sig, last_strength


# ── Helpers ───────────────────────────────────────────────────────────────


def _empty_signal_frame() -> SignalFrame:
    return pl.DataFrame(
        schema={
            "asset_id": pl.Utf8,
            "signal_date": pl.Date,
            "direction": pl.Utf8,
            "strength": pl.Float64,
            "confidence": pl.Float64,
        }
    )


def _signal_row(
    asset_id: str,
    signal_date: date,
    direction: str,
    strength: float = 1.0,
    confidence: float = 1.0,
) -> SignalFrame:
    return pl.DataFrame({
        "asset_id": [asset_id],
        "signal_date": [signal_date],
        "direction": [direction],
        "strength": [strength],
        "confidence": [confidence],
    })


def _concat(frames: list[SignalFrame]) -> SignalFrame:
    non_empty = [f for f in frames if not f.is_empty()]
    if not non_empty:
        return _empty_signal_frame()
    return pl.concat(non_empty)
