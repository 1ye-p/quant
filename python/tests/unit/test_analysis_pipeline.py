"""Phase 3.5 T1/T4：过拟合分析链路 + export 端点。

T1 — /analyze 签名修复：
     * bt_analyzer.load_result 从持久化产物重建 BacktestResult
       （snapshots → portfolio_returns、gold_fills → fills、metrics_uri → metrics）
     * trigger_analysis 传 (result, spec) 而非把 AnalysisRunSpec 当 result
     * 分析异常进入 job failed + error 字段（不再静默）
T4 — export：html 200 可下载；pdf 在依赖缺失时明确 501（不裸 500）。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks

from cquant.api_server.routes.backtests import (
    export_backtest_report,
    get_backtest_analysis,
    get_job_status,
    trigger_analysis,
)
from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.bt_analyzer.run import AnalysisRunner, AnalysisRunSpec, load_result
from cquant.datahub.catalog import Catalog
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import StrategyDSL, ValidationContext

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── 合成数据（与 test_fills_persistence 同构） ────────────────────────────────

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


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "analysis_test.duckdb", repo_root=_REPO_ROOT)
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


def _run_backtest(cat: Catalog, prices: pl.DataFrame) -> str:
    strategy = DSLStrategy(
        spec=StrategyDSL.from_dict(_DSL_DICT, context=_VCTX),
        factor_registry={"my_low_vol": object()},
        top_n=3,
    )
    runner = BacktestRunner(cat)
    return runner.run(BacktestRunSpec(
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


@pytest.fixture()
def filled_run(catalog):
    """跑一个真实回测：产生 snapshots + fills 的 run。"""
    cat, prices = catalog
    run_id = _run_backtest(cat, prices)
    return cat, run_id


def _call_endpoint(coro_with_bt):
    """Execute endpoint + its BackgroundTasks synchronously (test driver)."""
    async def _driver():
        bt = BackgroundTasks()
        # endpoints take (..., background_tasks, catalog, ...) — inject bt
        result = await coro_with_bt(bt)
        for task in bt.tasks:
            await task.func(*task.args, **task.kwargs)
        return result
    return asyncio.run(_driver())


# ── T1: load_result 重建 ──────────────────────────────────────────────────────

class TestLoadResult:
    def test_reconstructs_from_artifacts(self, filled_run) -> None:
        cat, run_id = filled_run
        result = load_result(run_id, cat)
        assert result.run_id == run_id
        assert not result.portfolio_returns.is_empty()
        assert {"trade_date", "portfolio_return"} <= set(result.portfolio_returns.columns)
        assert not result.fills.is_empty()
        assert isinstance(result.metrics.sharpe_ratio, float)

    def test_missing_artifacts_raise_clear_error(self, catalog) -> None:
        cat, _ = catalog
        cat.execute(
            "INSERT INTO gold_backtest_runs "
            "(run_id, engine, strategy_id, dataset_version, signal_set_version, "
            " cost_model_config, started_at, completed_at, status, metrics_uri, tags) "
            "VALUES ('run_no_snaps', 'vector', 's1', 'v1', '', '{}', now(), now(), "
            "        'completed', '', '[]')"
        )
        with pytest.raises(ValueError, match="No portfolio snapshots"):
            load_result("run_no_snaps", cat)

    def test_runner_accepts_reconstructed_result(self, filled_run) -> None:
        """runner.run(result, spec) 签名端到端可用并产出 PSR/DSR。"""
        cat, run_id = filled_run
        report = AnalysisRunner(cat).run(
            load_result(run_id, cat),
            AnalysisRunSpec(backtest_run_id=run_id),
        )
        assert report.backtest_run_id == run_id
        assert report.psr is not None
        assert report.dsr is not None
        row = cat.query(
            "SELECT psr, dsr FROM gold_bt_analysis_runs WHERE backtest_run_id = ?",
            [run_id],
        )
        assert not row.is_empty()
        assert row["psr"][0] is not None and row["dsr"][0] is not None


# ── T1: /analyze 端点（成功 + 失败透出） ─────────────────────────────────────

class TestAnalyzeEndpoint:
    def test_analyze_persists_and_returns_analysis(self, filled_run) -> None:
        """OverfittingTab 数据源验证：/analysis 返回 psr/dsr 非空。"""
        cat, run_id = filled_run

        def _endpoint(bt: BackgroundTasks):
            return trigger_analysis(run_id, bt, cat, None)

        resp = _call_endpoint(_endpoint)
        assert resp["status"] == "running"

        job = asyncio.run(get_job_status(resp["job_id"], cat))
        assert job["status"] == "completed", f"job error: {job.get('error')}"
        assert job["run_id"]  # analysis_run_id recorded

        # /analysis 端点不再 404 "No analysis found"
        analysis = asyncio.run(get_backtest_analysis(run_id, cat))
        assert analysis["psr"] is not None
        assert analysis["dsr"] is not None
        assert analysis["overall_overfit_score"] is not None

    def test_analyze_failure_surfaces_in_job(self, catalog) -> None:
        """缺产物的 run → job failed + error 含原因（不再静默）。"""
        cat, _ = catalog
        cat.execute(
            "INSERT INTO gold_backtest_runs "
            "(run_id, engine, strategy_id, dataset_version, signal_set_version, "
            " cost_model_config, started_at, completed_at, status, metrics_uri, tags) "
            "VALUES ('run_broken', 'vector', 's1', 'v1', '', '{}', now(), now(), "
            "        'completed', '', '[]')"
        )

        def _endpoint(bt: BackgroundTasks):
            return trigger_analysis("run_broken", bt, cat, None)

        resp = _call_endpoint(_endpoint)
        job = asyncio.run(get_job_status(resp["job_id"], cat))
        assert job["status"] == "failed"
        assert job["error"]
        assert "No portfolio snapshots" in job["error"]


# ── T4: export html/pdf ───────────────────────────────────────────────────────

class TestExportEndpoints:
    def test_export_html_200_downloadable(self, filled_run) -> None:
        cat, run_id = filled_run
        response = asyncio.run(export_backtest_report(run_id, cat, format="html"))
        assert response.status_code == 200
        assert "text/html" in response.media_type
        assert "attachment" in response.headers.get("content-disposition", "")

    def test_export_pdf_explicit_behavior(self, filled_run) -> None:
        """pdf：200（依赖可用）或 501 + 明确说明（依赖缺失）——不裸 500。"""
        cat, run_id = filled_run
        try:
            response = asyncio.run(export_backtest_report(run_id, cat, format="pdf"))
            assert response.status_code == 200
            assert response.media_type == "application/pdf"
        except HTTPException as exc:
            assert exc.status_code == 501
            assert "weasyprint" in str(exc.detail) or "PDF" in str(exc.detail)
