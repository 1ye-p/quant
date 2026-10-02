"""CRUD API 端点测试（P1-5）：/datasets/external-indicators/catalog*

覆盖：4 端点 happy path、404（key 不存在）、400（非法 key / 非法 frequency /
非法 available_date_rule / P2 保留字段）、purge_data 语义、PATCH 持久化、
列表 live 新鲜度字段（stale / latest_trade_date）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BASE = "/api/v1/datasets/external-indicators/catalog"


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog, monkeypatch):
    # dev 模式 + 无 key：非交易端点放行（conda 环境可能注入 CQUANT_API_KEY，先摘掉）
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _seed(catalog: Catalog, key: str = "margin_balance", n_rows: int = 3) -> None:
    catalog.execute(
        "INSERT INTO silver_external_indicator_catalog "
        "(indicator_key, display_name, source_type, source_name, frequency) "
        f"VALUES ('{key}', '{key}', 'csv', 'test_src', 'daily')"
    )
    for i in range(n_rows):
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            f"VALUES ('test_src', '{key}', '__MARKET__', "
            f"'2025-01-0{i + 1}', {100.0 + i}, '2025-01-0{i + 2}')"
        )


def _data_count(catalog: Catalog, key: str) -> int:
    return catalog.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicators "
        "WHERE indicator_key = ?",
        [key],
    )["n"][0]


# ── GET list ─────────────────────────────────────────────────────────────────


class TestListCatalog:
    def test_list_empty(self, client):
        resp = client.get(_BASE)
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}

    def test_list_contains_live_freshness_fields(self, client, catalog):
        _seed(catalog)
        catalog.execute(
            "INSERT INTO silver_prices_1d "
            "(asset_id, trade_date, open, high, low, close, volume, source) VALUES "
            "('SZSE:000001', '2025-06-02', 10, 11, 9, 10, 1000, 'test'), "
            "('SZSE:000001', '2025-06-03', 10, 11, 9, 10, 1000, 'test'), "
            "('SZSE:000001', '2025-06-04', 10, 11, 9, 10, 1000, 'test'), "
            "('SZSE:000001', '2025-06-05', 10, 11, 9, 10, 1000, 'test'), "
            "('SZSE:000001', '2025-06-06', 10, 11, 9, 10, 1000, 'test')"
        )
        resp = client.get(_BASE)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["indicator_key"] == "margin_balance"
        assert item["latest_trade_date"] == "2025-01-03"
        # 5 个交易日滞后 > daily 容差 3 → stale
        assert item["stale"] is True
        assert item["frequency"] == "daily"
        assert "last_status" in item and "enabled" in item


# ── GET detail ───────────────────────────────────────────────────────────────


class TestGetCatalogEntry:
    def test_detail_with_preview(self, client, catalog):
        _seed(catalog, n_rows=3)
        resp = client.get(f"{_BASE}/margin_balance")
        assert resp.status_code == 200
        entry = resp.json()
        assert entry["indicator_key"] == "margin_balance"
        preview = entry["preview"]
        assert len(preview) == 3
        assert preview[-1] == {
            "trade_date": "2025-01-03",
            "value": 102.0,
            "available_date": "2025-01-04",
        }

    def test_detail_404(self, client):
        resp = client.get(f"{_BASE}/no_such_key")
        assert resp.status_code == 404

    def test_detail_invalid_key_400(self, client):
        resp = client.get(f"{_BASE}/Bad-Key")
        assert resp.status_code == 400


# ── PATCH ────────────────────────────────────────────────────────────────────


class TestPatchCatalogEntry:
    def test_patch_updates_fields(self, client, catalog):
        _seed(catalog)
        resp = client.patch(
            f"{_BASE}/margin_balance",
            json={
                "display_name": "两融余额",
                "unit": "亿元",
                "frequency": "weekly",
                "enabled": False,
                "available_date_rule": "A",
                "description": "融资融券余额",
            },
        )
        assert resp.status_code == 200
        entry = resp.json()
        assert entry["display_name"] == "两融余额"
        assert entry["unit"] == "亿元"
        assert entry["frequency"] == "weekly"
        assert entry["enabled"] is False
        assert entry["available_date_rule"] == "A"

        # persisted
        row = catalog.query(
            "SELECT display_name, unit, frequency, enabled, available_date_rule "
            "FROM silver_external_indicator_catalog WHERE indicator_key = ?",
            ["margin_balance"],
        ).row(0, named=True)
        assert row["display_name"] == "两融余额"
        assert row["frequency"] == "weekly"
        assert row["enabled"] is False

    def test_patch_partial_keeps_other_fields(self, client, catalog):
        _seed(catalog)
        resp = client.patch(
            f"{_BASE}/margin_balance", json={"display_name": "改名"}
        )
        assert resp.status_code == 200
        assert resp.json()["frequency"] == "daily"  # untouched

    def test_patch_404(self, client):
        resp = client.patch(
            f"{_BASE}/no_such_key", json={"display_name": "x"}
        )
        assert resp.status_code == 404

    def test_patch_invalid_frequency_400(self, client, catalog):
        _seed(catalog)
        resp = client.patch(
            f"{_BASE}/margin_balance", json={"frequency": "monthly"}
        )
        assert resp.status_code == 400

    def test_patch_invalid_rule_400(self, client, catalog):
        _seed(catalog)
        resp = client.patch(
            f"{_BASE}/margin_balance", json={"available_date_rule": "C"}
        )
        assert resp.status_code == 400

    def test_patch_invalid_key_400(self, client):
        resp = client.patch("/BAD_key", json={"display_name": "x"})
        assert resp.status_code in (400, 404)  # route miss → 400 by key check path
        resp2 = client.patch(f"{_BASE}/BAD-KEY", json={"display_name": "x"})
        assert resp2.status_code == 400

    def test_patch_p2_fields_rejected_400(self, client, catalog):
        _seed(catalog)
        resp = client.patch(
            f"{_BASE}/margin_balance",
            json={"pinned_source": "akshare"},
        )
        assert resp.status_code == 400
        assert "pinned_source" in resp.json()["detail"]
        resp2 = client.patch(
            f"{_BASE}/margin_balance",
            json={"source_config": "{}"},
        )
        assert resp2.status_code == 400
        assert "source_config" in resp2.json()["detail"]


# ── DELETE ───────────────────────────────────────────────────────────────────


class TestDeleteCatalogEntry:
    def test_delete_keeps_data_by_default(self, client, catalog):
        _seed(catalog)
        resp = client.delete(f"{_BASE}/margin_balance")
        assert resp.status_code == 200
        body = resp.json()
        assert body["purged_data"] is False
        assert _data_count(catalog, "margin_balance") == 3
        assert client.get(f"{_BASE}/margin_balance").status_code == 404

    def test_delete_purge_true_removes_data(self, client, catalog):
        _seed(catalog)
        resp = client.delete(f"{_BASE}/margin_balance?purge_data=true")
        assert resp.status_code == 200
        assert resp.json()["purged_data"] is True
        assert _data_count(catalog, "margin_balance") == 0

    def test_delete_404(self, client):
        assert client.delete(f"{_BASE}/no_such_key").status_code == 404

    def test_delete_invalid_key_400(self, client):
        assert client.delete(f"{_BASE}/Bad-Key").status_code == 400
