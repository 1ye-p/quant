"""B1 e2e：归因生产路径全链贯通（写侧 → 落表 → 读侧）.

生产装配路径（沿 test_risk_endpoints_recovery 模式）：TestClient + 真实
POST /backtests 路由 + 生产 DSL runner，tmp catalog 自隔离。

链路：POST /backtests（带 benchmark 语义的等权基准 + 多资产行业映射场景）
→ 轮询完成 → 自动分析落 gold_bt_analysis_runs → AnalysisRunner 生产写侧
落 gold_bt_attribution → GET /backtests/{run_id}/attribution 200 →
Brinson 恒等式（allocation + selection + interaction ≈ active_return，
容差 1e-6）+ total_return 同源一致（独立从 gold_signals ⋈ silver_prices_1d
重算 Σ w̄·r 与 summary.total_return 对账）。

已知写侧缺口（本测试不修复，红线：不动 bt_analyzer 归因计算/写入）：
1. ``load_result`` 重建的 BacktestResult 不含 spec.prices（_ReconstructedSpec
   默认空表）→ 自动分析路径的 Brinson 块拿不到价格、从不产归因。故本
   e2e 在回测完成后用**同一 tmp catalog 的真实 silver_prices_1d** 补挂
   prices 再走生产 ``AnalysisRunner.run``（真实 _persist_attribution 写入，
   无 mock）。修 load_result 的 prices 重建后可去掉补挂步骤。
2. engine.py 传 ``benchmark_returns={a: bench_ret}``（常数）使三效应恒 ≈ 0，
   故等权夹具下恒等式为 0=0 平凡成立；不等权夹具下恒等式被写侧破坏
   （Σ effects ≡ 0 ≠ active_return），见任务报告 concern W2。
"""

from __future__ import annotations

import dataclasses
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.bt_analyzer.run import (
    AnalysisRunSpec,
    AnalysisRunner,
    _ReconstructedSpec,
    load_result,
)
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

N_DAYS = 40
ASSETS = ["SSE:600000", "SSE:600036", "SSE:601318"]
FEATURE_SET = "fsv_attr_e2e"
DATES = [date(2025, 1, 2) + timedelta(days=i) for i in range(N_DAYS)]

DSL_SPEC: dict = {
    "name": "attr_e2e",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
}


@pytest.fixture()
def attr_catalog(tmp_path, monkeypatch):
    """tmp DuckDB：行情 + 因子 + DSL 策略配置（生产路由前置数据）。"""
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "attr_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()

    rng = np.random.default_rng(7)
    price_rows = []
    p = {a: 50.0 for a in ASSETS}
    for d in DATES:
        for a in ASSETS:
            p[a] *= 1 + rng.normal(0.001, 0.01)
            price_rows.append({
                "asset_id": a, "trade_date": d,
                "open": p[a], "high": p[a] * 1.01, "low": p[a] * 0.99,
                "close": p[a], "volume": 1e6, "amount": p[a] * 1e6,
                "adj_factor": 1.0, "adj_close": p[a],
                "is_suspended": False, "source": "test",
            })
    conn.register("_px", pl.DataFrame(price_rows).to_arrow())
    conn.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, amount, "
        " adj_factor, adj_close, is_suspended, source) "
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount, "
        "       adj_factor, adj_close, is_suspended, source FROM _px"
    )
    conn.unregister("_px")

    # 因子 'mom'：全日期稳定排序（等权 top_n=2 → 固定双资产持仓；
    # 沿 test_risk_endpoints_recovery 的 tie-break 模式）
    fac_rows = [
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": float(i % len(ASSETS))}
        for i, d in enumerate(DATES)
        for a in ASSETS
    ]
    conn.register("_fac", pl.DataFrame(fac_rows).to_arrow())
    conn.execute(
        "INSERT INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _fac"
    )
    conn.unregister("_fac")

    conn.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, created_at, updated_at) "
        "VALUES ('attr_e2e', 'json', '{}', now(), now())"
    )
    return cat


@pytest.fixture()
def client(attr_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: attr_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, attr_catalog
    app.dependency_overrides = {}


def _post_backtest(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/backtests",
        json={
            "strategy_id": "attr_e2e",
            "dataset_version": "v1",
            "strategy_type": "DSL",
            "dsl_spec": DSL_SPEC,
            "start_date": DATES[0].isoformat(),
            "end_date": DATES[-1].isoformat(),
            "feature_set_version": FEATURE_SET,
            "top_n": 2,
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + 120
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/backtests/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed", job
    return job["run_id"]


def _run_analysis_with_prices(cat: Catalog, run_id: str) -> str:
    """生产 AnalysisRunner 写侧跑通归因落表，返回 analysis_run_id。

    load_result 目前不重建 spec.prices（见模块 docstring 缺口 1），
    此处用同一 catalog 的真实 silver_prices_1d 补挂后走**未打补丁的生产
    写侧**（AnalysisRunner.run → _persist_attribution）——不 mock 分析器。
    """
    result = load_result(run_id, cat)
    prices = cat.query(
        "SELECT asset_id, trade_date, close FROM silver_prices_1d "
        "WHERE asset_id IN ('SSE:600000', 'SSE:600036', 'SSE:601318') "
        "  AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [DATES[0], DATES[-1]],
    )
    spec = _ReconstructedSpec(
        prices=prices,
        start_date=result.spec.start_date,
        end_date=result.spec.end_date,
    )
    report = AnalysisRunner(cat).run(
        dataclasses.replace(result, spec=spec),
        AnalysisRunSpec(backtest_run_id=run_id),
    )
    return report.analysis_run_id


def test_attribution_end_to_end(client) -> None:
    """生产路径：回测 → 分析落表 → GET /attribution 200 → Brinson 恒等式。"""
    c, cat = client
    run_id = _post_backtest(c)

    # 自动分析已落 gold_bt_analysis_runs（写侧管线活着的证据）
    analysis = cat.query(
        "SELECT COUNT(*) n FROM gold_bt_analysis_runs WHERE backtest_run_id = ?",
        [run_id],
    )
    assert analysis["n"][0] >= 1

    ar_id = _run_analysis_with_prices(cat, run_id)
    assert cat.query(
        "SELECT COUNT(*) n FROM gold_bt_attribution WHERE analysis_run_id = ?",
        [ar_id],
    )["n"][0] == 1

    resp = c.get(f"/api/v1/backtests/{run_id}/attribution")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_run_id"] == ar_id

    s = body["summary"]
    # Brinson 恒等式：三效应之和 ≈ active_return（等权夹具下为平凡 0=0，
    # 见模块 docstring 缺口 2）
    effects_sum = s["allocation"] + s["selection"] + s["interaction"]
    assert effects_sum == pytest.approx(s["active_return"], abs=1e-6)
    assert s["total_return"] == pytest.approx(
        s["benchmark_return"] + s["active_return"], abs=1e-9
    )

    # 同源一致：独立从 gold_signals ⋈ silver_prices_1d 重算 Σ w̄·r
    weights = cat.query(
        "SELECT asset_id, AVG(target_weight) w FROM gold_signals "
        "WHERE signal_set_version = ? GROUP BY asset_id",
        [run_id],
    )
    prices = cat.query(
        "SELECT asset_id, trade_date, close FROM silver_prices_1d "
        "WHERE trade_date IN (?, ?) AND asset_id IN "
        "(SELECT DISTINCT asset_id FROM gold_signals WHERE signal_set_version = ?) "
        "ORDER BY trade_date",
        [DATES[0], DATES[-1], run_id],
    )
    px = {}
    for row in prices.iter_rows(named=True):
        px.setdefault(row["asset_id"], {})[row["trade_date"]] = float(row["close"])
    expected_total = 0.0
    for row in weights.iter_rows(named=True):
        series = px.get(row["asset_id"], {})
        if len(series) >= 2:
            first, last = series[DATES[0]], series[DATES[-1]]
            expected_total += float(row["w"]) * (last / first - 1)
    assert s["total_return"] == pytest.approx(expected_total, abs=1e-9), (
        f"summary.total_return {s['total_return']} != 独立重算 {expected_total}"
    )

    # periods（daily_json，实为 per-rebalance-period）与 sectors 解析形态
    assert isinstance(body["periods"], list) and body["periods"]
    assert {"allocation", "selection", "interaction"}.issubset(body["periods"][0])
    assert isinstance(body["sectors"], list) and body["sectors"]
    assert {"sector", "port_weight", "bench_weight"}.issubset(body["sectors"][0])


def test_attribution_absent_when_no_benchmark(client) -> None:
    """无归因的 run：GET /attribution → 404 reason=no_attribution。

    当前生产自动分析路径因 load_result 无 prices 从不产归因（缺口 1），
    任何不带补挂分析的回测都落在 no_attribution 态——这正是三态判定中
    「有 analysis run 但无归因行」的真实生产样本。
    """
    c, cat = client
    run_id = _post_backtest(c)

    # 前置：run 存在 + 自动分析存在，但归因行不存在
    assert not cat.query(
        "SELECT COUNT(*) n FROM gold_bt_attribution a JOIN gold_bt_analysis_runs b "
        "ON a.analysis_run_id = b.analysis_run_id WHERE b.backtest_run_id = ?",
        [run_id],
    )["n"][0] > 0

    resp = c.get(f"/api/v1/backtests/{run_id}/attribution")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_attribution"
