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


class _SignalsUntil(Strategy):
    """Emits buy-and-hold signals only before ``cutoff``; empty afterwards."""

    def __init__(self, asset_ids: list[str], cutoff: date) -> None:
        self._asset_ids = asset_ids
        self._cutoff = cutoff

    @property
    def strategy_id(self) -> str:
        return "regime_test_sporadic"

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        if ctx.as_of_date >= self._cutoff:
            return pl.DataFrame(schema={
                "asset_id": pl.String,
                "signal_date": pl.Date,
                "direction": pl.String,
                "strength": pl.Float64,
                "confidence": pl.Float64,
            })
        return pl.DataFrame({
            "asset_id": self._asset_ids,
            "signal_date": [ctx.as_of_date] * len(self._asset_ids),
            "direction": ["long"] * len(self._asset_ids),
            "strength": [1.0] * len(self._asset_ids),
            "confidence": [1.0] * len(self._asset_ids),
        })


def _prices_with_limit_down_range(
    asset: str, start: date, end: date,
) -> pl.DataFrame:
    """Two-asset drift frame where ``asset`` is limit-down every day in
    [start, end] (close==low at exactly -10% — main-board signature)."""
    rows: list[dict] = []
    prev_close: dict[str, float] = {}
    for i in range(N_DAYS):
        d = START + timedelta(days=i)
        for a, base in (("SH600001", 10.0), ("SH600002", 20.0)):
            prev = prev_close.get(a, base)
            p = base * (1 + 0.001 * i)
            open_, high, low = p, p * 1.01, p * 0.99
            if start <= d <= end and a == asset:
                # limit-down vs the ACTUAL prior close (which may itself be
                # a limit-down close on multi-day blocks)
                p = round(prev * 0.90, 2)
                open_ = high = low = p
            prev_close[a] = p
            rows.append({
                "trade_date": d, "asset_id": a,
                "open": open_, "high": high, "low": low,
                "close": p, "volume": 1_000_000.0, "amount": p * 1_000_000,
                "is_suspended": False,
            })
    return pl.DataFrame(rows)


def _run_sporadic(regime_sm, cutoff: date, prices: pl.DataFrame):
    """Like _run but the strategy stops emitting signals at ``cutoff``."""
    engine = VectorBacktestEngine()
    spec = BacktestSpec(
        strategy=_SignalsUntil(["SH600001", "SH600002"], cutoff),
        prices=prices,
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

    def test_regime_full_derisk_logged_in_forced_exits(self) -> None:
        """Backlog #5: scale→0 full de-risk appears in result.forced_exits
        (reason="regime_risk_off"), one entry per committed position."""
        derisk_day = _week_start(2)
        result = _run(ScriptedRegime({derisk_day: 0.0}, sticky_zero=True))
        assert result.error is None, result.error

        regime_entries = [
            e for e in result.forced_exits if e["reason"] == "regime_risk_off"
        ]
        assert regime_entries, "full de-risk must be visible in the exit feed"
        by_date = {e["date"] for e in regime_entries}
        assert by_date == {derisk_day}, (
            f"entries expected exactly on the de-risk rebalance day; got {by_date}"
        )
        logged_assets = {e["asset_id"] for e in regime_entries}
        assert logged_assets == {"SH600001", "SH600002"}
        for e in regime_entries:
            assert e["entry_price"] > 0
            assert e["exit_price"] > 0
        # cooldown exemption: regime de-risk does NOT poison re-entry — the
        # next rebalance after the scale recovers to 1.0 may re-buy (no
        # force_exited_assets entries are created by the regime branch).
        result2 = _run(ScriptedRegime({derisk_day: 0.0}, sticky_zero=False))
        assert result2.error is None, result2.error
        rebuys = result2.fills.filter(
            (pl.col("side") == "buy") & (pl.col("trade_date") > derisk_day)
        )
        assert rebuys.height > 0, (
            "recovery rebalance must be able to re-enter (cooldown-exempt)"
        )

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

    def test_regime_pending_survives_empty_signal_rebalance(self) -> None:
        """B3: empty-signal rebalance day must still evaluate the regime.

        Construction: scale=0 de-risk at week-1 rebalance; A's sell is
        limit-down blocked through the rest of the week; the week-2
        rebalance strategy returns EMPTY signals. The regime state machine
        is still latched at scale 0, so the "regime:" pending sell for A
        must NOT be dropped (the buggy ``regime_desired.get(td, 1.0)``
        default reads as "recovered" on days with no evaluation entry).
        """
        derisk_day = _week_start(1)                 # signals still emitted here
        empty_day = _week_start(2)                  # strategy returns no signals
        block_start = derisk_day + timedelta(days=1)
        block_end = empty_day - timedelta(days=1)   # A blocked until rebalance eve

        prices = _prices_with_limit_down_range(
            "SH600001", block_start, block_end,
        )
        sm = ScriptedRegime({derisk_day: 0.0}, sticky_zero=True)
        result = _run_sporadic(sm, cutoff=empty_day, prices=prices)
        assert result.error is None, result.error

        # the state machine was evaluated ON the empty-signal rebalance day
        assert empty_day in sm.calls, (
            "regime must be evaluated on empty-signal rebalance days "
            f"(reevaluate:daily semantics); calls={sm.calls}"
        )
        # ...and the latched scale-0 decision is recorded for that day
        hist = result.regime_scale_history.filter(pl.col("trade_date") == empty_day)
        assert hist.height == 1 and hist["desired_scale"][0] == 0.0

        # the blocked de-risk sell survived the empty rebalance and filled after
        a_sells = result.fills.filter(
            (pl.col("asset_id") == "SH600001") & (pl.col("side") == "sell")
        ).sort("trade_date")
        assert a_sells.height >= 1, (
            "regime: pending sell must be retried across the empty-signal "
            "rebalance while the state machine is still latched at scale 0"
        )
        assert a_sells["trade_date"][0] >= empty_day, (
            "A is limit-down until the rebalance eve; its first possible fill "
            "is on/after the empty-signal rebalance day"
        )

    def test_cleanup_runs_on_full_derisk_rebalance(self) -> None:
        """B3 fix round 1: the "Always clear on rebalance" cleanup block must
        run on a signal-day full de-risk rebalance (weights_dict == {}) too.

        Construction: A is force-exited (stop-loss, full exit → cooldown)
        mid-week; the NEXT rebalance is a regime scale→0 full de-risk (its
        weights_dict is voided to {}). The cooldown on A must be cleared at
        that de-risk rebalance, so when the regime recovers at the following
        rebalance, A's signals are not filtered and A is re-bought.
        (Regression: the B3 dedent accidentally placed the cleanup inside
        ``if weights_dict and nav_estimate > 0:``, skipping it on full
        de-risk rebalances.)
        """
        from cquant.core.enums import RiskDecisionType
        from cquant.core.types import OrderIntent, RiskDecision, RiskSnapshot
        from cquant.riskguard.models import RiskContext
        from cquant.riskguard.policies.base import RiskPolicy
        from cquant.riskguard.policies.forced_exit import (
            ForcedExit,
            ForcedExitPolicy,
        )

        class _StopLossDual(ForcedExitPolicy, RiskPolicy):
            @property
            def name(self) -> str:
                return "test_stop_loss_dual"

            def evaluate(
                self, candidate: OrderIntent, snapshot: RiskSnapshot, ctx: RiskContext
            ) -> RiskDecision:
                return RiskDecision(
                    decision=RiskDecisionType.APPROVED,
                    original_qty=candidate.requested_qty,
                    approved_qty=candidate.requested_qty,
                    reasons=[],
                    policy_names=[self.name],
                )

            def check_exits(self, positions, current_prices, entry_prices, state=None):
                exits = []
                for aid in positions:
                    entry = entry_prices.get(aid, 0.0)
                    if entry <= 0 or aid not in current_prices:
                        continue
                    if (current_prices[aid] - entry) / entry < -0.05:
                        exits.append(ForcedExit(
                            asset_id=aid, reason="stop", urgency="high",
                        ))
                return exits

        derisk_day = _week_start(2)          # regime scale→0 rebalance
        recover_day = _week_start(3)         # regime back to 1.0
        next_rebalance = _week_start(4)
        drop_day = _week_start(1) + timedelta(days=2)  # forced exit mid-week

        rows: list[dict] = []
        for i in range(N_DAYS):
            d = START + timedelta(days=i)
            p_a = 10.0 if d < drop_day else 8.0        # -20% after drop_day
            p_b = 20.0 * (1 + 0.001 * i)
            for asset, p in (("SH600001", p_a), ("SH600002", p_b)):
                rows.append({
                    "trade_date": d, "asset_id": asset,
                    "open": p, "high": p * 1.01, "low": p * 0.99,
                    "close": p, "volume": 1_000_000.0, "amount": p * 1_000_000,
                    "is_suspended": False,
                })
        prices = pl.DataFrame(rows)

        engine = VectorBacktestEngine()
        spec = BacktestSpec(
            strategy=_BuyAndHold(["SH600001", "SH600002"]),
            prices=prices,
            start_date=START,
            end_date=END,
            initial_cash=Decimal("1_000_000"),
            cost_model=CostModel.for_cn(),
            rebalance_frequency="1w",
            regime_sm=ScriptedRegime({derisk_day: 0.0, recover_day: 1.0}),
            risk_policies=[_StopLossDual()],
        )
        result = engine.run(spec)
        assert result.error is None, result.error

        # A was force-exited before the de-risk rebalance (sanity)
        a_exits = [e for e in result.forced_exits if e["asset_id"] == "SH600001"]
        assert a_exits and a_exits[0]["date"] < derisk_day

        # Regression: A must be re-bought at the recovery rebalance — the
        # de-risk rebalance (weights_dict == {}) still cleared its cooldown.
        a_rebuys = result.fills.filter(
            (pl.col("asset_id") == "SH600001")
            & (pl.col("side") == "buy")
            & (pl.col("trade_date") >= recover_day)
            & (pl.col("trade_date") < next_rebalance)
        )
        assert a_rebuys.height > 0, (
            "cooldown from A's forced exit must be cleared on the full "
            "de-risk rebalance, allowing re-entry when the regime recovers"
        )

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


class TestHoldStateWarnings:
    def test_hold_state_warnings_not_accumulating(self) -> None:
        """Consecutive missing-data days must not forward-accumulate warnings."""
        d = RegimeDef(mode="continuous", scale_expr="zscore(x, 20)")
        ok_day = date(2025, 3, 1)
        ctx = StubMarketCtx({("zscore(x, 20)", ok_day): 0.3})
        sm = RegimeStateMachine(d, ctx)  # type: ignore[arg-type]
        sm.evaluate(ok_day)
        results = [sm.evaluate(ok_day + timedelta(days=i)) for i in (1, 2, 3)]
        assert all(r.warnings for r in results), "each day must surface its own warning"
        assert all(len(r.warnings) <= 2 for r in results)
        assert len(results[2].warnings) == 1  # day 3: exactly the day's reason
        # scale/state still carried forward
        assert abs(results[2].position_scale - 0.3) < 1e-12
