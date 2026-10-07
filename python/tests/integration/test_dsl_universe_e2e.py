"""F1 e2e: DSL universe 回归修复——生产路径验证（POST /backtests 全链路）。

回归链：zz500 DSL 策略 → RunModal（已修复）发 idx_zz500 → 后端实跑 zz500
且 tags 留痕；用户改选 hs300 重跑 → universe_override tag 在，实际资产池为
hs300 成分（经 gold_bt_signal_details 验证）。
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.backtest_vector.universe import INDEX_CONSTITUENTS_DDL
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

N_DAYS = 30
ZZ500_ASSETS = ["SSE:600036", "SSE:600519"]
HS300_ASSETS = ["SZSE:000001", "SZSE:300750"]
ALL_ASSETS = ZZ500_ASSETS + HS300_ASSETS
DATES = [date(2025, 7, 1) + timedelta(days=i) for i in range(N_DAYS)]
FEATURE_SET = "fsv_uni_e2e"

DSL_SPEC: dict = {
    "name": "uni_e2e",
    "universe": "idx_zz500",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
}


@pytest.fixture()
def uni_catalog(tmp_path, monkeypatch):
    """Prices + mom 因子 + 指数成分表（zz500=000905 / hs300=000300）。"""
    return _build_uni_catalog(tmp_path, monkeypatch)


def _build_uni_catalog(tmp_path, monkeypatch):
    """Fixture 主体（plain builder，便于诊断脚本复用）。"""
    cat = Catalog(db_path=tmp_path / "uni_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()
    conn.execute(INDEX_CONSTITUENTS_DDL)

    rng = np.random.default_rng(7)
    price_rows = []
    p = {a: 50.0 for a in ALL_ASSETS}
    for d in DATES:
        for a in ALL_ASSETS:
            p[a] *= 1 + rng.normal(0.001, 0.01)
            price_rows.append({
                "asset_id": a, "trade_date": d,
                "open": p[a], "high": p[a] * 1.01, "low": p[a] * 0.99,
                "close": p[a], "volume": 1e6, "amount": p[a] * 1e6,
                "adj_factor": 1.0, "adj_close": p[a],
                "is_suspended": False, "source": "test",
            })
    dfp = pl.DataFrame(price_rows)
    conn.register("_px", dfp.to_arrow())
    conn.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, amount, "
        " adj_factor, adj_close, is_suspended, source) "
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount, "
        "       adj_factor, adj_close, is_suspended, source FROM _px"
    )
    conn.unregister("_px")

    values = {a: float(ALL_ASSETS.index(a)) for a in ALL_ASSETS}
    rows = [
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": values[a]}
        for d in DATES for a in ALL_ASSETS
    ]
    dff = pl.DataFrame(rows)
    conn.register("_fac", dff.to_arrow())
    conn.execute(
        "INSERT INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _fac"
    )
    conn.unregister("_fac")

    cons_rows = (
        [{"index_code": "000905", "asset_id": a, "entry_date": DATES[0], "is_current": True}
         for a in ZZ500_ASSETS]
        + [{"index_code": "000300", "asset_id": a, "entry_date": DATES[0], "is_current": True}
           for a in HS300_ASSETS]
    )
    dfc = pl.DataFrame(cons_rows)
    conn.register("_cons", dfc.to_arrow())
    conn.execute(
        "INSERT INTO meta_index_constituents "
        "(index_code, asset_id, entry_date, is_current) "
        "SELECT index_code, asset_id, entry_date, is_current FROM _cons"
    )
    conn.unregister("_cons")

    # 策略配置：zz500 DSL（RunModal 修复后 modal 读到的就是这份 dsl_spec）
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, parsed_config, created_at, updated_at) "
        "VALUES ('uni_e2e', 'json', '{}', "
        f"'{json.dumps({'strategy_type': 'DSL', 'dsl_spec': DSL_SPEC})}', now(), now())"
    )
    return cat


@pytest.fixture()
def client(uni_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: uni_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, uni_catalog
    app.dependency_overrides = {}


def _post_and_wait(c, **overrides) -> str:
    """POST /backtests → 轮询 job 完成 → 返回 run_id。"""
    resp = c.post("/api/v1/backtests", json={
        "strategy_id": "uni_e2e",
        "dataset_version": "v1",
        "start_date": str(DATES[3]),
        "end_date": str(DATES[-1]),
        "feature_set_version": FEATURE_SET,
        "strategy_type": "DSL",
        **overrides,
    })
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]
    for _ in range(120):
        job = c.get(f"/api/v1/backtests/jobs/{job_id}").json()
        if job["status"] == "completed":
            return job["run_id"]
        if job["status"] == "failed":
            raise AssertionError(f"backtest job failed: {job.get('error')}")
        time.sleep(0.5)
    raise AssertionError("backtest job did not finish in 60s")


def _run_tags(cat, run_id: str) -> dict:
    raw = cat.query(
        "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )["tags"][0]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return dict(parsed or {})


def _fill_assets(cat, run_id: str) -> set[str]:
    """实际成交资产池 = 实跑 universe 的直接证据（价格面板按 resolved
    universe 过滤，池外资产无价格行 → 不可能产生 fill）。信号明细的
    candidate/enter 行覆盖完整特征截面（B2 设计），不能用作池断言。"""
    rows = cat.query(
        "SELECT DISTINCT asset_id FROM gold_fills WHERE run_id = ?",
        [run_id],
    )
    return set(rows["asset_id"].to_list())


def test_dsl_universe_e2e_consistent_then_override(client) -> None:
    c, cat = client

    # 1. zz500 DSL，body 一致（modal 修复后的默认 payload）→ 实跑 zz500 + 留痕
    run1 = _post_and_wait(c, dsl_spec=dict(DSL_SPEC), universe_id="idx_zz500")
    tags1 = _run_tags(cat, run1)
    assert tags1["universe_resolved"] == "idx_zz500"
    assert "universe_override" not in tags1
    # 信号明细有行（DSL 信号留痕路径打通）
    assert cat.query(
        "SELECT COUNT(*) AS n FROM gold_bt_signal_details WHERE run_id = ?",
        [run1],
    )["n"][0] > 0
    assets1 = _fill_assets(cat, run1)
    assert assets1, "no fills persisted — cannot verify actual pool"
    assert assets1 <= set(ZZ500_ASSETS), (
        f"expected zz500 pool only, got {sorted(assets1)}"
    )

    # 2. 用户改选 hs300 重跑 → override 留痕 + 实际池切换为 hs300 成分
    run2 = _post_and_wait(c, dsl_spec=dict(DSL_SPEC), universe_id="idx_hs300")
    tags2 = _run_tags(cat, run2)
    assert tags2["universe_resolved"] == "idx_hs300"
    assert tags2["universe_override"] == "idx_zz500->idx_hs300"
    assets2 = _fill_assets(cat, run2)
    assert assets2, "no fills persisted — cannot verify actual pool"
    assert assets2 <= set(HS300_ASSETS), (
        f"expected hs300 pool only, got {sorted(assets2)}"
    )
    assert assets2.isdisjoint(assets1), "two runs must trade disjoint pools"
