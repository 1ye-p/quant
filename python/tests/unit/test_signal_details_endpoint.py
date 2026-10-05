"""B2 读侧：GET /backtests/{run_id}/signals 端点单测.

双语义（date 参数二态）：
- 无 date  → 调仓日列表 {dates: [...], total}（DISTINCT trade_date，只取自
  gold_bt_signal_details 本表——数据源自洽，不另造来源）
- 有 date  → 该日明细 {items, total, page, page_size}（rank 升序、NULL 最后，
  分页 page/page_size 默认 ≤500 上限，factor_scores_json 解析为 dict）

404 判定顺序（逐级）：
1. run_not_found            — gold_backtest_runs 无此 run
2. unsupported_strategy_type — run 的 strategy_type 不在 DSL/MultiFactor
  白名单（Combo 无 last_score_detail 能力，评审 I1 后移出）（先查 run 类型再查明细行；StaticTopN 直落此态）
3. no_signal_details        — run 存在、类型支持但明细表无行（旧 run / 落盘失败）

夹具：tmp catalog 直接插 gold_backtest_runs / gold_bt_signal_details 行
（写侧由 runner sink 负责，此处只验证读侧）。
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

RUN_ID = "run_sig_unit"


def _insert_run(cat: Catalog, run_id: str = RUN_ID,
                strategy_type: str = "DSL") -> None:
    cat.execute(
        "INSERT INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status, "
        " strategy_type) "
        f"VALUES ('{run_id}', 'vector', 'strat', 'v1', now(), 'completed', "
        f"'{strategy_type}')"
    )


def _insert_detail(cat: Catalog, run_id: str, td: str, asset_id: str,
                   score: float | None, rank: int | None,
                   action: str = "hold", prev_w: float = 0.0,
                   new_w: float = 0.0,
                   factor_scores: dict | None = None) -> None:
    fs = json.dumps(factor_scores) if factor_scores is not None else None
    cat.execute(
        "INSERT INTO gold_bt_signal_details "
        "(run_id, trade_date, asset_id, score, rank, action, prev_weight, "
        " new_weight, factor_scores_json) "
        f"VALUES ('{run_id}', '{td}', '{asset_id}', "
        f"{'NULL' if score is None else score}, "
        f"{'NULL' if rank is None else rank}, '{action}', {prev_w}, {new_w}, "
        f"{'NULL' if fs is None else chr(39) + fs + chr(39)})"
    )


@pytest.fixture()
def sig_catalog(tmp_path, monkeypatch):
    # chdir 沙箱化：默认 catalog 路径 CWD 相对，避免与本地 dev server 的
    # data/catalog.duckdb 锁冲突（沿 attribution fixture 先例）。
    monkeypatch.chdir(tmp_path)
    cat = Catalog(db_path=tmp_path / "sig_endpoint_unit.duckdb",
                  repo_root=_REPO_ROOT)
    cat.initialize()
    # run 行的 strategy_type 列由 runner 迁移补齐（run.py ALTER）；纯读侧
    # fixture 不经过 runner，手动补列以对齐生产 schema。
    cat.execute(
        "ALTER TABLE gold_backtest_runs ADD COLUMN IF NOT EXISTS "
        "strategy_type VARCHAR DEFAULT ''"
    )
    return cat


@pytest.fixture()
def client(sig_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: sig_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, sig_catalog
    app.dependency_overrides = {}


def _get(c: TestClient, run_id: str = RUN_ID, **params) -> object:
    return c.get(f"/api/v1/backtests/{run_id}/signals", params=params or None)


# ── dates 语义（无 date） ────────────────────────────────────────────────────

def test_dates_distinct_sorted(client) -> None:
    c, cat = client
    _insert_run(cat)
    _insert_detail(cat, RUN_ID, "2025-06-09", "A0", 1.0, 1)
    _insert_detail(cat, RUN_ID, "2025-06-02", "A1", 1.0, 1)
    _insert_detail(cat, RUN_ID, "2025-06-09", "A2", 0.9, 2)  # 同日多资产
    resp = _get(c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dates"] == ["2025-06-02", "2025-06-09"]  # DISTINCT + 升序
    assert body["total"] == 2


# ── 明细语义（有 date） ─────────────────────────────────────────────────────

def test_detail_rank_asc_nulls_last_and_pagination(client) -> None:
    c, cat = client
    _insert_run(cat)
    # rank 3/1/NULL → 期望顺序 1, 3, NULL
    _insert_detail(cat, RUN_ID, "2025-06-02", "A2", 0.8, 3, "candidate")
    _insert_detail(cat, RUN_ID, "2025-06-02", "A0", 1.0, 1, "enter",
                   factor_scores={"mom": 0.6, "value": 0.4})
    _insert_detail(cat, RUN_ID, "2025-06-02", "X_UNS", None, None, "exit")

    resp = _get(c, date="2025-06-02")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [it["asset_id"] for it in body["items"]] == ["A0", "A2", "X_UNS"]
    assert body["total"] == 3
    assert body["page"] == 0 and body["page_size"] == 50

    a0 = body["items"][0]
    assert a0["score"] == pytest.approx(1.0)
    assert a0["factor_scores"] == {"mom": pytest.approx(0.6),
                                   "value": pytest.approx(0.4)}
    xuns = body["items"][2]
    assert xuns["score"] is None and xuns["factor_scores"] == {}

    # 分页：page_size=2 第 1 页 → 仅第 3 行（NULL rank）
    resp = _get(c, date="2025-06-02", page=1, page_size=2)
    body = resp.json()
    assert [it["asset_id"] for it in body["items"]] == ["X_UNS"]
    assert body["total"] == 3 and body["page"] == 1 and body["page_size"] == 2


def test_detail_page_size_capped_at_500(client) -> None:
    """page_size 上限 500：边界值 200，超限由 Query 校验直接 422。"""
    c, cat = client
    _insert_run(cat)
    _insert_detail(cat, RUN_ID, "2025-06-02", "A0", 1.0, 1)
    resp = _get(c, date="2025-06-02", page_size=500)
    assert resp.status_code == 200, resp.text
    assert resp.json()["page_size"] == 500
    resp = _get(c, date="2025-06-02", page_size=9999)
    assert resp.status_code == 422


def test_detail_empty_date_returns_empty_items(client) -> None:
    """类型支持 + 明细表有其他日期行：查无数据的日期 → 200 空列表（非 404）。"""
    c, cat = client
    _insert_run(cat)
    _insert_detail(cat, RUN_ID, "2025-06-02", "A0", 1.0, 1)
    resp = _get(c, date="2025-06-03")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == [] and body["total"] == 0


# ── 404 语义 ────────────────────────────────────────────────────────────────

def test_404_run_not_found(client) -> None:
    c, _ = client
    resp = _get(c, "run_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "run_not_found"


def test_404_unsupported_strategy_type(client) -> None:
    """StaticTopN（及任意非白名单类型）直落此态——即使明细表也无行。"""
    c, cat = client
    _insert_run(cat, "run_static", strategy_type="StaticTopN")
    resp = _get(c, "run_static")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unsupported_strategy_type"


def test_404_unsupported_beats_no_details(client) -> None:
    """类型判定先于明细行判定：非白名单 + 明细表恰好有行仍 404 unsupported。"""
    c, cat = client
    _insert_run(cat, "run_static2", strategy_type="MarketNeutral")
    _insert_detail(cat, "run_static2", "2025-06-02", "A0", 1.0, 1)
    resp = _get(c, "run_static2")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unsupported_strategy_type"


def test_404_no_signal_details(client) -> None:
    """类型支持但无明细行（旧 run / 落盘失败）→ no_signal_details。"""
    c, cat = client
    _insert_run(cat)  # DSL，无明细行
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_signal_details"


def test_strategy_type_fallback_to_saved_config(client) -> None:
    """旧 run 行 strategy_type 为空：沿 get_backtest 先例回退
    meta_strategy_configs.parsed_config。"""
    c, cat = client
    _insert_run(cat, "run_legacy", strategy_type="")
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_text, parsed_config, created_at, updated_at) "
        f"VALUES ('strat', 'legacy', "
        f"'{json.dumps({'strategy_type': 'MultiFactor'})}', now(), now())"
    )
    _insert_detail(cat, "run_legacy", "2025-06-02", "A0", 1.0, 1)
    resp = _get(c, "run_legacy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["dates"] == ["2025-06-02"]


# ── 防御性解析 ──────────────────────────────────────────────────────────────

def test_malformed_factor_scores_degrades_to_empty_dict(client) -> None:
    """旧数据 factor_scores_json 畸形：解析为 {} 并 log warning，绝不 500。"""
    c, cat = client
    _insert_run(cat)
    # JSON 类型列本身会拒绝畸形串；重建表把该列降为 VARCHAR 模拟
    # 「动态建列时代的旧库」（PK 索引使 ALTER DROP/TYPE 不可用）
    cat.execute(
        "CREATE OR REPLACE TABLE gold_bt_signal_details AS SELECT * "
        "EXCLUDE (factor_scores_json), "
        "CAST(factor_scores_json AS VARCHAR) AS factor_scores_json "
        "FROM gold_bt_signal_details"
    )
    cat.execute(
        "INSERT INTO gold_bt_signal_details "
        "(run_id, trade_date, asset_id, score, rank, action, prev_weight, "
        " new_weight, factor_scores_json) "
        f"VALUES ('{RUN_ID}', '2025-06-02', 'A0', 1.0, 1, 'hold', 0.5, 0.5, "
        "'{not json')"
    )
    resp = _get(c, date="2025-06-02")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["factor_scores"] == {}
