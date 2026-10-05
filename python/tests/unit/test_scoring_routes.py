"""Tests for /scoring/run config validation (fill_null enum, A3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(tmp_path / "catalog.duckdb")
    cat.initialize()
    return cat


@pytest.fixture()
def client(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    # Patch the cached factory too — app lifespan calls get_catalog() directly
    # (outside DI), which would otherwise hit the real data/catalog.duckdb.
    monkeypatch.setattr(deps, "_get_catalog", lambda: catalog)
    monkeypatch.setattr(deps, "close_catalog", lambda: False)
    # scoring.py caches "tables ensured" at module level per process — reset so
    # each test's fresh tmp catalog actually gets its DDL.
    from cquant.api_server.routes import scoring as scoring_routes

    monkeypatch.setattr(scoring_routes, "_tables_ensured", False)
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    with TestClient(app) as c:
        yield c
    app.dependency_overrides = {}


def _body(**overrides) -> dict:
    body = {
        "name": "t",
        "factors": [{"factor_name": "f1", "weight": 1.0, "direction": "long"}],
        "feature_set_version": "v1",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
    }
    body.update(overrides)
    return body


class TestFillNullEnum:
    def test_invalid_fill_null_returns_422(self, client: TestClient):
        resp = client.post("/api/v1/scoring/run", json=_body(fill_null="bogus"))
        assert resp.status_code == 422

    def test_all_four_modes_accepted(self, client: TestClient):
        for mode in ("median", "mean", "zero", "risk_penalty"):
            resp = client.post("/api/v1/scoring/run", json=_body(fill_null=mode))
            assert resp.status_code == 200, mode
            assert resp.json()["status"] == "pending"

    def test_penalty_per_missing_accepted(self, client: TestClient):
        resp = client.post(
            "/api/v1/scoring/run",
            json=_body(fill_null="risk_penalty", penalty_per_missing=0.25),
        )
        assert resp.status_code == 200

    def test_penalty_per_missing_invalid_type_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/scoring/run",
            json=_body(fill_null="risk_penalty", penalty_per_missing="abc"),
        )
        assert resp.status_code == 422

    def test_exclude_not_accepted_in_scorer(self, client: TestClient):
        """exclude 仅回测策略侧（multi_factor）支持，scorer 侧 422。"""
        resp = client.post("/api/v1/scoring/run", json=_body(fill_null="exclude"))
        assert resp.status_code == 422
