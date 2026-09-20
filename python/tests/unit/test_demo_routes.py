"""Tests for demo / onboarding endpoints (POST /demo/seed, GET /demo/status)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_DIR = REPO_ROOT / "examples" / "demo_data"


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(tmp_path / "catalog.duckdb")
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    monkeypatch.setenv("CQUANT_DEMO_DATA_DIR", str(DEMO_DIR))
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app) as c:
        yield c
    app.dependency_overrides = {}


class TestDemoStatus:
    def test_status_unseeded(self, client: TestClient) -> None:
        resp = client.get("/api/v1/demo/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["seeded"] is False
        assert body["strategy_id"] is None

    def test_status_seeded(self, client: TestClient, catalog: Catalog) -> None:
        catalog.execute(
            "INSERT INTO silver_prices_1d (asset_id, trade_date, open, high, low, close,"
            " volume, source) VALUES ('SSE:600000', '2024-01-02', 1, 1, 1, 1, 1, 'demo')"
        )
        catalog.execute(
            "INSERT INTO meta_strategy_configs (strategy_id, config_format, config_text,"
            " created_at, updated_at) VALUES ('demo_momentum_top10', 'json', '{}', now(), now())"
        )
        resp = client.get("/api/v1/demo/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["seeded"] is True
        assert body["strategy_id"] == "demo_momentum_top10"
        assert body["dataset_version"] == "demo_synthetic_v1"


class TestDemoSeed:
    def test_seed_idempotent(self, client: TestClient, catalog: Catalog) -> None:
        resp = client.post("/api/v1/demo/seed")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["seeded"] is True
        assert body["dataset_version"] == "demo_synthetic_v1"
        assert body["strategy_id"] == "demo_momentum_top10"
        assert body["prices"]["assets"] == 50
        assert body["prices"]["rows"] == 50 * 480
        # strategy config persisted as DSL
        cfg = catalog.query(
            "SELECT parsed_config FROM meta_strategy_configs WHERE strategy_id = 'demo_momentum_top10'"
        )
        import json

        parsed = json.loads(cfg["parsed_config"][0])
        assert parsed["strategy_type"] == "DSL"
        assert parsed["dsl_spec"]["score"][0]["factor"] == "ret_20d"
        # external indicator imported
        ind = catalog.query(
            "SELECT COUNT(*) AS cnt FROM silver_external_indicators"
            " WHERE indicator_key = 'market_breadth'"
        )
        assert ind["cnt"][0] > 0

        # Second call (idempotent) succeeds with the same stats
        resp2 = client.post("/api/v1/demo/seed")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["prices"]["rows"] == body["prices"]["rows"]

        # status now reports seeded
        status = client.get("/api/v1/demo/status").json()
        assert status["seeded"] is True

    def test_seed_materializes_ret_20d(self, client: TestClient, catalog: Catalog) -> None:
        resp = client.post("/api/v1/demo/seed")
        fsv = resp.json()["feature_set_version"]
        assert fsv, "expected ret_20d factor materialization to succeed"
        factors = catalog.query(
            "SELECT COUNT(*) AS cnt FROM gold_factor_values"
            " WHERE feature_set_version = ? AND factor_name = 'ret_20d'",
            [fsv],
        )
        assert factors["cnt"][0] > 0
