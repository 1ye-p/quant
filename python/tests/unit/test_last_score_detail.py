"""B2：策略侧 last_score_detail / missing_factors 可选能力暴露。

MultiFactorStrategy 与 DSLStrategy 均在 generate_signals 成功路径结尾写入
``last_score_detail``（列形态：asset_id / score / rank 前三列 + ``_w_{factor}``
分项列，分项列名保留 ``_w_`` 前缀），并在每次调用开头清空（失败/空数据时保持
None，无跨日残留）；缺失因子经 warning 点累计到 ``missing_factors``。
Strategy ABC 无相关属性 —— 引擎用 ``getattr(strategy, "last_score_detail", None)``
可选检测，普通策略不炸。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from cquant.backtest_vector.strategy import StrategyContext
from cquant.backtest_vector.strategies.multi_factor import MultiFactorStrategy
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import StrategyDSL

_DAY = date(2025, 6, 2)


def _features(extra_factor: str | None = None) -> pl.DataFrame:
    """8 资产 × 1 日截面；可选追加一个只有部分策略配置的因子列。"""
    rows = []
    raw = [
        ("SSE:600000", 0.9, -0.4),
        ("SSE:600001", 0.5, 0.1),
        ("SSE:600002", 0.1, 0.6),
        ("SSE:600003", -0.2, -0.8),
        ("SSE:600004", -0.5, 0.3),
        ("SSE:600005", 0.7, -0.1),
        ("SSE:600006", -0.9, 0.9),
        ("SSE:600007", 0.3, -0.6),
    ]
    for asset_id, mom, ret in raw:
        row = {"asset_id": asset_id, "trade_date": _DAY, "momentum_60d": mom, "ret_20d": ret}
        if extra_factor:
            row[extra_factor] = 0.0
        rows.append(row)
    return pl.DataFrame(rows)


def _ctx(features: pl.DataFrame) -> StrategyContext:
    return StrategyContext(
        as_of_date=_DAY,
        universe_id="test_universe",
        feature_set_version="v1",
        features=features,
    )


def _make_multifactor() -> MultiFactorStrategy:
    return MultiFactorStrategy(
        strategy_id="mf_test",
        factor_weights={"momentum_60d": 0.6, "ret_20d": 0.4},
        top_n=3,
    )


def _make_dsl() -> DSLStrategy:
    spec = StrategyDSL.from_dict({
        "name": "test_dsl",
        "score": [
            {"factor": "momentum_60d", "weight": 0.6},
            {"factor": "ret_20d", "weight": 0.4},
        ],
    })
    return DSLStrategy(spec, top_n=3)


# ── 分项列加和 ≈ 总分 ────────────────────────────────────────────────────────

def test_multifactor_exposes_score_detail() -> None:
    strat = _make_multifactor()
    signals = strat.generate_signals(_ctx(_features()))
    assert signals.height == 3  # fixture sanity: top_n 生效

    detail = strat.last_score_detail
    assert isinstance(detail, pl.DataFrame)
    assert detail.height > 0
    assert detail.columns[:3] == ["asset_id", "score", "rank"]
    assert "_w_momentum_60d" in detail.columns
    assert "_w_ret_20d" in detail.columns

    part_cols = [c for c in detail.columns if c.startswith("_w_")]
    sums = detail.select(pl.sum_horizontal(part_cols).alias("parts"))["parts"]
    assert (detail["score"] - sums).abs().max() < 1e-9

    # rank 按总分降序、从 1 开始
    assert detail["rank"].to_list() == list(range(1, detail.height + 1))
    assert detail["score"].is_sorted(descending=True)


def test_dsl_exposes_score_detail() -> None:
    strat = _make_dsl()
    signals = strat.generate_signals(_ctx(_features()))
    assert signals.height == 3

    detail = strat.last_score_detail
    assert isinstance(detail, pl.DataFrame)
    assert detail.height > 0
    assert detail.columns[:3] == ["asset_id", "score", "rank"]
    assert "_w_momentum_60d" in detail.columns
    assert "_w_ret_20d" in detail.columns

    part_cols = [c for c in detail.columns if c.startswith("_w_")]
    sums = detail.select(pl.sum_horizontal(part_cols).alias("parts"))["parts"]
    assert (detail["score"] - sums).abs().max() < 1e-9

    assert detail["rank"].to_list() == list(range(1, detail.height + 1))
    assert detail["score"].is_sorted(descending=True)


def test_detail_shape_consistent_across_strategies() -> None:
    """两策略 detail 列形态一致：前三列 asset_id/score/rank，分项列 `_w_{factor}`。"""
    mf = _make_multifactor()
    dsl = _make_dsl()
    mf.generate_signals(_ctx(_features()))
    dsl.generate_signals(_ctx(_features()))
    assert mf.last_score_detail is not None
    assert dsl.last_score_detail is not None
    assert mf.last_score_detail.columns[:3] == dsl.last_score_detail.columns[:3]
    assert all(c.startswith("_w_") for c in mf.last_score_detail.columns[3:])
    assert all(c.startswith("_w_") for c in dsl.last_score_detail.columns[3:])


# ── ABC 零改动 / 无能力策略 ──────────────────────────────────────────────────

def test_strategy_without_capability_returns_none() -> None:
    from cquant.backtest_vector.run import StaticTopNStrategy

    strat = StaticTopNStrategy("static_test", top_n=3, sort_factor="ret_20d")
    strat.generate_signals(_ctx(_features()))
    assert getattr(strat, "last_score_detail", None) is None
    assert getattr(strat, "missing_factors", None) is None


# ── 生命周期：每次调用重置 ───────────────────────────────────────────────────

def test_score_detail_reset_per_call() -> None:
    strat = _make_multifactor()
    strat.generate_signals(_ctx(_features()))
    assert strat.last_score_detail is not None

    # 第二次调用：空数据早退 → detail 重置为 None（无残留）
    empty_ctx = StrategyContext(as_of_date=_DAY, universe_id="u", features=None)
    strat.generate_signals(empty_ctx)
    assert strat.last_score_detail is None

    # 第三次调用：中途抛错 → detail 同样保持 None（开头先置后算）
    strat.generate_signals(_ctx(_features()))
    assert strat.last_score_detail is not None

    # monkeypatch 打分步骤抛异常
    orig = MultiFactorStrategy._handle_missing_factors
    def boom(self, df, weights):
        raise RuntimeError("boom")
    MultiFactorStrategy._handle_missing_factors = boom
    try:
        with pytest.raises(RuntimeError):
            strat.generate_signals(_ctx(_features()))
    finally:
        MultiFactorStrategy._handle_missing_factors = orig
    assert strat.last_score_detail is None


# ── missing_factors 累计 ─────────────────────────────────────────────────────

def test_missing_factors_accumulated() -> None:
    # DSL：因子 nope_factor 未物化（不在特征宽表列中）
    spec = StrategyDSL.from_dict({
        "name": "missing_dsl",
        "score": [
            {"factor": "momentum_60d", "weight": 0.5},
            {"factor": "nope_factor", "weight": 0.5},
        ],
    })
    strat = DSLStrategy(spec)
    strat.generate_signals(_ctx(_features()))
    assert "nope_factor" in strat.missing_factors
    assert "momentum_60d" not in strat.missing_factors

    # 二次调用不重复累积
    strat.generate_signals(_ctx(_features()))
    assert strat.missing_factors.count("nope_factor") == 1

    # MultiFactor fill_0：缺失因子列被警告并补 0
    mf = MultiFactorStrategy(
        strategy_id="mf_missing",
        factor_weights={"momentum_60d": 0.5, "ghost_factor": 0.5},
        top_n=2,
    )
    mf.generate_signals(_ctx(_features()))
    assert "ghost_factor" in mf.missing_factors
    mf.generate_signals(_ctx(_features()))
    assert mf.missing_factors.count("ghost_factor") == 1
