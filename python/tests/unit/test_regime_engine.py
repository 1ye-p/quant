"""Regime engine-layer tests (P3S-2 T4).

Machine-level semantics (threshold / switch-latch / continuous, hold-on-
missing-data) plus the 5-item engine checklist against synthetic in-memory
data (construction pattern follows test_delisting_e2e.py).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import polars as pl

from cquant.backtest_vector.costs import CostModel
from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.strategy_dsl.regime import RegimeResult, RegimeStateMachine
from cquant.strategy_dsl.schema import RegimeDef, RuleDef, StateDef

START = date(2025, 1, 6)  # Monday
N_DAYS = 40
END = START + timedelta(days=N_DAYS - 1)


# ── fixtures ───────────────────────────────────────────────────────────────

class _BuyAndHold(Strategy):
    """Always-long 2 assets (pattern from test_delisting_e2e)."""

    def __init__(self, asset_ids: list[str]) -> None:
        self._asset_ids = asset_ids

    @property
    def strategy_id(self) -> str:
        return "regime_test_buy_hold"

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        return pl.DataFrame({
            "asset_id": self._asset_ids,
            "signal_date": [ctx.as_of_date] * len(self._asset_ids),
            "direction": ["long"] * len(self._asset_ids),
            "strength": [1.0] * len(self._asset_ids),
            "confidence": [1.0] * len(self._asset_ids),
        })


class ScriptedRegime:
    """Engine-side stub: date → position_scale, else 1.0.

    ``sticky_zero=True`` keeps scale at 0 once it hits 0 (mimics a latched
    risk-off regime across rebalances).
    """

    def __init__(self, scales: dict[date, float], sticky_zero: bool = False) -> None:
        self._scales = scales
        self._sticky_zero = sticky_zero
        self._latched_zero = False
        self.calls: list[date] = []

    def evaluate(self, as_of_date: date) -> RegimeResult:
        self.calls.append(as_of_date)
        scale = self._scales.get(as_of_date, 1.0)
        if scale <= 0.0:
            self._latched_zero = True
        elif self._latched_zero and self._sticky_zero:
            scale = 0.0
        return RegimeResult(
            position_scale=scale,
            state="scripted",
            as_of_date=as_of_date,
        )


class StubMarketCtx:
    """Machine-side stub: (expr, as_of) → value; KeyError → missing data."""

    def __init__(self, mapping: dict[tuple[str, date], float]) -> None:
        self._mapping = mapping

    def evaluate(self, expr: str, as_of: date, indicators=None) -> float:
        return self._mapping[(expr, as_of)]


def _two_asset_prices(limit_down_day: date | None = None) -> pl.DataFrame:
    """A/B steady upward drift; A has a single limit-down day when asked."""
    rows: list[dict] = []
    for i in range(N_DAYS):
        d = START + timedelta(days=i)
        for asset, base in (("SH600001", 10.0), ("SH600002", 20.0)):
            p = base * (1 + 0.001 * i)
            open_, high, low = p, p * 1.01, p * 0.99
            if limit_down_day is not None and d == limit_down_day and asset == "SH600001":
                prev = base * (1 + 0.001 * (i - 1))
                p = round(prev * 0.90, 2)   # exactly -10% (main board)
                open_ = high = p
                low = p                    # close == low → limit-down signature
            rows.append({
                "trade_date": d, "asset_id": asset,
                "open": open_, "high": high, "low": low,
                "close": p, "volume": 1_000_000.0, "amount": p * 1_000_000,
                "is_suspended": False,
            })
    return pl.DataFrame(rows)


def _run(regime_sm, prices: pl.DataFrame | None = None):
    engine = VectorBacktestEngine()
    spec = BacktestSpec(
        strategy=_BuyAndHold(["SH600001", "SH600002"]),
        prices=prices if prices is not None else _two_asset_prices(),
        start_date=START,
        end_date=END,
        initial_cash=Decimal("1_000_000"),
        cost_model=CostModel.for_cn(),
        rebalance_frequency="1w",
        regime_sm=regime_sm,
    )
    return engine.run(spec)


def _week_start(week: int) -> date:
    """Rebalance dates = first trading day of each week (weekly freq)."""
    return START + timedelta(days=7 * week)


# ── engine checklist tests ─────────────────────────────────────────────────

class TestEngineRegime:
    def test_regime_scale_zero_sells(self) -> None:
        """Checklist #1/#2: scale→0 after optimizer → T+1 sell of everything."""
        derisk_day = _week_start(2)
        result = _run(ScriptedRegime({derisk_day: 0.0}, sticky_zero=True))
        assert result.error is None, result.error

        sells = result.fills.filter(
            (pl.col("side") == "sell") & (pl.col("trade_date") > derisk_day)
        )
        sold_assets = set(sells["asset_id"].to_list())
        assert sold_assets == {"SH600001", "SH600002"}, (
            f"both assets must be sold after de-risk day; got {sold_assets}"
        )
        # first sell executes strictly after the de-risk decision day (T+1)
        first_sell = sells["trade_date"].min()
        assert first_sell > derisk_day

        # desired scale recorded as 0 from the de-risk rebalance onward
        hist = result.regime_scale_history.filter(pl.col("trade_date") >= derisk_day)
        assert hist.height > 0
        assert (hist["desired_scale"] == 0.0).all()
        # and exposure is actually liquidated eventually (actual catches up)
        assert (hist["actual_scale"].tail(3) < 0.05).any()

    def test_regime_t1_blocked_reinjection(self) -> None:
        """Checklist #2: limit-down blocks the de-risk sell → retry next days.

        The blocked asset (A, limit-down on the execution day) must still be
        sold later via the pending_force_exits reinjection ("regime:" keys),
        and the injected retry weights must be non-trivially present in the
        weight stream on multiple dates.
        """
        derisk_day = _week_start(2)
        exec_day = derisk_day + timedelta(days=1)  # next trading (calendar) day
        result = _run(
            ScriptedRegime({derisk_day: 0.0}, sticky_zero=True),
            prices=_two_asset_prices(limit_down_day=exec_day),
        )
        assert result.error is None, result.error

        a_sells = result.fills.filter(
            (pl.col("asset_id") == "SH600001") & (pl.col("side") == "sell")
        ).sort("trade_date")
        assert a_sells.height >= 1, "blocked sell must eventually fill on retry"
        assert a_sells["trade_date"][0] > exec_day, (
            "A's first sell cannot fill on its limit-down day"
        )
        # reinjection: zero-target weight rows for A on more than one date
        a_targets = result.positions.filter(
            (pl.col("asset_id") == "SH600001") & (pl.col("target_weight") == 0.0)
        )
        assert a_targets.height >= 2, (
            "pending regime sells must be re-injected across days until filled"
        )

    def test_regime_limitdown_unfilled_exposure(self) -> None:
        """Checklist #3: desired vs actual scale diverge while blocked."""
        derisk_day = _week_start(2)
        exec_day = derisk_day + timedelta(days=1)
        result = _run(
            ScriptedRegime({derisk_day: 0.0}, sticky_zero=True),
            prices=_two_asset_prices(limit_down_day=exec_day),
        )
        hist = result.regime_scale_history.filter(pl.col("trade_date") == exec_day)
        assert hist.height == 1
        row = hist.to_dicts()[0]
        assert row["desired_scale"] == 0.0
        # A could not be sold (limit-down) → B-only liquidation leaves real
        # exposure far above the desired zero
        assert row["actual_scale"] > 0.4, (
            f"actual_scale {row['actual_scale']} must reflect the blocked sell"
        )

    def test_regime_scale_recovery_refill(self) -> None:
        """Checklist #4: scale recovers → next rebalance naturally refills."""
        derisk_day = _week_start(1)
        recover_day = _week_start(2)
        result = _run(ScriptedRegime({derisk_day: 0.0, recover_day: 1.0}))
        assert result.error is None, result.error

        post_recovery_buys = result.fills.filter(
            (pl.col("side") == "buy") & (pl.col("trade_date") > recover_day)
        )
        assert post_recovery_buys.height > 0, (
            "recovered regime must re-enter via the next rebalance targets"
        )
        hist = result.regime_scale_history
        assert hist["desired_scale"].tail(1)[0] == 1.0

    def test_engine_holds_scale_between_rebalances(self) -> None:
        """Partial scale flows through FillSimulator as reduced weights."""
        scale_day = _week_start(2)
        result = _run(ScriptedRegime({scale_day: 0.5}))
        assert result.error is None, result.error
        # weights injected on the rebalance following scale_day are halved
        w = result.positions.filter(
            (pl.col("trade_date") > scale_day) & (pl.col("trade_date") <= scale_day + timedelta(days=7))
        )
        assert w.height > 0
        assert (w["target_weight"] <= 0.26).all(), (
            "equal-weight 0.5 per asset must scale to ~0.25"
        )


# ── machine-level semantics tests ──────────────────────────────────────────

class TestRegimeStateMachine:
    def test_threshold_first_match_and_default(self) -> None:
        d = RegimeDef(
            mode="threshold",
            rules=[
                RuleDef(when="x < -1", position_scale=0.0),
                RuleDef(when="x < 0", position_scale=0.5),
                RuleDef(when=None, position_scale=1.0),
            ],
        )
        ctx = StubMarketCtx({
            ("x < -1", date(2025, 1, 6)): 1.0,
            ("x < 0", date(2025, 1, 7)): 1.0,
            ("x < -1", date(2025, 1, 7)): 0.0,
            ("x < -1", date(2025, 1, 8)): 0.0,
            ("x < 0", date(2025, 1, 8)): 0.0,
        })
        sm = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        assert sm.evaluate(date(2025, 1, 6)).position_scale == 0.0  # first rule wins
        assert sm.evaluate(date(2025, 1, 7)).position_scale == 0.5  # second
        assert sm.evaluate(date(2025, 1, 8)).position_scale == 1.0  # default fallback

    def test_regime_latch_no_refire(self) -> None:
        """Switch latches in declared order (D6); re-evaluation is idempotent
        and does not flip back while the entering condition still holds."""
        d = RegimeDef(
            mode="switch",
            initial="risk_off",
            states=[
                StateDef(name="risk_off", enter_when="y > 0", position_scale=0.0),
                StateDef(name="risk_on", enter_when="y <= 0", position_scale=1.0),
            ],
        )
        ctx = StubMarketCtx({
            ("y > 0", date(2025, 1, 6)): 1.0,   # day1: risk_off enters (already initial)
            ("y <= 0", date(2025, 1, 6)): 0.0,
            ("y > 0", date(2025, 1, 7)): 1.0,   # day2: risk_off enter_when still true → latch
            ("y <= 0", date(2025, 1, 7)): 0.0,
            ("y > 0", date(2025, 1, 8)): 0.0,   # day3: only risk_on fires → transition
            ("y <= 0", date(2025, 1, 8)): 1.0,
            ("y > 0", date(2025, 1, 9)): 0.0,   # day4: nothing fires → hold risk_on
            ("y <= 0", date(2025, 1, 9)): 0.0,
        })
        sm = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        r1 = sm.evaluate(date(2025, 1, 6))
        assert r1.state == "risk_off" and r1.position_scale == 0.0
        r2 = sm.evaluate(date(2025, 1, 7))
        assert r2.state == "risk_off" and r2.position_scale == 0.0  # no re-fire
        assert r2.warnings == []
        r3 = sm.evaluate(date(2025, 1, 8))
        assert r3.state == "risk_on" and r3.position_scale == 1.0
        r4 = sm.evaluate(date(2025, 1, 9))
        assert r4.state == "risk_on" and r4.position_scale == 1.0  # latch holds

    def test_regime_data_missing_hold_state(self) -> None:
        """Missing/failed indicator → keep previous scale + warning (no silent
        switch)."""
        d = RegimeDef(mode="continuous", scale_expr="zscore(x, 20)")
        ok_day, missing_day = date(2025, 3, 1), date(2025, 3, 2)
        ctx = StubMarketCtx({("zscore(x, 20)", ok_day): 0.3})
        sm = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        r1 = sm.evaluate(ok_day)
        assert abs(r1.position_scale - 0.3) < 1e-12 and r1.warnings == []
        r2 = sm.evaluate(missing_day)  # KeyError inside → hold-state
        assert abs(r2.position_scale - 0.3) < 1e-12
        assert r2.warnings, "missing data must surface a warning"
        # first-day-ever failure falls back to full scale (not zero)
        sm2 = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        r0 = sm2.evaluate(missing_day)
        assert r0.position_scale == 1.0 and r0.warnings

    def test_continuous_clamped_to_unit_interval(self) -> None:
        d = RegimeDef(mode="continuous", scale_expr="x / 10")
        ctx = StubMarketCtx({
            ("x / 10", date(2025, 1, 6)): 25.0,
            ("x / 10", date(2025, 1, 7)): -3.0,
            ("x / 10", date(2025, 1, 8)): 0.7,
        })
        sm = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        assert sm.evaluate(date(2025, 1, 6)).position_scale == 1.0   # clamp high
        assert sm.evaluate(date(2025, 1, 7)).position_scale == 0.0   # clamp low
        assert abs(sm.evaluate(date(2025, 1, 8)).position_scale - 0.7) < 1e-12
