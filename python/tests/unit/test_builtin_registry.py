"""内置指标注册表 + builtins/enable 端点测试（P2-1）。

覆盖：注册表完整性（12 条、值域、与 spike 清单对照）、tushare_ready 双读、
GET builtins 就绪态形状 + enabled 对齐、enable 写目录行/幂等/404/backfill
pending 分支、monthly 频率扩展（容差 + PATCH）。
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.indicator_sources.builtin_registry import (
    BUILTIN_CATALOG,
    BuiltinIndicatorDef,
    tushare_ready,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BASE = "/api/v1/datasets/external-indicators"

# spike 文档「锁定首批清单（12 个）」的 key 全集（一一对应，勿增删）
_SPIKE_KEYS = {
    "margin_fin_balance_sse",
    "margin_total_balance_sse",
    "margin_balance_szse",
    "north_net_buy",
    "north_acc_net_buy",
    "market_pe_all",
    "index_pe_sse50_ttm",
    "shibor_overnight",
    "cn_gov_yield_10y",
    "cn_yield_curve_10y2y",
    "usd_cny_parity",
    "usd_cny_boc",
}


# ── 注册表完整性 ─────────────────────────────────────────────────────────────


class TestBuiltinCatalog:
    def test_twelve_entries_matching_spike_keys(self):
        assert len(BUILTIN_CATALOG) == 12
        assert {d.indicator_key for d in BUILTIN_CATALOG} == _SPIKE_KEYS

    def test_key_regex_and_value_domains(self):
        import re

        pat = re.compile(r"^[a-z_0-9]+$")
        for d in BUILTIN_CATALOG:
            assert pat.match(d.indicator_key), d.indicator_key
            assert d.candidates, d.indicator_key
            assert set(d.candidates) <= {"tushare", "akshare"}, d.indicator_key
            assert d.available_date_rule == "B", d.indicator_key
            assert d.frequency in {"daily", "weekly", "monthly"}, d.indicator_key
            assert d.default_backfill_years == 2, d.indicator_key
            assert d.display_name and d.description

    def test_spot_check_three_entries(self):
        by_key = {d.indicator_key: d for d in BUILTIN_CATALOG}
        # 1) shibor：akshare 首选 + tushare 备选（spike 唯一 tushare 备选项）
        shibor = by_key["shibor_overnight"]
        assert shibor.candidates == ("akshare", "tushare")
        assert shibor.frequency == "daily"
        # 2) 两个月频估值指标 → monthly
        assert by_key["market_pe_all"].frequency == "monthly"
        assert by_key["index_pe_sse50_ttm"].frequency == "monthly"
        # 3) north_acc_net_buy：单位万亿元 + 注记进 description
        acc = by_key["north_acc_net_buy"]
        assert acc.unit == "万亿元"
        assert "万亿" in acc.description
        # 其余指标 akshare 单候选
        assert by_key["north_net_buy"].candidates == ("akshare",)

    def test_frozen_dataclass(self):
        d = BUILTIN_CATALOG[0]
        with pytest.raises(Exception):
            d.indicator_key = "x"  # type: ignore[misc]


# ── tushare_ready 双读 ───────────────────────────────────────────────────────


def _patch_settings_token(monkeypatch, value: str) -> None:
    """settings.tushare_token 是只读 property（委托 data_source），patch 类属性。"""
    from cquant.core.config import settings

    monkeypatch.setattr(
        type(settings), "tushare_token", property(lambda self: value)
    )


class TestTushareReady:
    def test_settings_priority(self, monkeypatch):
        _patch_settings_token(monkeypatch, "from-settings")
        monkeypatch.setenv("TUSHARE_TOKEN", "from-env")
        assert tushare_ready() is True

    def test_env_fallback(self, monkeypatch):
        _patch_settings_token(monkeypatch, "")
        monkeypatch.setenv("TUSHARE_TOKEN", "from-env")
        assert tushare_ready() is True

    def test_both_absent_false(self, monkeypatch):
        _patch_settings_token(monkeypatch, "")
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
        assert tushare_ready() is False


# ── API fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    _patch_settings_token(monkeypatch, "")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _seed_anchor(catalog: Catalog, anchor: str = "2025-06-06") -> None:
    catalog.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, source) "
        f"VALUES ('SZSE:000001', '{anchor}', 10, 11, 9, 10, 1000, 'test')"
    )


# ── GET /builtins ────────────────────────────────────────────────────────────


class TestGetBuiltins:
    def test_shape_and_ready_flags(self, client):
        resp = client.get(f"{_BASE}/builtins")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 12
        items = {i["indicator_key"]: i for i in body["items"]}
        # akshare 恒 ready；tushare ready = tushare_ready()（fixture 里已摘 token → False）
        north = items["north_net_buy"]
        assert north["candidates"] == [{"name": "akshare", "ready": True}]
        assert north["enabled"] is False  # 目录无该 key
        assert north["frequency"] == "daily"
        shibor = items["shibor_overnight"]
        assert shibor["candidates"] == [
            {"name": "akshare", "ready": True},
            {"name": "tushare", "ready": False},
        ]
        assert items["market_pe_all"]["frequency"] == "monthly"

    def test_enabled_aligns_with_catalog(self, client, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, enabled) "
            "VALUES ('north_net_buy', 'x', 'csv', 's', TRUE)"
        )
        body = client.get(f"{_BASE}/builtins").json()
        items = {i["indicator_key"]: i for i in body["items"]}
        assert items["north_net_buy"]["enabled"] is True
        assert items["shibor_overnight"]["enabled"] is False

    def test_tushare_ready_flag_follows_token(self, client, monkeypatch):
        monkeypatch.setenv("TUSHARE_TOKEN", "tok")
        body = client.get(f"{_BASE}/builtins").json()
        shibor = {i["indicator_key"]: i for i in body["items"]}["shibor_overnight"]
        assert shibor["candidates"][1] == {"name": "tushare", "ready": True}


# ── POST /builtins/{key}/enable ──────────────────────────────────────────────


class TestEnableBuiltin:
    def test_enable_writes_catalog_row(self, client, catalog):
        _seed_anchor(catalog, "2025-06-06")
        resp = client.post(f"{_BASE}/builtins/market_pe_all/enable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["indicator_key"] == "market_pe_all"
        assert body["backfill"] == "pending"  # T3 未交付 → ImportError 分支
        row = catalog.query(
            "SELECT source_type, source_name, pinned_source, frequency, "
            "available_date_rule, enabled, backfill_start, display_name "
            "FROM silver_external_indicator_catalog WHERE indicator_key = ?",
            ["market_pe_all"],
        ).row(0, named=True)
        assert row["source_type"] == "builtin"
        assert row["source_name"] == "builtin"
        assert row["pinned_source"] is None
        assert row["frequency"] == "monthly"
        assert row["available_date_rule"] == "B"
        assert row["enabled"] is True
        assert row["display_name"] == "全A平均市盈率"
        # 默认 backfill_start = 锚定日(2025-06-06) − 2 年
        assert str(row["backfill_start"]) == "2023-06-06"

    def test_enable_explicit_backfill_start(self, client, catalog):
        resp = client.post(
            f"{_BASE}/builtins/shibor_overnight/enable",
            json={"backfill_start": "2024-01-01"},
        )
        assert resp.status_code == 200
        row = catalog.query(
            "SELECT backfill_start, frequency FROM silver_external_indicator_catalog "
            "WHERE indicator_key = ?",
            ["shibor_overnight"],
        ).row(0, named=True)
        assert str(row["backfill_start"]) == "2024-01-01"
        assert row["frequency"] == "daily"

    def test_enable_idempotent(self, client, catalog):
        _seed_anchor(catalog)
        r1 = client.post(f"{_BASE}/builtins/north_net_buy/enable")
        r2 = client.post(f"{_BASE}/builtins/north_net_buy/enable")
        assert r1.status_code == 200 and r2.status_code == 200
        n = catalog.query(
            "SELECT COUNT(*) AS n FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'north_net_buy'"
        )["n"][0]
        assert n == 1

    def test_enable_unknown_key_404(self, client):
        resp = client.post(f"{_BASE}/builtins/no_such_indicator/enable")
        assert resp.status_code == 404

    def test_enable_backfill_wired_when_module_present(self, client, catalog, monkeypatch):
        """T3 落地后链路：fake refresh 模块被调用且摘要透传。"""
        calls: list[dict] = []

        def fake_refresh(cat, keys, backfill, trigger):
            calls.append({"keys": keys, "backfill": backfill, "trigger": trigger})
            return {"ok": True, "refreshed": list(keys)}

        fake_mod = types.ModuleType(
            "cquant.datahub.pipelines.indicator_sources.refresh"
        )
        fake_mod.run_external_indicator_refresh = fake_refresh
        monkeypatch.setitem(
            sys.modules, "cquant.datahub.pipelines.indicator_sources.refresh", fake_mod
        )
        resp = client.post(f"{_BASE}/builtins/usd_cny_parity/enable")
        assert resp.status_code == 200
        assert resp.json()["backfill"] == {"ok": True, "refreshed": ["usd_cny_parity"]}
        assert calls == [
            {"keys": ["usd_cny_parity"], "backfill": True, "trigger": "manual"}
        ]

    def test_enable_flips_get_builtins_enabled(self, client):
        client.post(f"{_BASE}/builtins/cn_gov_yield_10y/enable")
        body = client.get(f"{_BASE}/builtins").json()
        item = {i["indicator_key"]: i for i in body["items"]}["cn_gov_yield_10y"]
        assert item["enabled"] is True


# ── monthly 频率扩展 ─────────────────────────────────────────────────────────


class TestMonthlyFrequency:
    def test_monthly_stale_tolerance_25_trading_days(self, catalog):
        """monthly 容差 25 交易日：20 个交易日滞后 → 不 stale；26 → stale。"""
        from cquant.datahub.pipelines.indicator_catalog import (
            STALE_TOLERANCE_TRADING_DAYS,
            list_catalog,
        )

        assert STALE_TOLERANCE_TRADING_DAYS["monthly"] == 25
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, frequency) "
            "VALUES ('m_pe', 'm_pe', 'csv', 's', 'monthly')"
        )
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('s', 'm_pe', '__MARKET__', '2025-05-06', 1.0, '2025-05-07')"
        )
        # 26 个交易日锚点窗口：2025-05-07 起 26 个连续工作日
        days = [
            f"2025-05-{d:02d}" for d in range(7, 31)
        ] + ["2025-06-02", "2025-06-03"]  # 24 + 2 = 26 个交易日
        for d in days:
            catalog.execute(
                "INSERT INTO silver_prices_1d "
                "(asset_id, trade_date, open, high, low, close, volume, source) "
                f"VALUES ('SZSE:000001', '{d}', 10, 11, 9, 10, 1000, 'test')"
            )
        item = {r["indicator_key"]: r for r in list_catalog(catalog)}["m_pe"]
        assert item["stale"] is True  # 26 交易日滞后 > 25

    def test_monthly_within_tolerance_not_stale(self, catalog):
        from cquant.datahub.pipelines.indicator_catalog import list_catalog

        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, frequency) "
            "VALUES ('m_pe', 'm_pe', 'csv', 's', 'monthly')"
        )
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('s', 'm_pe', '__MARKET__', '2025-05-06', 1.0, '2025-05-07')"
        )
        for d in [f"2025-05-{x:02d}" for x in range(7, 31)][:20]:  # 20 交易日滞后
            catalog.execute(
                "INSERT INTO silver_prices_1d "
                "(asset_id, trade_date, open, high, low, close, volume, source) "
                f"VALUES ('SZSE:000001', '{d}', 10, 11, 9, 10, 1000, 'test')"
            )
        item = {r["indicator_key"]: r for r in list_catalog(catalog)}["m_pe"]
        assert item["stale"] is False  # 20 ≤ 25

    def test_patch_monthly_now_allowed(self, client, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, frequency) "
            "VALUES ('k1', 'k1', 'csv', 's', 'daily')"
        )
        resp = client.patch(
            f"{_BASE}/catalog/k1", json={"frequency": "monthly"}
        )
        assert resp.status_code == 200
        assert resp.json()["frequency"] == "monthly"
