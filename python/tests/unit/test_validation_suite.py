"""Phase 4 T1：一键验证套件后端（validation-suite）。

覆盖：
1. 有产物 run → 套件 job completed → checklist 各字段非 None
   （非 regime 策略 regime_cycles_sufficient=None）
2. DSL 策略 → 敏感性空间含各因子权重 ±10% 扫描点 + WF 结果 weights_refit=false（D7）
3. 单步失败（monkeypatch WalkForwardRefit 抛错）→ 套件不炸，该项 failed+原因可见
4. GET /{run_id}/validation-suite 返回落库结果
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from starlette.background import BackgroundTasks

from cquant.api_server.routes.backtests import (
    get_job_status,
    get_validation_suite,
    run_validation_suite,
)
from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.datahub.catalog import Catalog
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import StrategyDSL, ValidationContext

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── 合成数据（与 test_analysis_pipeline 同构） ────────────────────────────────

def _synthetic_prices(n_assets: int = 6, n_days: int = 20) -> pl.DataFrame:
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

# 套件通过 run tags 找回 dsl_spec（gold_backtest_runs 不存 dsl_spec 本体）
_RUN_TAGS: dict = {
    "dsl_spec": _DSL_DICT,
    "strategy_type": "DSL",
    "top_n": 3,
    "sort_factor": "momentum_60d",
}


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "validation_suite_test.duckdb", repo_root=_REPO_ROOT)
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


@pytest.fixture()
def filled_run(catalog):
    """跑一个真实 DSL 回测：产生 snapshots + fills + tags(含 dsl_spec) 的 run。"""
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
        tags=_RUN_TAGS,
        risk_policies=strategy.build_risk_policies(),
    ))
    return cat, run_id


def _run_suite_endpoint(cat, run_id) -> dict:
    """POST validation-suite + 同步执行 BackgroundTasks，返回响应。"""
    async def _driver():
        bt = BackgroundTasks()
        resp = await run_validation_suite(run_id, bt, cat, None)
        for task in bt.tasks:
            await task.func(*task.args, **task.kwargs)
        return resp

    return asyncio.run(_driver())


def _get_suite(cat, run_id) -> dict:
    return asyncio.run(get_validation_suite(run_id, cat))


def _steps_by_name(suite: dict) -> dict[str, dict]:
    return {s["step"]: s for s in suite["steps"]}


# ── 1. 套件完成 + checklist 聚合 ─────────────────────────────────────────────

class TestValidationSuiteEndToEnd:
    def test_suite_completes_and_checklist_filled(self, filled_run) -> None:
        cat, run_id = filled_run
        resp = _run_suite_endpoint(cat, run_id)
        assert resp["status"] == "running"

        job = asyncio.run(get_job_status(resp["job_id"], cat))
        assert job["status"] == "completed", f"job error: {job.get('error')}"

        suite = _get_suite(cat, run_id)
        ck = suite["checklist"]
        # 非 regime 策略：regime_cycles_sufficient 为 None，其余字段有值
        assert isinstance(ck["psr_pass"], bool)
        assert isinstance(ck["fold_stable"], bool)
        assert isinstance(ck["sensitivity_flat"], bool)
        assert ck["regime_cycles_sufficient"] is None
        assert suite["psr"] is not None and suite["dsr"] is not None
        # 成本假设：默认值醒目展示
        assert ck["cost_assumptions"]["defaults_used"] is True
        assert "note" in ck["cost_assumptions"]
        assert "commission_rate" in ck["cost_assumptions"]["defaults"]
        # 阈值常量随结果透出
        assert ck["thresholds"]["psr_pass"] == 0.95

    def test_missing_run_404(self, catalog) -> None:
        cat, _ = catalog
        with pytest.raises(Exception) as exc_info:
            _run_suite_endpoint(cat, "run_does_not_exist")
        assert exc_info.value.status_code == 404


# ── 2. DSL 参数映射 + D7 标注 ────────────────────────────────────────────────

class TestDslParamMapping:
    def test_sensitivity_space_contains_weight_perturbations(self, filled_run) -> None:
        """敏感性空间含各因子权重 ±10% 扫描点。"""
        cat, run_id = filled_run
        _run_suite_endpoint(cat, run_id)
        suite = _get_suite(cat, run_id)
        sens = _steps_by_name(suite)["sensitivity"]
        assert sens["status"] == "completed"
        assert sens["mode"] == "dsl"
        space = sens["param_space"]
        assert space["score_weight:momentum_60d"] == [0.36, 0.44]  # 0.4 ±10%
        assert space["score_weight:custom:my_low_vol"] == [0.54, 0.66]  # 0.6 ±10%
        assert sens["n_combinations"] == 4  # 2 factors × 2 values ≤ 50

    def test_walk_forward_annotates_weights_refit_false(self, filled_run) -> None:
        """D7：DSL 的 WF 结果附 weights_refit=false + 中文说明。"""
        cat, run_id = filled_run
        _run_suite_endpoint(cat, run_id)
        suite = _get_suite(cat, run_id)
        wf = _steps_by_name(suite)["walk_forward"]
        assert wf["status"] == "completed"
        assert wf["weights_refit"] is False
        assert "权重未重估" in wf["weights_refit_note"]
        assert len(wf["fold_sharpes"]) > 0


# ── 3. 单步失败隔离 ──────────────────────────────────────────────────────────

class TestStepFailureIsolation:
    def test_walk_forward_failure_does_not_break_suite(self, filled_run, monkeypatch) -> None:
        """monkeypatch WF 抛错 → 套件 job 仍 completed，该项 failed+原因可见。"""
        from cquant.bt_analyzer.walk_forward_refit import WalkForwardRefit

        def _boom(*args, **kwargs):
            raise RuntimeError("wf-exploded")

        monkeypatch.setattr(WalkForwardRefit, "from_backtest_result", _boom)

        cat, run_id = filled_run
        resp = _run_suite_endpoint(cat, run_id)
        job = asyncio.run(get_job_status(resp["job_id"], cat))
        assert job["status"] == "completed", f"套件不应因单步失败而炸: {job.get('error')}"

        suite = _get_suite(cat, run_id)
        steps = _steps_by_name(suite)
        wf = steps["walk_forward"]
        assert wf["status"] == "failed"
        assert "wf-exploded" in wf["error"]
        # 其余步骤不受影响
        assert steps["psr_dsr"]["status"] == "completed"
        assert steps["sensitivity"]["status"] == "completed"
        # WF 失败 → fold_stable 退化为 None（三态清单可区分）
        assert suite["checklist"]["fold_stable"] is None


# ── 4. GET 端点 ──────────────────────────────────────────────────────────────

class TestGetValidationSuite:
    def test_get_returns_latest_persisted_result(self, filled_run) -> None:
        cat, run_id = filled_run
        _run_suite_endpoint(cat, run_id)
        _run_suite_endpoint(cat, run_id)  # 第二次执行 → 取最近
        suite = _get_suite(cat, run_id)
        assert suite["run_id"] == run_id
        assert suite["suite_id"]
        assert suite["created_at"]
        assert {s["step"] for s in suite["steps"]} >= {
            "psr_dsr", "walk_forward", "sensitivity", "regime_cycles",
        }

    def test_get_404_when_never_run(self, filled_run) -> None:
        cat, run_id = filled_run
        with pytest.raises(Exception) as exc_info:
            _get_suite(cat, run_id)
        assert exc_info.value.status_code == 404
