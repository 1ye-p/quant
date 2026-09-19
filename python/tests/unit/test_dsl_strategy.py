"""T1+T2：StrategyDSL schema 双侧校验 + DSLStrategy 无 regime 端到端。

T1 — pydantic schema：roundtrip（dict/YAML）、中文错误 + 路径、regime 三模式互斥。
T2 — DSLStrategy：score（CrossSectionScorer 复用）→ SignalFrame →
     VectorBacktestEngine（sizer/policy 映射）→ NAV/fills；
     同构：BacktestRunner 标准持久化路径接受 strategy_type="DSL"，
     报告 artifact（runs/fills/signals）与内置策略同一生成管线。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import types

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.backtest_vector.strategy import StrategyContext
from cquant.datahub.catalog import Catalog
from cquant.riskguard.policies.stop_loss import FixedStopLossPolicy
from cquant.riskguard.sizers.equal_weight import EqualWeightSizer
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import (
    StrategyDSL,
    ValidationContext,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── 合成数据 ──────────────────────────────────────────────────────────────────

def _synthetic_prices(n_assets: int = 6, n_days: int = 40) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    assets = [f"SSE:{600000 + i}" for i in range(n_assets)]
    rows = []
    p = {a: 50.0 for a in assets}
    d = date(2025, 1, 2)
    for i in range(n_days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        for a in assets:
            p[a] *= 1 + rng.normal(0.0008, 0.012)
            rows.append({
                "asset_id": a, "trade_date": d,
                "open": p[a], "high": p[a] * 1.01, "low": p[a] * 0.99,
                "close": p[a], "volume": 1e6, "amount": p[a] * 1e6,
                "is_suspended": False,
            })
        d += timedelta(days=1)
    return pl.DataFrame(rows).sort("trade_date", "asset_id")


def _synthetic_features(prices: pl.DataFrame) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    assets = prices["asset_id"].unique().to_list()
    days = prices["trade_date"].unique().to_list()
    rows = []
    for dt in days:
        for a in assets:
            rows.append({
                "asset_id": a, "trade_date": dt,
                "momentum_60d": rng.normal(0.02, 0.05),
                "my_low_vol": rng.normal(-0.01, 0.02),
            })
    return pl.DataFrame(rows).sort("trade_date", "asset_id")


_DSL_DICT: dict = {
    "name": "my_low_vol_rotation",
    "universe": "all",
    "frequency": "daily",
    "score": [
        {"factor": "momentum_60d", "weight": 0.4},
        {"factor": "custom:my_low_vol", "weight": 0.6},
    ],
    "position": {"method": "equal_weight"},
    "risk": [{"type": "fixed_stop_loss", "params": {"stop_pct": -0.05}}],
    "benchmark": "",
    "regime": None,
}

_VCTX = ValidationContext(
    known_factors={"momentum_60d"},
    custom_factors={"my_low_vol"},
)


# ── T1: schema ────────────────────────────────────────────────────────────────

class TestSchemaRoundtrip:
    def test_dict_roundtrip(self) -> None:
        spec = StrategyDSL.from_dict(_DSL_DICT, context=_VCTX)
        assert spec.name == "my_low_vol_rotation"
        assert spec.score[1].resolved_name == "my_low_vol"
        dumped = spec.model_dump()
        again = StrategyDSL.from_dict(dumped, context=_VCTX)
        assert again == spec

    def test_yaml_roundtrip(self) -> None:
        spec = StrategyDSL.from_dict(_DSL_DICT, context=_VCTX)
        text = spec.to_yaml()
        assert "custom:my_low_vol" in text
        again = StrategyDSL.from_yaml(text, context=_VCTX)
        assert again == spec

    def test_minimal_dict(self) -> None:
        spec = StrategyDSL.from_dict({"name": "s", "score": [{"factor": "ret_20d", "weight": 1.0}]})
        assert spec.position.method == "equal_weight"
        assert spec.risk == []
        assert spec.regime is None


class TestSchemaValidation:
    def test_zero_weight_rejected(self) -> None:
        with pytest.raises(ValidationError, match="权重不能为 0"):
            StrategyDSL.from_dict({
                "name": "s", "score": [{"factor": "ret_20d", "weight": 0.0}],
            })

    def test_all_zero_weights_rejected(self) -> None:
        # 单项校验先触发（权重不能为 0）；模型级「全零」兜底与之互补
        with pytest.raises(ValidationError, match="权重不能为 0"):
            StrategyDSL.from_dict({
                "name": "s",
                "score": [{"factor": "a", "weight": 0.0}, {"factor": "b", "weight": 0.0}],
            }, context=ValidationContext(known_factors={"a", "b"}))

    def test_unknown_factor_rejected_with_context(self) -> None:
        with pytest.raises(ValidationError, match="不存在"):
            StrategyDSL.from_dict({
                "name": "s", "score": [{"factor": "nope", "weight": 1.0}],
            }, context=_VCTX)

    def test_unknown_custom_factor_rejected(self) -> None:
        with pytest.raises(ValidationError, match="自定义因子"):
            StrategyDSL.from_dict({
                "name": "s", "score": [{"factor": "custom:ghost", "weight": 1.0}],
            }, context=_VCTX)

    def test_no_context_skips_existence(self) -> None:
        spec = StrategyDSL.from_dict({
            "name": "s", "score": [{"factor": "anything", "weight": 1.0}],
        })
        assert spec.score[0].factor == "anything"

    def test_unknown_risk_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StrategyDSL.from_dict({
                "name": "s",
                "score": [{"factor": "a", "weight": 1.0}],
                "risk": [{"type": "not_a_policy"}],
            })

    def test_unknown_sizer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="未知的仓位方法"):
            StrategyDSL.from_dict({
                "name": "s",
                "score": [{"factor": "a", "weight": 1.0}],
                "position": {"method": "magic"},
            })

    def test_field_path_in_error(self) -> None:
        with pytest.raises(ValidationError) as ei:
            StrategyDSL.from_dict({
                "name": "s", "score": [{"factor": "a", "weight": 0.0}],
            })
        assert "score" in str(ei.value.errors()[0]["loc"])


class TestRegimeValidation:
    def _base(self, **regime: object) -> dict:
        return {
            "name": "s",
            "score": [{"factor": "a", "weight": 1.0}],
            "regime": regime,
        }

    def test_threshold_ok(self) -> None:
        spec = StrategyDSL.from_dict(self._base(
            mode="threshold",
            indicators={"breadth": "ext.market_breadth_20d"},
            rules=[
                {"when": "breadth < 0.2", "position_scale": 0.0},
                {"position_scale": 1.0},
            ],
        ))
        assert spec.regime is not None and spec.regime.mode == "threshold"

    def test_threshold_requires_rules_and_single_default(self) -> None:
        with pytest.raises(ValidationError, match="必须提供 rules"):
            StrategyDSL.from_dict(self._base(mode="threshold"))
        with pytest.raises(ValidationError, match="恰好包含一条 default"):
            StrategyDSL.from_dict(self._base(
                mode="threshold",
                rules=[{"when": "breadth < 0.2", "position_scale": 0.0}],
            ))

    def test_switch_ok(self) -> None:
        spec = StrategyDSL.from_dict(self._base(
            mode="switch",
            indicators={"active_cap": "ext.compass_active_cap"},
            initial="neutral",
            reevaluate="daily",
            states=[
                {"name": "risk_on", "enter_when": "pct_change(active_cap, 1) >= 0.02", "position_scale": 1.0},
                {"name": "risk_off", "enter_when": "pct_change(active_cap, 1) <= -0.01", "position_scale": 0.0},
                {"name": "neutral", "enter_when": "abs(pct_change(active_cap, 1)) < 0.005", "position_scale": 0.5},
            ],
        ))
        assert spec.regime is not None and len(spec.regime.states) == 3

    def test_switch_requires_initial_in_states(self) -> None:
        with pytest.raises(ValidationError, match="initial"):
            StrategyDSL.from_dict(self._base(
                mode="switch",
                states=[{"name": "risk_on", "enter_when": "a > 1", "position_scale": 1.0}],
            ))
        with pytest.raises(ValidationError, match="不在 states 中"):
            StrategyDSL.from_dict(self._base(
                mode="switch",
                initial="ghost",
                states=[{"name": "risk_on", "enter_when": "a > 1", "position_scale": 1.0}],
            ))

    def test_continuous_requires_scale_expr(self) -> None:
        with pytest.raises(ValidationError, match="scale_expr"):
            StrategyDSL.from_dict(self._base(mode="continuous"))
        spec = StrategyDSL.from_dict(self._base(
            mode="continuous", scale_expr="clip(breadth, 0.2, 0.5)",
        ))
        assert spec.regime is not None

    def test_bad_expression_syntax_rejected(self) -> None:
        with pytest.raises(ValidationError, match="表达式语法错误"):
            StrategyDSL.from_dict(self._base(
                mode="continuous", scale_expr="breadth <<<< 0.5",
            ))


# ── T2: DSLStrategy + 引擎端到端 ─────────────────────────────────────────────

class TestDSLStrategyExecutor:
    def _strategy(self) -> DSLStrategy:
        spec = StrategyDSL.from_dict(_DSL_DICT, context=_VCTX)
        return DSLStrategy(
            spec=spec,
            factor_registry={"my_low_vol": object()},
            top_n=3,
        )

    def test_implements_strategy_abc(self) -> None:
        from cquant.backtest_vector.strategy import Strategy
        strat = self._strategy()
        assert isinstance(strat, Strategy)
        assert strat.strategy_id == "dsl_my_low_vol_rotation"
        strat.fit({"prices": None})  # no-op

    def test_generate_signals_shape(self) -> None:
        prices = _synthetic_prices()
        features = _synthetic_features(prices)
        strat = self._strategy()
        as_of = features["trade_date"].unique().sort()[5]
        ctx = StrategyContext(
            as_of_date=as_of, universe_id="all",
            features=features.filter(pl.col("trade_date") <= as_of),
        )
        signals = strat.generate_signals(ctx)
        assert set(signals.columns) == {"asset_id", "signal_date", "direction", "strength", "confidence"}
        assert signals.height == 3
        assert (signals["signal_date"] == as_of).all()
        assert (signals["direction"] == "long").all()

    def test_sizer_and_policy_mapping(self) -> None:
        strat = self._strategy()
        sizer = strat.build_sizer()
        assert isinstance(sizer, EqualWeightSizer)
        policies = strat.build_risk_policies()
        assert len(policies) == 1
        assert isinstance(policies[0], FixedStopLossPolicy)

    def test_missing_custom_factor_in_registry_rejected(self) -> None:
        spec = StrategyDSL.from_dict(_DSL_DICT, context=_VCTX)
        with pytest.raises(ValueError, match="factor_registry"):
            DSLStrategy(spec=spec, factor_registry={"other": object()})

    def test_engine_end_to_end(self) -> None:
        prices = _synthetic_prices()
        features = _synthetic_features(prices)
        strat = self._strategy()
        spec = BacktestSpec(
            strategy=strat,
            prices=prices,
            start_date=prices["trade_date"].min(),
            end_date=prices["trade_date"].max(),
            initial_cash=Decimal("1_000_000"),
            features=features,
            sizer=strat.build_sizer(),
            risk_policies=strat.build_risk_policies(),
        )
        result = VectorBacktestEngine().run(spec)
        assert result.error is None
        assert result.metrics.total_trades > 0
        assert not result.fills.is_empty()
        assert result.portfolio_returns is not None and not result.portfolio_returns.is_empty()


# ── T2 同构：BacktestRunner 标准持久化路径 ────────────────────────────────────

@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "dsl_test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    prices = _synthetic_prices(n_assets=6, n_days=20)
    features = _synthetic_features(prices)
    conn = cat._get_conn()
    conn.register("_p", prices.with_columns(pl.lit("test").alias("source")).to_arrow())
    conn.execute(
        "INSERT OR REPLACE INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, amount, is_suspended, source) "
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount, is_suspended, source FROM _p"
    )
    conn.unregister("_p")

    long_feats = features.unpivot(
        index=["asset_id", "trade_date"], on=["momentum_60d", "my_low_vol"],
        variable_name="factor_name", value_name="value",
    ).with_columns(pl.lit("fsv_dsl").alias("feature_set_version"))
    conn.register("_f", long_feats.to_arrow())
    conn.execute(
        "INSERT OR REPLACE INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _f"
    )
    conn.unregister("_f")
    return cat, prices


class TestRunnerIsomorphism:
    def test_runner_accepts_dsl_strategy_type(self) -> None:
        """_build_strategy 不因 DSL 类型缺失分支；产出 DSLStrategy。"""
        runner = object.__new__(BacktestRunner)
        runner._catalog = None
        spec = BacktestRunSpec(
            dataset_version="v1", strategy_id="dsl_run",
            strategy_type="DSL", dsl_spec=_DSL_DICT,
            start_date=date(2025, 1, 2), end_date=date(2025, 3, 31),
        )
        strategy = BacktestRunner._build_strategy(runner, spec)
        assert isinstance(strategy, DSLStrategy)
        assert strategy.strategy_id == "dsl_my_low_vol_rotation"

    def test_runner_dsl_requires_dsl_spec(self) -> None:
        runner = object.__new__(BacktestRunner)
        runner._catalog = None
        spec = BacktestRunSpec(
            dataset_version="v1", strategy_id="dsl_run",
            strategy_type="DSL",
            start_date=date(2025, 1, 2), end_date=date(2025, 3, 31),
        )
        with pytest.raises(ValueError, match="dsl_spec"):
            BacktestRunner._build_strategy(runner, spec)

    def test_full_persistence_path(self, catalog) -> None:
        """DSL 策略走标准 BacktestRunner：runs/fills/signals 同一生成管线。"""
        cat, prices = catalog
        strategy = DSLStrategy(
            spec=StrategyDSL.from_dict(_DSL_DICT, context=_VCTX),
            factor_registry={"my_low_vol": object()},
            top_n=3,
        )
        runner = BacktestRunner(cat)
        run_id = runner.run(BacktestRunSpec(
            dataset_version="v1",
            strategy_id="dsl_my_low_vol_rotation",
            strategy_type="DSL",
            dsl_spec=_DSL_DICT,
            feature_set_version="fsv_dsl",
            start_date=prices["trade_date"].min(),
            end_date=prices["trade_date"].max(),
            initial_cash=Decimal("1_000_000"),
            risk_policies=strategy.build_risk_policies(),
        ))
        assert run_id

        runs = cat.query(
            "SELECT run_id, strategy_id, status FROM gold_backtest_runs WHERE run_id = ?",
            [run_id],
        )
        assert not runs.is_empty()
        assert runs["strategy_id"][0] == "dsl_my_low_vol_rotation"
        assert runs["status"][0] == "completed"

        fills = cat.query(
            "SELECT * FROM gold_fills WHERE run_id = ?", [run_id]
        )
        assert not fills.is_empty()

        signals = cat.query(
            "SELECT * FROM gold_signals WHERE signal_set_version = ?", [run_id]
        )
        assert not signals.is_empty()


# ── T3: runner DSL 分支加固（context 注入 / sizer+risk 挂载 / registry） ─────

def _insert_custom_factor(cat, name: str = "my_low_vol") -> None:
    from cquant.factorlab.custom_factor_loader import load_custom_factors
    load_custom_factors(cat)  # 副作用：CREATE TABLE IF NOT EXISTS meta_custom_factors
    cat.execute(
        "DELETE FROM meta_custom_factors WHERE factor_id = ?", [f"cf_{name}"]
    )
    cat.execute(
        "INSERT INTO meta_custom_factors (factor_id, name, expression, description) "
        "VALUES (?, ?, ?, '')",
        [f"cf_{name}", name, "rank(-std(ret_20d, 20))"],
    )


class TestRunnerDslHardening:
    def test_runner_rejects_unknown_factor(self, catalog) -> None:
        """拼错因子 → 构造期 ValueError（中文报错），不再静默降级。"""
        cat, _ = catalog
        runner = BacktestRunner(cat)
        bad = dict(_DSL_DICT)
        bad["score"] = [{"factor": "momentum_60x", "weight": 1.0}]
        spec = BacktestRunSpec(
            dataset_version="v1", strategy_id="dsl_run",
            strategy_type="DSL", dsl_spec=bad,
            start_date=date(2025, 1, 2), end_date=date(2025, 3, 31),
        )
        with pytest.raises(ValueError, match="不存在"):
            runner._build_strategy(spec)

    def test_runner_mounts_sizer_and_risk(self, catalog) -> None:
        """DSL 分支：sizer 自动挂载、risk_policies 默认合并到 BacktestSpec。"""
        cat, prices = catalog
        _insert_custom_factor(cat)
        dsl = dict(_DSL_DICT)
        dsl["position"] = {"method": "kelly"}

        captured: dict = {}
        real_run = BacktestRunner(cat)._engine.run

        def spy(bt_spec):
            captured["spec"] = bt_spec
            return real_run(bt_spec)

        runner = BacktestRunner(cat)
        runner._engine = types.SimpleNamespace(run=spy)
        run_id = runner.run(BacktestRunSpec(
            dataset_version="v1",
            strategy_id="dsl_my_low_vol_rotation",
            strategy_type="DSL",
            dsl_spec=dsl,
            feature_set_version="fsv_dsl",
            start_date=prices["trade_date"].min(),
            end_date=prices["trade_date"].max(),
            initial_cash=Decimal("1_000_000"),
        ))
        assert run_id
        bt_spec = captured["spec"]
        from cquant.riskguard.sizers.kelly import KellySizer
        assert bt_spec.sizer is not None
        assert isinstance(bt_spec.sizer, KellySizer)
        assert bt_spec.risk_policies
        assert all(isinstance(p, FixedStopLossPolicy) for p in bt_spec.risk_policies)

    def test_runner_custom_factor_checked(self, catalog) -> None:
        """custom:not_exist → 构造期报错（registry 注入后检查生效）。"""
        cat, _ = catalog
        _insert_custom_factor(cat)  # registry 非空 → custom: 存在性检查激活
        runner = BacktestRunner(cat)
        bad = dict(_DSL_DICT)
        bad["score"] = [{"factor": "custom:not_exist", "weight": 1.0}]
        spec = BacktestRunSpec(
            dataset_version="v1", strategy_id="dsl_run",
            strategy_type="DSL", dsl_spec=bad,
            start_date=date(2025, 1, 2), end_date=date(2025, 3, 31),
        )
        with pytest.raises(ValueError, match="自定义因子"):
            runner._build_strategy(spec)
