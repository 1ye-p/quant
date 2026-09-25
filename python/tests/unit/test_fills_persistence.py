"""Phase 3.5 T2/T3/T5：fills 持久化透出 + tca/stress 端点 + qty 列名。

T2 — BacktestRunner._persist_fills 失败不再静默：
     error 日志（含 run_id）+ gold_backtest_runs.tags 盖 fills_persisted=false 戳。
T3 — /tca 与 /stress-test 对有 fills 的 run 返回 200 且关键字段非空
     （gold_bt_tca 表 DDL 此前缺失，已补入 analysis.sql）。
T5 — run-report 导出的最近 20 笔交易使用 qty 列（gold_fills 真实列名）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from cquant.api_server.routes.backtests import (
    export_backtest_report,
    get_backtest_tca,
    get_stress_test,
)
from cquant.backtest_vector.run import BacktestRunner, BacktestRunSpec
from cquant.datahub.catalog import Catalog
from cquant.strategy_dsl.executor import DSLStrategy
from cquant.strategy_dsl.schema import StrategyDSL, ValidationContext

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── 合成数据（与 test_dsl_strategy 同构） ────────────────────────────────────

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
    cat = Catalog(db_path=tmp_path / "fills_test.duckdb", repo_root=_REPO_ROOT)
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
    """跑一个真实回测：产生 fills + snapshots 的 run。"""
    cat, prices = catalog
    run_id = _run_backtest(cat, prices)
    return cat, run_id


# ── T2: fills 持久化 ─────────────────────────────────────────────────────────

class TestFillsPersistence:
    def test_normal_path_fills_match_metrics(self, filled_run) -> None:
        """正常路径：gold_fills 行数 = metrics.total_trades（= total_fills）。"""
        cat, run_id = filled_run
        fills = cat.query("SELECT * FROM gold_fills WHERE run_id = ?", [run_id])
        assert not fills.is_empty()

        metrics = BacktestRunner(cat).get_run_metrics(run_id)
        assert metrics is not None
        assert fills.height == metrics["total_trades"]

        # 成功路径不盖失败戳
        tags = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
        )["tags"][0]
        if tags:
            parsed = json.loads(tags) if isinstance(tags, str) else tags
            assert parsed.get("fills_persisted") is not False

    def test_failure_path_surfaces_in_run_metadata(
        self, catalog, monkeypatch, caplog
    ) -> None:
        """失败路径：upsert gold_fills 抛异常 → error 日志 + tags 盖戳，不再静默。"""
        cat, prices = catalog
        original_upsert = cat.upsert

        def exploding_upsert(table, *args, **kwargs):
            if table == "gold_fills":
                raise RuntimeError("simulated fills write failure")
            return original_upsert(table, *args, **kwargs)

        monkeypatch.setattr(cat, "upsert", exploding_upsert)

        with caplog.at_level("ERROR", logger="cquant.backtest_vector.run"):
            run_id = _run_backtest(cat, prices)

        # 回测本身仍完成，但 fills 空表 + 元数据可见失败
        fills = cat.query("SELECT * FROM gold_fills WHERE run_id = ?", [run_id])
        assert fills.is_empty()

        tags_raw = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
        )["tags"][0]
        assert tags_raw, "run tags must carry the failure stamp"
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else dict(tags_raw)
        assert tags.get("fills_persisted") is False
        assert "simulated fills write failure" in tags.get("fills_error", "")

        assert any(
            "gold_fills" in rec.message and run_id in rec.message
            for rec in caplog.records
        ), "failure must be logged at error level with run_id"

    def test_mark_failure_preserves_non_dict_tags(self, catalog) -> None:
        """Backlog #4：tags 为非 dict JSON（字符串/数组）时保留原值到 tags_legacy。"""
        cat, _ = catalog
        run_id = _run_backtest(cat, _synthetic_prices())
        # Simulate a legacy non-dict tags value (older writers stored a bare
        # JSON array / string).
        cat.execute(
            "UPDATE gold_backtest_runs SET tags = ? WHERE run_id = ?",
            ['["legacy", "tags"]', run_id],
        )
        runner = BacktestRunner(cat)
        runner._mark_fills_persist_failure(run_id, RuntimeError("boom"))

        raw = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
        )["tags"][0]
        tags = json.loads(raw) if isinstance(raw, str) else dict(raw)
        assert tags["fills_persisted"] is False
        assert "boom" in tags["fills_error"]
        assert tags["tags_legacy"] == '["legacy", "tags"]', (
            "original non-dict tags value must be preserved, not dropped"
        )

    def test_mark_failure_keeps_dict_tags_merge(self, filled_run) -> None:
        """正常 dict tags：合并写入，不产生 tags_legacy。"""
        cat, run_id = filled_run
        cat.execute(
            "UPDATE gold_backtest_runs SET tags = ? WHERE run_id = ?",
            ['{"top_n": 3}', run_id],
        )
        BacktestRunner(cat)._mark_fills_persist_failure(
            run_id, ValueError("nope")
        )
        raw = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
        )["tags"][0]
        tags = json.loads(raw) if isinstance(raw, str) else dict(raw)
        assert tags["top_n"] == 3
        assert tags["fills_persisted"] is False
        assert "tags_legacy" not in tags


# ── T3: tca / stress-test 端点 ────────────────────────────────────────────────

class TestTcaAndStressEndpoints:
    def test_stress_test_returns_scenarios(self, filled_run) -> None:
        cat, run_id = filled_run
        result = asyncio.run(get_stress_test(run_id, cat, custom_start=None, custom_end=None))
        assert isinstance(result["scenarios"], list) and result["scenarios"]
        for sc in result["scenarios"]:
            assert sc.get("name")
            assert "impact" in sc

    def test_tca_returns_cost_breakdown(self, filled_run) -> None:
        cat, run_id = filled_run
        # 补一条 analysis run + TCA 汇总（analyze 流程的产物）
        cat.execute(
            "INSERT INTO gold_bt_analysis_runs "
            "(analysis_run_id, backtest_run_id, overall_overfit_score, dsr, psr, summary, created_at) "
            "VALUES ('ar_test_1', ?, 0.1, 0.5, 0.6, 'test', now())",
            [run_id],
        )
        cat.execute(
            "INSERT INTO gold_bt_tca "
            "(analysis_run_id, total_turnover, total_commission, total_stamp_duty, "
            "total_slippage, total_cost, cost_per_trade, cost_pct_turnover, "
            "num_trades, avg_trade_size) "
            "VALUES ('ar_test_1', 1e6, 100.0, 50.0, 30.0, 180.0, 1.0, 0.02, 20, 50000.0)"
        )
        result = asyncio.run(get_backtest_tca(run_id, cat))
        assert float(result["total_cost"]) == pytest.approx(180.0)
        # IS 分析依赖 gold_fills（qty 列）
        is_result = result.get("implementation_shortfall", {})
        assert "total_is_bps" in is_result


# ── T5: run-report 最近交易 qty 列 ────────────────────────────────────────────

class TestRunReportQtyColumn:
    def test_export_report_renders_recent_fills(self, filled_run) -> None:
        cat, run_id = filled_run
        response = asyncio.run(export_backtest_report(run_id, cat))
        assert response.status_code == 200
        body = response.body.decode("utf-8") if isinstance(response.body, bytes) else str(response.body)
        assert "最近 20 笔交易" in body
        # 有成交资产出现在表格中（SELECT qty 不再 binder error → 500）
        fills = cat.query(
            "SELECT asset_id, qty FROM gold_fills WHERE run_id = ? LIMIT 1", [run_id]
        )
        assert not fills.is_empty()
        assert fills["asset_id"][0] in body
