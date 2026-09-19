"""TDD tests: factor_weights must flow from UI strategy config to MultiFactorStrategy engine.

Breakage being fixed (Phase 0 friction 3-3):
  UI collects factor_weights -> stored in strategy config
  -> BacktestCreateBody had no factor_weights field
  -> BacktestRunSpec had no factor_weights field
  -> _build_strategy hardcoded {sort_factor: 1.0}
"""

from __future__ import annotations

import logging

import pytest

from cquant.api_server.routes.backtests import BacktestCreateBody
from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.backtest_vector.strategies.multi_factor import MultiFactorStrategy


def _spec(**kwargs) -> BacktestRunSpec:
    """Build a MultiFactor BacktestRunSpec with sensible defaults."""
    base = dict(
        dataset_version="test_ds",
        strategy_id="mf_test",
        start_date=__import__("datetime").date(2025, 1, 1),
        end_date=__import__("datetime").date(2025, 6, 30),
        strategy_type="MultiFactor",
        sort_factor="factor_a",
    )
    base.update(kwargs)
    return BacktestRunSpec(**base)


def _build(spec: BacktestRunSpec) -> MultiFactorStrategy:
    # _build_strategy does not touch self/catalog; call without init (no DuckDB needed).
    runner = object.__new__(BacktestRunner)
    return BacktestRunner._build_strategy(runner, spec)


def test_factor_weights_flow_to_engine():
    """Strategy config factors=[a,b,c], factor_weights={a:0.4,b:0.4,c:0.2}
    via BacktestCreateBody -> BacktestRunSpec -> MultiFactorStrategy
    must yield strategy._factor_weights == {a:0.4, b:0.4, c:0.2}."""
    weights = {"factor_a": 0.4, "factor_b": 0.4, "factor_c": 0.2}

    # 1. Request body accepts factor_weights (UI -> API contract)
    body = BacktestCreateBody(
        strategy_id="mf_test",
        dataset_version="test_ds",
        start_date="2025-01-01",
        end_date="2025-06-30",
        strategy_type="MultiFactor",
        factor_weights=weights,
    )
    assert body.factor_weights == weights

    # 2. RunSpec carries factor_weights (API -> engine contract)
    spec = _spec(factor_weights=dict(body.factor_weights))
    assert spec.factor_weights == weights

    # 3. Engine strategy receives them (not the old hardcoded {sort_factor: 1.0})
    strategy = _build(spec)
    assert isinstance(strategy, MultiFactorStrategy)
    assert strategy._factor_weights == {"factor_a": 0.4, "factor_b": 0.4, "factor_c": 0.2}


def test_no_weights_backward_compat():
    """Legacy strategies without factor_weights: engine behavior bit-for-bit
    identical to the pre-fix hardcoded fallback {sort_factor: 1.0}."""
    spec = _spec()  # factor_weights is None / absent
    assert spec.factor_weights is None
    strategy = _build(spec)
    assert isinstance(strategy, MultiFactorStrategy)
    # Exact pre-fix hardcoded form (was: factor_weights={spec.sort_factor: 1.0})
    assert strategy._factor_weights == {"factor_a": 1.0}
    assert strategy._top_n == spec.top_n
    assert strategy._missing_factor_strategy == spec.missing_factor_strategy
    assert strategy._penalty_per_missing == spec.penalty_per_missing


def test_weights_validation():
    """factor_weights keys not in the strategy's factors list -> rejected with error;
    all-zero weights -> rejected."""
    from cquant.api_server.routes.backtests import _validate_factor_weights

    factors = ["factor_a", "factor_b", "factor_c"]

    # Unknown key -> rejected (with log), not silently ignored
    with pytest.raises(ValueError, match="factor_d"):
        _validate_factor_weights({"factor_a": 0.5, "factor_d": 0.5}, factors)

    # All-zero weights -> rejected
    with pytest.raises(ValueError, match="全为零"):
        _validate_factor_weights({"factor_a": 0.0, "factor_b": 0.0}, factors)

    # Empty dict -> rejected (degenerate; must not silently fall back)
    with pytest.raises(ValueError):
        _validate_factor_weights({}, factors)

    # Valid weights pass through unchanged
    ok = {"factor_a": 0.4, "factor_b": 0.4, "factor_c": 0.2}
    assert _validate_factor_weights(ok, factors) == ok

    # No weights provided (legacy) -> None passes through untouched
    assert _validate_factor_weights(None, factors) is None

    # Unknown keys are logged
    logger = logging.getLogger("cquant.api_server.routes.backtests")
    with pytest.raises(ValueError), caplog_context(logger):
        _validate_factor_weights({"nope": 1.0}, factors)


class caplog_context:
    """Minimal helper: assert at least one log record is emitted."""

    def __init__(self, logger):
        self.logger = logger
        self.records: list = []

    def __enter__(self):
        self.orig = self.logger.handle
        self.logger.handle = lambda record: self.records.append(record)
        return self

    def __exit__(self, *exc):
        self.logger.handle = self.orig
        assert self.records, "expected a log record for invalid factor_weights keys"
        return False
