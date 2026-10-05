"""A2-1 TDD：Combo 子策略构造安全。

1. 透传 8 字段（factor_weights / missing_factor_strategy / penalty_per_missing /
   dsl_spec / breakout_config / custom_weights / sub_strategy_configs / combo_method）
2. 嵌套 Combo 深度上限 3（第 4 层抛 ValueError）
3. 静默降级消除：未知 strategy_type / 缺 strategy_type 均抛错（破坏性变更）
4. 子 DSL 含 regime 段 → 构造期 warning（regime 是组合级语义，不阻断）
5. DSL 子策略校验注入：_dsl_validation_context 到达子策略（未知因子报错）
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.backtest_vector.strategies.combo import CompositeStrategy
from cquant.backtest_vector.strategies.custom_weight_strategy import CustomWeightStrategy
from cquant.backtest_vector.strategies.multi_factor import MultiFactorStrategy
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import ValidationContext

# 已知因子注入：验证 _dsl_validation_context 到达子策略（未知因子报错）
_VCTX = ValidationContext(known_factors={"ret_20d"}, custom_factors=set())


def _runner() -> BacktestRunner:
    runner = object.__new__(BacktestRunner)
    runner._catalog = None
    return runner


def _top_spec(subs: list[dict], **kw) -> BacktestRunSpec:
    base = dict(
        dataset_version="v1",
        strategy_id="combo_top",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 30),
        strategy_type="Combo",
        sub_strategy_configs=subs,
    )
    base.update(kw)
    return BacktestRunSpec(**base)


def _build(spec: BacktestRunSpec):
    return BacktestRunner._build_strategy(_runner(), spec)


# ── 1. 透传 ──────────────────────────────────────────────────────────────────


def test_combo_child_multifactor_gets_weights():
    """子策略 factor_weights 透传：不再是退化 {sort_factor: 1.0}。"""
    weights = {"factor_a": 0.6, "factor_b": 0.4}
    combo = _build(_top_spec([{
        "strategy_id": "mf_sub",
        "strategy_type": "MultiFactor",
        "factor_weights": weights,
        "missing_factor_strategy": "risk_penalty",
        "penalty_per_missing": 0.25,
    }]))
    assert isinstance(combo, CompositeStrategy)
    (child,) = combo._strategies
    assert isinstance(child, MultiFactorStrategy)
    assert child._factor_weights == weights  # 透传，而非 {"factor_a": 1.0}
    assert child._missing_factor_strategy == "risk_penalty"
    assert child._penalty_per_missing == 0.25


def test_combo_child_custom_weights_passthrough():
    """custom_weights 透传到 CustomWeightStrategy。"""
    combo = _build(_top_spec([{
        "strategy_id": "cw_sub",
        "strategy_type": "CustomWeightStrategy",
        "custom_weights": {"SSE:600000": 0.7, "SSE:600001": 0.3},
    }]))
    (child,) = combo._strategies
    assert isinstance(child, CustomWeightStrategy)
    assert child._weights == {"SSE:600000": 0.7, "SSE:600001": 0.3}


def test_combo_child_breakout_and_combo_method_passthrough():
    """breakout_config / combo_method 透传。"""
    combo = _build(_top_spec(
        [{
            "strategy_id": "bp_sub",
            "strategy_type": "BreakoutPullback",
            "top_n": 5,
            "breakout_config": {"N": 30},
        }],
        combo_method="rank_mean",
    ))
    (child,) = combo._strategies
    assert child._cfg.N == 30
    assert combo._method == "rank_mean"


def test_combo_child_defaults_not_overridden_by_none():
    """子配置未提供透传字段时维持策略默认（None 不覆盖）。"""
    combo = _build(_top_spec([{
        "strategy_id": "mf_sub",
        "strategy_type": "MultiFactor",
        # 无 factor_weights → 历史 {sort_factor: 1.0} 回退不变
        "sort_factor": "factor_a",
    }]))
    (child,) = combo._strategies
    assert child._factor_weights == {"factor_a": 1.0}
    assert child._missing_factor_strategy == "fill_0"


# ── 2. DSL 子策略 ────────────────────────────────────────────────────────────


_DSL_SUB = {
    "name": "sub_dsl",
    "score": [{"factor": "ret_20d", "weight": 1.0}],
}


def test_combo_child_dsl_builds_and_validates(monkeypatch):
    """dsl_spec 透传 + _dsl_validation_context 到达子策略（未知因子报错）。"""
    runner = _runner()
    monkeypatch.setattr(
        BacktestRunner, "_dsl_validation_context",
        lambda self: (_VCTX, {}),
    )

    combo = BacktestRunner._build_strategy(runner, _top_spec([{
        "strategy_id": "dsl_sub",
        "strategy_type": "DSL",
        "dsl_spec": dict(_DSL_SUB),
    }]))
    (child,) = combo._strategies
    assert isinstance(child, DSLStrategy)

    bad = _top_spec([{
        "strategy_id": "dsl_bad",
        "strategy_type": "DSL",
        "dsl_spec": {**_DSL_SUB, "score": [{"factor": "no_such_factor", "weight": 1.0}]},
    }])
    with pytest.raises(ValueError, match="不存在"):
        BacktestRunner._build_strategy(runner, bad)


def test_child_dsl_regime_segment_warns(caplog):
    """子策略 dsl_spec 含 regime 段 → 构造期 warning，不阻断。"""
    regime_spec = {
        **_DSL_SUB,
        "regime": {
            "mode": "threshold",
            "indicators": {"breadth": "ext.market_breadth_20d"},
            "rules": [
                {"when": "breadth < 0.2", "position_scale": 0.0},
                {"position_scale": 1.0},
            ],
        },
    }
    with caplog.at_level(logging.WARNING, logger="cquant.backtest_vector.run"):
        combo = _build(_top_spec([{
            "strategy_id": "dsl_regime",
            "strategy_type": "DSL",
            "dsl_spec": regime_spec,
        }]))
    assert isinstance(combo, CompositeStrategy)  # 未阻断
    assert any("regime" in r.message for r in caplog.records)


# ── 3. 深度上限 ──────────────────────────────────────────────────────────────


def _nest(levels: int) -> dict:
    """levels 层嵌套 Combo，最内层是 MultiFactor 叶子。"""
    leaf = {"strategy_id": "leaf", "strategy_type": "MultiFactor",
            "factor_weights": {"a": 1.0}}
    cfg = leaf
    for _ in range(levels - 1):
        cfg = {"strategy_id": "outer", "strategy_type": "Combo",
               "sub_strategy_configs": [cfg]}
    return cfg


def test_combo_nested_depth3_ok():
    """3 层嵌套恰好合法（边界内通过）。"""
    combo = _build(_top_spec([_nest(3)]))
    assert isinstance(combo, CompositeStrategy)


def test_combo_nested_depth4_raises():
    with pytest.raises(ValueError, match="combo nesting exceeds depth limit 3"):
        _build(_top_spec([_nest(4)]))


# ── 4. 静默降级消除 ──────────────────────────────────────────────────────────


def test_unknown_strategy_type_raises():
    """顶层与子层未知 strategy_type 均抛 ValueError（不再落 StaticTopN）。"""
    with pytest.raises(ValueError, match="unknown strategy_type"):
        _build(_top_spec([], strategy_type="NoSuchStrategy"))
    with pytest.raises(ValueError, match="unknown strategy_type"):
        _build(_top_spec([{"strategy_id": "s", "strategy_type": "NoSuchStrategy"}]))


def test_missing_strategy_type_raises():
    """子配置缺 strategy_type 键 → ValueError（不再默认 StaticTopN）。"""
    with pytest.raises(ValueError, match="strategy_type"):
        _build(_top_spec([{"strategy_id": "s", "top_n": 5}]))


# ── 5. API 创建路由 400 映射 ─────────────────────────────────────────────────


def test_api_sub_config_validation():
    """_validate_sub_strategy_configs：缺 type / 超 3 层抛 ValueError（路由转 400）。"""
    from cquant.api_server.routes.backtests import _validate_sub_strategy_configs

    # 合法：显式 type + 3 层嵌套
    _validate_sub_strategy_configs([_nest(3)])
    _validate_sub_strategy_configs([{"strategy_type": "MultiFactor"}])

    with pytest.raises(ValueError, match="strategy_type"):
        _validate_sub_strategy_configs([{"top_n": 5}])
    with pytest.raises(ValueError, match="depth limit 3"):
        _validate_sub_strategy_configs([_nest(4)])
