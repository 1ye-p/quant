"""B1 读侧：GET /backtests/{run_id}/attribution 端点单测.

三态 404 判定顺序（逐级）：
1. run_not_found     — gold_backtest_runs 无此 run
2. no_analysis_run   — run 存在但 gold_bt_analysis_runs 无分析记录
3. no_attribution    — 最新 analysis run 存在但 gold_bt_attribution 无归因行
   （亦涵盖旧数据 JSON 列畸形时的降级：404 而非 500）

夹具：tmp catalog 直接插 gold_backtest_runs / gold_bt_analysis_runs /
gold_bt_attribution 三表行（写侧由 bt_analyzer 负责，此处只验证读侧）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

RUN_ID = "run_attr_unit"
AR_ID_OLD = "ar-old"
AR_ID_NEW = "ar-new"

_PERIODS = [{"period_id": 0, "active_return": 0.01, "allocation": 0.004,
             "selection": 0.005, "interaction": 0.001}]
_SECTORS = {"Finance": {"port_weight": 0.6, "bench_weight": 0.5,
                        "port_return": 0.10, "bench_return": 0.08}}


def _insert_run(cat: Catalog, run_id: str = RUN_ID) -> None:
    cat.execute(
        "INSERT INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status) "
        f"VALUES ('{run_id}', 'vector', 'strat', 'v1', now(), 'completed')"
    )


def _insert_analysis(cat: Catalog, ar_id: str, run_id: str = RUN_ID,
                     created_at: str = "2026-01-01 00:00:00+00") -> None:
    cat.execute(
        "INSERT INTO gold_bt_analysis_runs "
        "(analysis_run_id, backtest_run_id, overall_overfit_score, dsr, psr, "
        " summary, created_at) "
        f"VALUES ('{ar_id}', '{run_id}', 0.1, 0.5, 0.6, NULL, '{created_at}')"
    )


def _insert_attribution(cat: Catalog, ar_id: str = AR_ID_NEW,
                        daily: str | None = None,
                        sectors: str | None = None) -> None:
    daily = json.dumps(_PERIODS) if daily is None else daily
    sectors = json.dumps(_SECTORS) if sectors is None else sectors
    cat.execute(
        "INSERT INTO gold_bt_attribution "
        "(analysis_run_id, total_return, benchmark_return, active_return, "
        " allocation_effect, selection_effect, interaction_effect, "
        " daily_json, sector_details_json) "
        f"VALUES ('{ar_id}', 0.12, 0.10, 0.02, 0.007, 0.011, 0.002, "
        f"'{daily}', '{sectors}')"
    )


@pytest.fixture()
def attr_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "attr_unit.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(attr_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: attr_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, attr_catalog
    app.dependency_overrides = {}


def _get(c: TestClient, run_id: str = RUN_ID):
    return c.get(f"/api/v1/backtests/{run_id}/attribution")


def test_200_summary_matches_fixture(client) -> None:
    """正常路径：summary 数字与夹具一致，periods/sectors 为解析后的 JSON。"""
    c, cat = client
    _insert_run(cat)
    _insert_analysis(cat, AR_ID_NEW)
    _insert_attribution(cat, AR_ID_NEW)

    resp = _get(c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_run_id"] == AR_ID_NEW
    assert body["summary"] == pytest.approx({
        "total_return": 0.12, "benchmark_return": 0.10, "active_return": 0.02,
        "allocation": 0.007, "selection": 0.011, "interaction": 0.002,
    })
    assert body["periods"] == _PERIODS
    sectors = body["sectors"]
    # {sector: metrics} 对象 → 列表化（[{sector, **metrics}]）
    assert isinstance(sectors, list) and len(sectors) == 1
    assert sectors[0]["sector"] == "Finance"
    assert sectors[0]["port_weight"] == pytest.approx(0.6)
    assert sectors[0]["bench_return"] == pytest.approx(0.08)


def test_404_run_not_found(client) -> None:
    c, _ = client
    resp = _get(c, "run_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "run_not_found"


def test_404_no_analysis_run(client) -> None:
    c, cat = client
    _insert_run(cat)
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_analysis_run"


def test_404_no_attribution(client) -> None:
    c, cat = client
    _insert_run(cat)
    _insert_analysis(cat, AR_ID_NEW)
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_attribution"


def test_latest_analysis_run_wins(client) -> None:
    """多 analysis_run：取 created_at 最新者的归因行。"""
    c, cat = client
    _insert_run(cat)
    _insert_analysis(cat, AR_ID_OLD, created_at="2026-01-01 00:00:00+00")
    _insert_analysis(cat, AR_ID_NEW, created_at="2026-02-01 00:00:00+00")
    # 只有旧 analysis run 有归因行 —— 新者无归因 → no_attribution（判定按最新 run）
    _insert_attribution(cat, AR_ID_OLD)
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_attribution"

    # 最新 analysis run 也有归因行 → 200 且返回最新的
    _insert_attribution(cat, AR_ID_NEW)
    resp = _get(c)
    assert resp.status_code == 200, resp.text
    assert resp.json()["analysis_run_id"] == AR_ID_NEW


def test_malformed_json_degrades_to_no_attribution(client) -> None:
    """旧数据 JSON 列畸形：404 no_attribution（解析防御，不许 500）。

    DDL 建表的 JSON 类型列本身会拒绝畸形串；此处先把列降级为 VARCHAR
    模拟「动态建表时代的旧库」（upsert 建列无 JSON 约束）。
    """
    c, cat = client
    cat.execute(
        "ALTER TABLE gold_bt_attribution ALTER daily_json TYPE VARCHAR"
    )
    _insert_run(cat)
    _insert_analysis(cat, AR_ID_NEW)
    _insert_attribution(cat, AR_ID_NEW, daily="{not json")
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_attribution"
