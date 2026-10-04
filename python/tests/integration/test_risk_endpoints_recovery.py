"""B4a e2e：gold_positions 幻影表修复后的三风险端点恢复.

`gold_positions` 被 correlation / factor-exposure / risk-contribution 查询
但无 DDL 无写入方——修复前任何 run 三端点皆 500（Catalog: table does not
exist）。本文件走**生产装配路径**（TestClient + 真实 POST /backtests 路由
+ BacktestRunner 生产 runner，tmp catalog 自隔离）证明：

1. 新 run 落盘 gold_positions 后三端点 200；
2. gold_positions 无行的旧 run 三端点 404（空结果分支），不再 500。
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

N_DAYS = 40
ASSETS = ["SSE:600000", "SSE:600036", "SSE:601318"]
FEATURE_SET = "fsv_risk_ep"
DATES = [date(2025, 1, 2) + timedelta(days=i) for i in range(N_DAYS)]

DSL_SPEC: dict = {
    "name": "risk_ep_recovery",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
}

RISK_ENDPOINTS = (
    "/correlation",
    "/factor-exposure",
    "/risk-contribution",
)


@pytest.fixture()
def risk_catalog(tmp_path, monkeypatch):
    """tmp DuckDB：行情 + 1 因子 + 1 条 DSL 策略配置（生产路由前置数据）。"""
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "risk_ep.duckdb", repo_root=_REPO_ROOT)
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

    # 因子 'mom'：确定性 per-asset level → 稳定 top-N 排序
    fac_rows = [
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": float(i % len(ASSETS))}
        for i, d in enumerate(DATES) for a in ASSETS
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
        "VALUES ('risk_ep_recovery', 'json', '{}', now(), now())"
    )
    return cat


@pytest.fixture()
def client(risk_catalog, monkeypatch):
    # 认证 fixture 沿 test_external_indicator_e2e 模式：dev 模式 + 无 key
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: risk_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, risk_catalog
    app.dependency_overrides = {}


def _post_backtest(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/backtests",
        json={
            "strategy_id": "risk_ep_recovery",
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

    # TestClient 同步执行 BackgroundTasks；轮询 job 兜底（防未来改异步执行）
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/backtests/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed", job
    return job["run_id"]


def test_risk_endpoints_recover_after_fix(client) -> None:
    """生产路径 run → gold_positions 落盘 → 三风险端点全部 200。"""
    c, cat = client
    run_id = _post_backtest(c)

    # 前置：per-asset 持仓确实落盘（端点 200 的数据来源）
    positions = cat.query(
        "SELECT * FROM gold_positions WHERE run_id = ?", [run_id]
    )
    assert not positions.is_empty(), "gold_positions must be populated by the run"

    for ep in RISK_ENDPOINTS:
        resp = c.get(f"/api/v1/backtests/{run_id}{ep}")
        assert resp.status_code == 200, (
            f"{ep} returned {resp.status_code}: {resp.text[:300]}"
        )
        assert resp.json() is not None


def test_old_run_degrades_gracefully(client) -> None:
    """gold_positions 无行的 run：三端点 404（空结果分支），不再 500。"""
    c, cat = client
    # 伪造一个无 positions 的旧 run（gold_bt_runs 行存在、gold_positions 无行）
    cat.execute(
        "INSERT INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status) "
        "VALUES ('run_ghost_positions', 'vector', 'risk_ep_recovery', 'v1', "
        "now(), 'completed')"
    )
    for ep in RISK_ENDPOINTS:
        resp = c.get(f"/api/v1/backtests/run_ghost_positions{ep}")
        assert resp.status_code == 404, (
            f"{ep} returned {resp.status_code} (expected 404 empty-result "
            f"branch, not 500): {resp.text[:300]}"
        )
