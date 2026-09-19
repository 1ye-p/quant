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


# ── Route-level wiring tests (config priority + explicit-body override) ──────

import json

import polars as pl
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

import cquant.api_server.routes.backtests as bt_routes


CFG_WEIGHTS = {"factor_a": 0.5, "factor_b": 0.3, "factor_c": 0.2}
CFG_JSON = json.dumps({
    "strategy_type": "MultiFactor",
    "factors": ["factor_a", "factor_b", "factor_c"],
    "factor_weights": CFG_WEIGHTS,
})


class _StubCatalog:
    """Catalog stub: 1st query returns the strategy config, later queries empty."""

    def __init__(self):
        self._first = True

    def query(self, sql, params=None):
        if self._first and "meta_strategy_configs" in sql:
            self._first = False
            return pl.DataFrame({"parsed_config": [CFG_JSON]})
        return pl.DataFrame()

    def execute(self, *a, **kw):
        return None


def _body(**kwargs):
    base = dict(
        strategy_id="mf_test",
        dataset_version="test_ds",
        start_date="2025-01-01",
        end_date="2025-06-30",
        strategy_type="MultiFactor",
        sort_factor="factor_a",
    )
    base.update(kwargs)
    return bt_routes.BacktestCreateBody(**base)


@pytest.fixture()
def _route_env(monkeypatch):
    """Neutralize persistence/side effects; capture the spec reaching the engine."""
    captured: dict = {}

    def _fake_run_backtest(catalog, spec):
        captured["spec"] = spec
        return "run_1"

    monkeypatch.setattr(bt_routes, "_run_backtest", _fake_run_backtest)
    monkeypatch.setattr(bt_routes, "_ensure_schema_extensions", lambda cat: None)
    monkeypatch.setattr(bt_routes, "_ensure_job_table", lambda cat: None)
    monkeypatch.setattr(bt_routes, "_save_job", lambda *a, **kw: None)
    return captured


def _run_route(body) -> dict:
    """Invoke the create_backtest route function directly; run the background job."""
    import asyncio

    bt = BackgroundTasks()
    result = asyncio.run(bt_routes.create_backtest(body, bt, _StubCatalog()))
    assert result["status"] == "running"
    # Execute the queued job synchronously (spec capture happens in _run_backtest).
    # tasks[0].func == run_job_async (coroutine wrapper); args[0] is the _run_job closure.
    for task in bt.tasks:
        task.args[0]()
    return result


def test_route_config_priority(_route_env):
    """策略配置有 factor_weights 时，body 不传 → 用配置值；body 显式传 → 覆盖。"""
    captured = _route_env

    # body 不传 factor_weights → 用策略配置值
    _run_route(_body())
    assert captured["spec"].factor_weights == CFG_WEIGHTS

    # body 显式传（非 None）→ 单次运行覆盖配置
    override = {"factor_a": 0.2, "factor_b": 0.2, "factor_c": 0.6}
    _run_route(_body(factor_weights=override))
    assert captured["spec"].factor_weights == override


def test_route_empty_dict_rejected(_route_env):
    """body 显式 factor_weights={} → 400（非静默回退到策略配置）。"""
    with pytest.raises(HTTPException) as exc_info:
        _run_route(_body(factor_weights={}))
    assert exc_info.value.status_code == 400
    assert "不能为空" in exc_info.value.detail


# ── DSL spec route-level wiring (same config-priority semantics) ─────────────

DSL_CFG_SPEC = {
    "name": "dsl_demo",
    "universe": "all",
    "frequency": "daily",
    "score": [{"factor": "ret_20d", "weight": 1.0}],
    "position": {"method": "equal_weight", "params": {}, "constraints": {}},
    "risk": [],
}
DSL_CFG_JSON = json.dumps({
    "strategy_type": "DSL",
    "strategy_id": "dsl_demo",
    "dsl_spec": DSL_CFG_SPEC,
})


class _DslStubCatalog(_StubCatalog):
    """Catalog stub returning a DSL strategy config."""

    def __init__(self):
        self._first = True

    def query(self, sql, params=None):
        if self._first and "meta_strategy_configs" in sql:
            self._first = False
            return pl.DataFrame({"parsed_config": [DSL_CFG_JSON]})
        return pl.DataFrame()


def _dsl_body(**kwargs):
    base = dict(
        strategy_id="dsl_demo",
        dataset_version="test_ds",
        start_date="2025-01-01",
        end_date="2025-06-30",
        strategy_type="DSL",
    )
    base.update(kwargs)
    return bt_routes.BacktestCreateBody(**base)


def _run_dsl_route(body, catalog) -> dict:
    import asyncio

    bt = BackgroundTasks()
    result = asyncio.run(bt_routes.create_backtest(body, bt, catalog))
    assert result["status"] == "running"
    for task in bt.tasks:
        task.args[0]()
    return result


def test_route_dsl_spec_body_passthrough(_route_env):
    """body 带 dsl_spec + strategy_type=DSL → spec.dsl_spec 透传（body 优先）。"""
    captured = _route_env
    body_spec = dict(DSL_CFG_SPEC, name="body_override")

    _run_dsl_route(_dsl_body(dsl_spec=body_spec), _DslStubCatalog())
    assert captured["spec"].dsl_spec == body_spec
    assert captured["spec"].strategy_type == "DSL"


def test_route_dsl_spec_config_fallback(_route_env):
    """策略配置带 dsl_spec 而 body 不带 → 也透传（config 优先语义）。"""
    captured = _route_env

    _run_dsl_route(_dsl_body(), _DslStubCatalog())
    assert captured["spec"].dsl_spec == DSL_CFG_SPEC
    assert captured["spec"].strategy_type == "DSL"
