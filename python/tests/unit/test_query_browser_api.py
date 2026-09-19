"""Tests for the read-only data browser query endpoint + corporate-actions.

Covers: table whitelist enforcement, column validation (injection guard),
operator whitelist, parameterized values, LIMIT cap, coverage preset, and
the per-asset corporate-actions history endpoint (empty table tolerated).
"""

from __future__ import annotations

from datetime import date

import duckdb
import polars as pl
import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app


class _FakeCatalog:
    """Minimal Catalog stand-in backed by an in-memory DuckDB."""

    def __init__(self) -> None:
        self.conn = duckdb.connect(":memory:")

    def query(self, sql: str, params: list | None = None) -> pl.DataFrame:
        res = self.conn.execute(sql, params or [])
        cols = [d[0] for d in res.description]
        return pl.DataFrame(res.fetchall(), schema=cols, orient="row")

    def execute(self, sql: str, params: list | None = None) -> None:
        self.conn.execute(sql, params or [])


@pytest.fixture()
def catalog() -> _FakeCatalog:
    cat = _FakeCatalog()
    cat.execute(
        """
        CREATE TABLE silver_prices_1d (
            asset_id VARCHAR, trade_date DATE, close DOUBLE, volume DOUBLE
        )
        """
    )
    cat.execute(
        """
        INSERT INTO silver_prices_1d VALUES
            ('SZSE:000001', '2025-01-02', 10.0, 1000),
            ('SZSE:000001', '2025-06-30', 11.0, 1100),
            ('SSE:600036',  '2025-01-02', 30.0, 2000),
            ('CNI:881001',  '2025-01-02', 4000.0, 0)
        """
    )
    cat.execute(
        """
        CREATE TABLE silver_corporate_actions (
            action_id VARCHAR, asset_id VARCHAR, action_type VARCHAR,
            ex_date DATE, record_date DATE, pay_date DATE, ratio DOUBLE,
            cash_amount DOUBLE, currency VARCHAR, description VARCHAR,
            source VARCHAR
        )
        """
    )
    cat.execute(
        """
        INSERT INTO silver_corporate_actions VALUES
            ('a1', 'SSE:600036', 'cash_dividend', '2025-06-15',
             '2025-06-16', '2025-06-20', NULL, 1.30, 'CNY', '2024年度分红', 'tushare')
        """
    )
    return cat


@pytest.fixture()
def client(catalog: _FakeCatalog):
    app.dependency_overrides[deps.get_catalog] = lambda: catalog
    app.dependency_overrides[deps.get_kb_service] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


class TestStructuredQuery:
    def test_tables_whitelist_listed(self, client: TestClient) -> None:
        resp = client.get("/api/v1/query/tables")
        assert resp.status_code == 200
        assert "silver_prices_1d" in resp.json()["tables"]

    def test_basic_select_star(self, client: TestClient) -> None:
        resp = client.post("/api/v1/query", json={"table": "silver_prices_1d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert "asset_id" in body["columns"]

    def test_table_whitelist_rejects_unknown(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query", json={"table": "silver_dataset_versionsx"}
        )
        assert resp.status_code == 400
        resp = client.post("/api/v1/query", json={"table": "gold_backtest_runs"})
        assert resp.status_code == 400  # not in the read whitelist

    def test_column_injection_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d",
                  "columns": ["close; DROP TABLE silver_prices_1d"]},
        )
        assert resp.status_code == 400

    def test_unknown_column_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d", "columns": ["not_a_col"]},
        )
        assert resp.status_code == 400

    def test_where_value_parameterized_not_sql(self, client: TestClient) -> None:
        # The value is a literal string containing SQL — it must never execute.
        resp = client.post(
            "/api/v1/query",
            json={
                "table": "silver_prices_1d",
                "where": [{"col": "asset_id", "op": "=", "val": "' OR 1=1 --"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0  # no row matches the literal

    def test_where_ops(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query",
            json={
                "table": "silver_prices_1d",
                "where": [{"col": "asset_id", "op": "IN",
                           "val": ["SZSE:000001", "SSE:600036"]}],
                "order_by": [{"col": "asset_id", "dir": "asc"}],
            },
        )
        body = resp.json()
        assert body["total"] == 3
        assert body["rows"][0]["asset_id"] == "SSE:600036"

        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d",
                  "where": [{"col": "close", "op": ">=", "val": 30.0}]},
        )
        assert resp.json()["total"] == 2  # SSE:600036 + index 881001

        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d",
                  "where": [{"col": "volume", "op": "IS NOT NULL"}]},
        )
        assert resp.json()["total"] == 4

    def test_bad_op_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d",
                  "where": [{"col": "close", "op": "; DROP TABLE x", "val": 1}]},
        )
        assert resp.status_code == 400

    def test_limit_capped(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query", json={"table": "silver_prices_1d", "limit": 5000}
        )
        # Pydantic ge/le rejects 5000 outright
        assert resp.status_code == 422

    def test_like_op_works(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query",
            json={"table": "silver_prices_1d",
                  "where": [{"col": "asset_id", "op": "LIKE", "val": "%:88%"}]},
        )
        assert resp.json()["total"] == 1

    def test_table_in_whitelist_but_absent_returns_empty(self, client: TestClient) -> None:
        resp = client.post("/api/v1/query", json={"table": "gold_factor_values"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestCoveragePreset:
    def test_coverage_excludes_indices_by_default(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query/coverage",
            json={"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["n_assets"] == 2  # 000001 + 600036, CNI:881001 excluded
        assert "CNI:881001" not in body["sample_assets"]

    def test_coverage_include_indices(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query/coverage",
            json={"start_date": "2025-01-01", "end_date": "2025-12-31",
                  "include_indices": True},
        )
        assert resp.json()["n_assets"] == 3

    def test_coverage_empty_window(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query/coverage",
            json={"start_date": "2030-01-01", "end_date": "2030-12-31"},
        )
        assert resp.json()["n_assets"] == 0
        assert resp.json()["sample_assets"] == []

    def test_coverage_reversed_dates_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/query/coverage",
            json={"start_date": "2025-12-31", "end_date": "2025-01-01"},
        )
        assert resp.status_code == 400


class TestCorporateActionsEndpoint:
    def test_per_asset_history(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/datasets/corporate-actions", params={"asset_id": "SSE:600036"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["action_type"] == "cash_dividend"
        assert body["items"][0]["ex_date"] == "2025-06-15"
        assert body["items"][0]["cash_amount"] == 1.30

    def test_unknown_asset_returns_empty(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/datasets/corporate-actions", params={"asset_id": "SSE:999999"}
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []
