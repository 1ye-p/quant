"""custom_http 目录创建 + 连接测试端点测试（P3-4）：
``POST /external-indicators/catalog`` / ``POST /external-indicators/test`` /
PATCH source_config 扩展 / GET 回显脱敏。

零真实网络：DNS 通过 monkeypatch ``socket.getaddrinfo`` 打桩（走真实
``validate_url`` 代码路径），HTTP 层 monkeypatch datasets 模块的
``guarded_fetch`` 符号。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.api_server.routes import datasets as datasets_routes
from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.indicator_sources.http_guard import GuardError

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BASE = "/api/v1/datasets/external-indicators"

_PUBLIC_IP = "93.184.216.34"


def _dns(ip: str):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake_getaddrinfo


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _config(**overrides) -> dict:
    cfg = {
        "method": "GET",
        "url_template": "https://api.example.com/macroe/indicator?date={date}",
        "params": {},
        "date_param_style": "yyyymmdd",
        "extraction": {
            "type": "jsonpath",
            "records_path": "$.data.records[*]",
            "field_map": {"trade_date": "date", "value": "close"},
        },
    }
    cfg.update(overrides)
    return cfg


def _create_body(**overrides) -> dict:
    body = {
        "indicator_key": "my_http_indicator",
        "display_name": "自定义 HTTP 指标",
        "frequency": "daily",
        "available_date_rule": "B",
        "source_config": _config(),
    }
    body.update(overrides)
    return body


def _catalog_row(catalog: Catalog, key: str) -> dict:
    return catalog.query(
        "SELECT source_type, source_name, source_config, frequency, enabled "
        "FROM silver_external_indicator_catalog WHERE indicator_key = ?",
        [key],
    ).row(0, named=True)


def _counts(catalog: Catalog) -> tuple[int, int]:
    n_cat = catalog.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicator_catalog"
    )["n"][0]
    n_data = catalog.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicators"
    )["n"][0]
    return n_cat, n_data


def _seed_custom_http(catalog: Catalog, key: str = "my_http_indicator") -> None:
    catalog.execute(
        "INSERT INTO silver_external_indicator_catalog "
        "(indicator_key, display_name, source_type, source_name, source_config, "
        " frequency) VALUES (?, ?, 'custom_http', 'custom_http', ?, 'daily')",
        [key, key, json.dumps(_config())],
    )


# ── POST /catalog（custom_http 创建）─────────────────────────────────────────


class TestCreateCustomHTTP:
    def test_create_ok(self, client, catalog, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        resp = client.post(f"{_BASE}/catalog", json=_create_body())
        assert resp.status_code == 201, resp.text
        entry = resp.json()
        assert entry["source_type"] == "custom_http"
        assert entry["enabled"] is True

        # 目录行：source_config 存原文 JSON（脱敏只在回显）
        row = _catalog_row(catalog, "my_http_indicator")
        assert row["source_type"] == "custom_http"
        stored = json.loads(row["source_config"])
        assert (
            stored["url_template"]
            == "https://api.example.com/macroe/indicator?date={date}"
        )

    def test_create_bad_scheme_400(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        resp = client.post(
            f"{_BASE}/catalog",
            json=_create_body(source_config=_config(url_template="ftp://x/y")),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "config_invalid"

    def test_create_bad_jsonpath_400(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        bad = _config()
        bad["extraction"]["records_path"] = "$.data.records[*(]"
        resp = client.post(f"{_BASE}/catalog", json=_create_body(source_config=bad))
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "config_invalid"

    def test_create_ssrf_url_400(self, client, catalog, monkeypatch):
        # 私网解析 → validate_url 预检拒绝（保存前，不等首次刷新）
        monkeypatch.setattr(socket, "getaddrinfo", _dns("192.168.1.1"))
        resp = client.post(f"{_BASE}/catalog", json=_create_body())
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "ssrf_blocked"
        # 未落库
        assert _counts(catalog) == (0, 0)

    def test_create_duplicate_key_409(self, client, catalog, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        assert client.post(f"{_BASE}/catalog", json=_create_body()).status_code == 201
        resp = client.post(f"{_BASE}/catalog", json=_create_body())
        assert resp.status_code == 409

    def test_create_with_pinned_source_400(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        resp = client.post(
            f"{_BASE}/catalog", json=_create_body(pinned_source="akshare")
        )
        assert resp.status_code == 400
        assert "pinned_source" in resp.json()["detail"]

    def test_create_bad_frequency_400(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        resp = client.post(
            f"{_BASE}/catalog", json=_create_body(frequency="quarterly")
        )
        assert resp.status_code == 400

    def test_create_bad_key_400(self, client, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        resp = client.post(f"{_BASE}/catalog", json=_create_body(indicator_key="Bad-Key"))
        assert resp.status_code == 400


# ── POST /test（连接测试，不入库）────────────────────────────────────────────


_ROWS = [
    {"trade_date": "20250601", "value": 100.0 + i} for i in range(25)
]


class TestConnectionTest:
    def test_ok_sample_and_diagnostics(self, client, catalog, monkeypatch):
        monkeypatch.setattr(datasets_routes, "_validate_url", lambda *a, **k: None)
        monkeypatch.setattr(
            datasets_routes, "_guarded_fetch", lambda cfg, dates, **kw: list(_ROWS)
        )
        before = _counts(catalog)
        resp = client.post(f"{_BASE}/test", json={"source_config": _config()})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["sample"]) == 10  # ≤10 行截断
        assert body["sample"][0] == {"trade_date": "20250601", "value": 100.0}
        d = body["diagnostics"]
        assert "{date}" not in d["resolved_url"]
        assert d["status"] == "ok"
        assert d["rows_parsed"] == 25
        assert d["field_map_hit"] is True
        # 不入库：目录与数据表零变化
        assert _counts(catalog) == before

    def test_guard_error_ssrf_400_stage(self, client, monkeypatch):
        monkeypatch.setattr(datasets_routes, "_validate_url", lambda *a, **k: None)

        def boom(cfg, dates, **kw):
            raise GuardError("ssrf_blocked", "host resolves to 10.0.0.1 — blocked")

        monkeypatch.setattr(datasets_routes, "_guarded_fetch", boom)
        resp = client.post(f"{_BASE}/test", json={"source_config": _config()})
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "ssrf_blocked"

    def test_guard_error_timeout_400_stage(self, client, monkeypatch):
        monkeypatch.setattr(datasets_routes, "_validate_url", lambda *a, **k: None)

        def boom(cfg, dates, **kw):
            raise GuardError("timeout", "request timed out")

        monkeypatch.setattr(datasets_routes, "_guarded_fetch", boom)
        resp = client.post(f"{_BASE}/test", json={"source_config": _config()})
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "timeout"

    def test_invalid_config_400(self, client):
        resp = client.post(
            f"{_BASE}/test",
            json={"source_config": _config(url_template="not a url")},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "config_invalid"

    def test_no_db_writes_on_error(self, client, catalog, monkeypatch):
        monkeypatch.setattr(datasets_routes, "_validate_url", lambda *a, **k: None)

        def boom(cfg, dates, **kw):
            raise GuardError("http_error", "HTTP 500")

        monkeypatch.setattr(datasets_routes, "_guarded_fetch", boom)
        before = _counts(catalog)
        client.post(f"{_BASE}/test", json={"source_config": _config()})
        assert _counts(catalog) == before


# ── PATCH source_config 扩展 ─────────────────────────────────────────────────


class TestPatchSourceConfig:
    def test_patch_valid_source_config(self, client, catalog, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        _seed_custom_http(catalog)
        new_cfg = _config(url_template="https://api2.example.com/v2?d={date}")
        resp = client.patch(
            f"{_BASE}/catalog/my_http_indicator", json={"source_config": new_cfg}
        )
        assert resp.status_code == 200, resp.text
        stored = json.loads(_catalog_row(catalog, "my_http_indicator")["source_config"])
        assert stored["url_template"] == "https://api2.example.com/v2?d={date}"

    def test_patch_bad_source_config_400(self, client, catalog, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        _seed_custom_http(catalog)
        resp = client.patch(
            f"{_BASE}/catalog/my_http_indicator",
            json={"source_config": _config(url_template="ftp://x/y")},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "config_invalid"

    def test_patch_ssrf_source_config_400(self, client, catalog, monkeypatch):
        _seed_custom_http(catalog)
        monkeypatch.setattr(socket, "getaddrinfo", _dns("10.0.0.5"))
        resp = client.patch(
            f"{_BASE}/catalog/my_http_indicator", json={"source_config": _config()}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["stage"] == "ssrf_blocked"

    def test_patch_csv_row_rejected_400(self, client, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name) "
            "VALUES ('csv_key', 'csv_key', 'csv', 'test')"
        )
        resp = client.patch(
            f"{_BASE}/catalog/csv_key", json={"source_config": _config()}
        )
        assert resp.status_code == 400
        assert "source_config" in resp.json()["detail"]

    def test_patch_response_source_config_redacted(self, client, catalog, monkeypatch):
        """PATCH 成功回显必须脱敏：密文 token → ***redacted***，${VAR} 原样。"""
        monkeypatch.setattr(socket, "getaddrinfo", _dns(_PUBLIC_IP))
        _seed_custom_http(catalog)
        new_cfg = _config(
            headers={
                "Authorization": "Bearer sk-testsecret123456",
                "X-Api-Key": "${MY_TOKEN}",
            }
        )
        resp = client.patch(
            f"{_BASE}/catalog/my_http_indicator", json={"source_config": new_cfg}
        )
        assert resp.status_code == 200, resp.text
        sc = resp.json()["source_config"]
        # 明文 secret 形态 → ***redacted***
        assert sc["headers"]["Authorization"] == "***redacted***"
        # ${VAR} 引用原样回显（不含真实值）
        assert sc["headers"]["X-Api-Key"] == "${MY_TOKEN}"
        assert "sk-testsecret123456" not in resp.text
        # DB 存原文（脱敏只在回显）——与 create 行为一致
        stored = json.loads(_catalog_row(catalog, "my_http_indicator")["source_config"])
        assert stored["headers"]["Authorization"] == "Bearer sk-testsecret123456"

    def test_patch_builtin_row_rejected_400(self, client, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name) "
            "VALUES ('builtin_key', 'builtin_key', 'builtin', 'builtin')"
        )
        resp = client.patch(
            f"{_BASE}/catalog/builtin_key", json={"source_config": _config()}
        )
        assert resp.status_code == 400


# ── GET 回显脱敏 ─────────────────────────────────────────────────────────────


class TestEchoRedaction:
    def _seed_with_secret(self, catalog: Catalog) -> None:
        cfg = _config(
            headers={
                "Authorization": "Bearer sk-secret-token-1234567890",
                "X-Trace-Id": "trace-1",
                "X-Env-Key": "${MY_INDICATOR_TOKEN}",
            }
        )
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, source_config) "
            "VALUES ('sec_key', 'sec_key', 'custom_http', 'custom_http', ?)",
            [json.dumps(cfg)],
        )

    def test_detail_redacts_plaintext_keeps_env_ref(self, client, catalog):
        self._seed_with_secret(catalog)
        resp = client.get(f"{_BASE}/catalog/sec_key")
        assert resp.status_code == 200
        sc = resp.json()["source_config"]
        assert sc["headers"]["Authorization"] == "***redacted***"
        assert sc["headers"]["X-Env-Key"] == "${MY_INDICATOR_TOKEN}"
        assert sc["headers"]["X-Trace-Id"] == "trace-1"

    def test_db_stores_plaintext_not_redacted(self, client, catalog):
        self._seed_with_secret(catalog)
        stored = _catalog_row(catalog, "sec_key")["source_config"]
        assert "sk-secret-token-1234567890" in stored  # 原文入库

    def test_list_redacts_too(self, client, catalog):
        self._seed_with_secret(catalog)
        resp = client.get(f"{_BASE}/catalog")
        items = resp.json()["items"]
        assert len(items) == 1
        sc = items[0]["source_config"]
        assert sc["headers"]["Authorization"] == "***redacted***"

    def test_csv_row_source_config_untouched(self, client, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, source_config) "
            "VALUES ('plain_key', 'plain_key', 'csv', 'test', '{\"k\": 1}')"
        )
        resp = client.get(f"{_BASE}/catalog/plain_key")
        assert resp.json()["source_config"] == {"k": 1}
