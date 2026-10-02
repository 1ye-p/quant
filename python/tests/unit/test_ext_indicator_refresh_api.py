"""手动刷新 + 运行历史端点测试（P2-4）。

覆盖：POST /external-indicators/refresh 三形态（全量 due / 指定 keys /
backfill）、未知 key 逐项报 error（非 404）、GET /runs 倒序/LIMIT/key
过滤/interrupted 标注、两端点 + enable 端点为 sync def（不阻 event loop）。
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BASE = "/api/v1/datasets/external-indicators"

_REFRESH_MOD = "cquant.datahub.pipelines.indicator_sources.refresh"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    from cquant.core.config import settings

    monkeypatch.setattr(
        type(settings), "tushare_token", property(lambda self: "")
    )
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    # 默认注入 fake refresh 模块（零真网）；个别用例覆盖/移除
    calls: list[dict] = []

    def fake_refresh(cat, keys, backfill, trigger):
        calls.append(
            {"keys": keys, "backfill": backfill, "trigger": trigger, "cat": cat}
        )
        return {"trigger": trigger, "results": [], "ok": 0, "error": 0}

    fake_mod = types.ModuleType(_REFRESH_MOD)
    fake_mod.run_external_indicator_refresh = fake_refresh
    monkeypatch.setitem(sys.modules, _REFRESH_MOD, fake_mod)

    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        # 把 calls 挂到 client 上供断言（fixture 闭包）
        c.refresh_calls = calls  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides = {}


def _insert_run(
    catalog: Catalog,
    key: str = "north_net_buy",
    status: str = "ok",
    started_at_expr: str = "NOW()",
    trigger: str = "manual",
) -> None:
    catalog.execute(
        "INSERT INTO silver_external_indicator_refresh_log "
        "(indicator_key, source_name, started_at, finished_at, status, trigger) "
        f"VALUES ('{key}', 'akshare', {started_at_expr}, "
        + ("NOW()" if status != "running" else "NULL")
        + f", '{status}', '{trigger}')"
    )


# ── POST /external-indicators/refresh ────────────────────────────────────────


class TestPostRefresh:
    def test_empty_body_full_due_refresh(self, client):
        resp = client.post(f"{_BASE}/refresh")
        assert resp.status_code == 200
        assert resp.json() == {
            "trigger": "manual", "results": [], "ok": 0, "error": 0
        }
        calls = client.refresh_calls  # type: ignore[attr-defined]
        assert len(calls) == 1
        assert calls[0]["keys"] is None  # None = 全量 due 枚举
        assert calls[0]["backfill"] is False
        assert calls[0]["trigger"] == "manual"

    def test_explicit_keys_forwarded(self, client):
        resp = client.post(
            f"{_BASE}/refresh", json={"keys": ["shibor_overnight", "usd_cny_parity"]}
        )
        assert resp.status_code == 200
        calls = client.refresh_calls  # type: ignore[attr-defined]
        assert calls[0]["keys"] == ["shibor_overnight", "usd_cny_parity"]
        assert calls[0]["backfill"] is False

    def test_backfill_true_forwarded(self, client):
        resp = client.post(
            f"{_BASE}/refresh", json={"keys": ["north_net_buy"], "backfill": True}
        )
        assert resp.status_code == 200
        calls = client.refresh_calls  # type: ignore[attr-defined]
        assert calls[0]["keys"] == ["north_net_buy"]
        assert calls[0]["backfill"] is True

    def test_unknown_key_reported_in_summary_not_404(self, client, catalog, monkeypatch):
        """未知 key 语义：不 404，逐 key 以 error 进 summary（真实 refresh 模块）。"""
        monkeypatch.delitem(sys.modules, _REFRESH_MOD)  # 还原真模块，零真网路径
        resp = client.post(f"{_BASE}/refresh", json={"keys": ["no_such_indicator"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] == 1
        result = body["results"][0]
        assert result["indicator_key"] == "no_such_indicator"
        assert result["status"] == "error"
        assert "not in catalog" in result["error"]

    def test_mixed_unknown_and_known_keys(self, client, catalog, monkeypatch):
        """未知 key 报 error，已知 builtin key 正常进入刷新流程（真模块，注入 fake adapter）。"""
        monkeypatch.delitem(sys.modules, _REFRESH_MOD)
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, enabled) "
            "VALUES ('shibor_overnight', 'Shibor O/N', 'builtin', 'builtin', TRUE)"
        )
        import polars as pl

        from cquant.datahub.pipelines.indicator_sources.adapters import (
            IndicatorFetchError,
        )

        class _FailAdapter:
            name = "akshare"

            def fetch(self, key, start, end):
                raise IndicatorFetchError("boom")

        real_mod = __import__(_REFRESH_MOD, fromlist=["run_external_indicator_refresh"])
        orig_refresh = real_mod.run_external_indicator_refresh

        def fail_refresh(cat, keys, backfill, trigger, adapters=None, inter_source_delay=0.0):
            return orig_refresh(
                cat, keys=keys, backfill=backfill, trigger=trigger,
                adapters={"akshare": _FailAdapter()}, inter_source_delay=0.0,
            )

        monkeypatch.setattr(
            real_mod, "run_external_indicator_refresh", fail_refresh, raising=False
        )
        # 锚定日 + 已有数据 → 增量窗口（不触发网络）
        catalog.execute(
            "INSERT INTO silver_prices_1d "
            "(asset_id, trade_date, open, high, low, close, volume, source) "
            "VALUES ('SZSE:000001', '2025-06-06', 10, 11, 9, 10, 1000, 'test')"
        )
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('akshare', 'shibor_overnight', '__MARKET__', '2025-06-05', 1.0, '2025-06-06')"
        )
        resp = client.post(
            f"{_BASE}/refresh",
            json={"keys": ["shibor_overnight", "no_such_indicator"]},
        )
        assert resp.status_code == 200
        results = {r["indicator_key"]: r for r in resp.json()["results"]}
        assert results["no_such_indicator"]["status"] == "error"
        assert results["shibor_overnight"]["status"] == "error"  # fetch boom
        assert results["shibor_overnight"]["source"] == "akshare"


# ── GET /external-indicators/runs ────────────────────────────────────────────


class TestGetRuns:
    def test_order_desc_and_field_passthrough(self, client, catalog):
        _insert_run(catalog, started_at_expr="NOW() - INTERVAL 3 HOUR")
        _insert_run(catalog, key="shibor_overnight", started_at_expr="NOW() - INTERVAL 1 HOUR")
        body = client.get(f"{_BASE}/runs").json()
        items = body["items"]
        assert body["total"] == 2
        assert items[0]["indicator_key"] == "shibor_overnight"  # 最新在前
        first = items[0]
        for f in ("run_id", "indicator_key", "source_name", "started_at",
                  "finished_at", "status", "trigger", "interrupted"):
            assert f in first, f
        assert first["source_name"] == "akshare"
        assert first["status"] == "ok"
        assert first["trigger"] == "manual"

    def test_key_exact_filter(self, client, catalog):
        _insert_run(catalog, key="north_net_buy")
        _insert_run(catalog, key="shibor_overnight")
        body = client.get(f"{_BASE}/runs", params={"key": "north_net_buy"}).json()
        assert body["total"] == 1
        assert body["items"][0]["indicator_key"] == "north_net_buy"

    def test_limit(self, client, catalog):
        for i in range(5):
            _insert_run(catalog, started_at_expr=f"NOW() - INTERVAL {i} HOUR")
        body = client.get(f"{_BASE}/runs", params={"limit": 3}).json()
        assert body["total"] == 3

    def test_interrupted_annotation(self, client, catalog):
        """2 小时前的 running → interrupted=True；刚启动的 running 不标注。"""
        _insert_run(catalog, status="running", started_at_expr="NOW() - INTERVAL 2 HOUR")
        _insert_run(catalog, key="shibor_overnight", status="running",
                    started_at_expr="NOW()")
        _insert_run(catalog, key="usd_cny_parity", status="ok",
                    started_at_expr="NOW() - INTERVAL 3 HOUR")
        body = client.get(f"{_BASE}/runs").json()
        by_key = {i["indicator_key"]: i for i in body["items"]}
        assert by_key["north_net_buy"]["interrupted"] is True
        assert by_key["north_net_buy"]["status"] == "running"  # 渲染层标注，不改库
        assert by_key["shibor_overnight"]["interrupted"] is False
        assert by_key["usd_cny_parity"]["interrupted"] is False  # 非 running 不标

    def test_db_not_mutated_by_annotation(self, client, catalog):
        _insert_run(catalog, status="running", started_at_expr="NOW() - INTERVAL 5 HOUR")
        client.get(f"{_BASE}/runs")
        row = catalog.query(
            "SELECT status FROM silver_external_indicator_refresh_log "
            "WHERE indicator_key = 'north_net_buy'"
        ).row(0, named=True)
        assert row["status"] == "running"

    def test_empty(self, client):
        body = client.get(f"{_BASE}/runs").json()
        assert body == {"items": [], "total": 0}


# ── sync def 检查（T3 移交：长任务不得挂 event loop）────────────────────────


class TestSyncDef:
    def _handler(self, path: str, method: str):
        route = next(
            r for r in app.routes
            if getattr(r, "path", "") == f"/api/v1/datasets/external-indicators{path}"
            and method in r.methods
        )
        return route.endpoint

    def test_refresh_endpoint_sync_def(self):
        assert not inspect.iscoroutinefunction(self._handler("/refresh", "POST"))

    def test_runs_endpoint_sync_def(self):
        assert not inspect.iscoroutinefunction(self._handler("/runs", "GET"))

    def test_enable_endpoint_sync_def(self):
        assert not inspect.iscoroutinefunction(
            self._handler("/builtins/{indicator_key}/enable", "POST")
        )
