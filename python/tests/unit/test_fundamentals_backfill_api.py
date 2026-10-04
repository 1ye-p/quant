"""POST /datasets/fundamentals/backfill-announce 端点测试（A1 / D4-A）。

覆盖：dry_run 不写库、实跑返回统计且与库内变化一致、strict 模式未认证 401
（认证 fixture 模式沿 test_ext_indicator_catalog_api.py，注意 delenv
CQUANT_API_KEY）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_URL = "/api/v1/datasets/fundamentals/backfill-announce"


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    cat.executemany(
        "INSERT INTO silver_fundamentals (asset_id, report_date, announce_date, source) "
        "VALUES (?, ?, ?, ?)",
        [
            # akshare lookahead rows（回填目标）
            ("SSE:600000", date(2025, 3, 31), date(2025, 3, 31), "akshare"),
            ("SZSE:000001", date(2024, 12, 31), date(2024, 12, 31), "akshare"),
            # tushare 正常行——不可被改
            ("SSE:600036", date(2025, 3, 31), date(2025, 4, 25), "tushare"),
        ],
    )
    return cat


@pytest.fixture()
def client(catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    # lifespan（scheduler/backfill）也会取真实 catalog：本机若有 uvicorn 实例
    # 持有 data/catalog.duckdb 文件锁会直接冲突——统一指到 tmp catalog。
    monkeypatch.setattr(deps, "_get_catalog", lambda: catalog)
    monkeypatch.setattr(deps, "close_catalog", lambda: None)  # shutdown: tmp catalog 由 fixture 关闭
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _announce(cat, asset_id, report_date):
    df = cat.query(
        "SELECT announce_date FROM silver_fundamentals "
        "WHERE asset_id = ? AND report_date = ?",
        [asset_id, report_date],
    )
    return df["announce_date"][0] if len(df) else None


def test_backfill_dry_run_writes_nothing(client, catalog):
    resp = client.post(_URL, json={"dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == 2
    assert body["updated"] == 0
    assert body["by_source"] == {"akshare": 2}
    # dry_run 不写库 → 前视行仍在，violations_after 保持 2
    assert body["violations_after"] == 2
    assert body["tushare_violations"] == 0
    # 库未变化：lookahead 行原样
    assert _announce(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 3, 31)


def test_backfill_default_is_real_run(client, catalog):
    """body 缺省 dry_run=False：实跑，返回统计且与库内变化一致。"""
    resp = client.post(_URL, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 2
    assert body["violations_after"] == 0  # 实跑后前视行已修复
    # 库内变化与统计一致
    assert _announce(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 4, 30)
    assert _announce(catalog, "SZSE:000001", date(2024, 12, 31)) == date(2025, 4, 30)
    # tushare 行未被修改
    assert _announce(catalog, "SSE:600036", date(2025, 3, 31)) == date(2025, 4, 25)


def test_backfill_strict_mode_unauthenticated_401(catalog, monkeypatch):
    """strict 模式：配置了 key 但未携带凭据 → 401。"""
    monkeypatch.setenv("CQUANT_AUTH_MODE", "strict")
    monkeypatch.setenv("CQUANT_API_KEY", "test-secret-key")
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    monkeypatch.setattr(deps, "_get_catalog", lambda: catalog)
    monkeypatch.setattr(deps, "close_catalog", lambda: None)  # shutdown: tmp catalog 由 fixture 关闭
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(_URL, json={"dry_run": True})
    assert resp.status_code == 401
    app.dependency_overrides = {}
