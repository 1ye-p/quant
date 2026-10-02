"""Startup migration: app lifespan backfills external indicator catalog (P1-4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.indicator_catalog import list_catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _insert_indicator(catalog: Catalog, source: str, key: str, d: date) -> None:
    catalog.execute(
        "INSERT INTO silver_external_indicators "
        "(source, indicator_key, asset_id, trade_date, value, available_date) "
        "VALUES (?, ?, '__MARKET__', ?, 1.0, ?)",
        [source, key, d, d],
    )


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    cat = _make_catalog(tmp_path)
    _insert_indicator(cat, "src_a", "gdp_yoy", date(2026, 1, 5))
    _insert_indicator(cat, "src_b", "cpi_yoy", date(2026, 1, 6))
    return cat


@pytest.fixture()
def client(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    # lifespan resolves these at call time via `from ... import`, so patching
    # the module attributes is enough — no real scheduler / real DB touched.
    monkeypatch.setattr(deps, "get_catalog", lambda: catalog)
    import cquant.api_server.data_scheduler as ds

    monkeypatch.setattr(ds, "start_data_scheduler", lambda cat: None)
    with TestClient(app) as c:
        yield c


def test_startup_backfills_catalog_from_data(client: TestClient, catalog: Catalog):
    keys = {r["indicator_key"] for r in list_catalog(catalog)}
    assert keys == {"gdp_yoy", "cpi_yoy"}
    for row in list_catalog(catalog):
        assert row["source_type"] == "csv"
        assert row["last_status"] == "never_run"


def test_startup_survives_backfill_failure(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    """Migration failure must never block app startup (warning only)."""
    import cquant.api_server.data_scheduler as ds
    import cquant.datahub.pipelines.indicator_catalog as ic

    def _boom(_catalog):
        raise RuntimeError("migration exploded")

    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    monkeypatch.setattr(deps, "get_catalog", lambda: catalog)
    monkeypatch.setattr(ds, "start_data_scheduler", lambda cat: None)
    monkeypatch.setattr(ic, "backfill_catalog_from_data", _boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_startup_migration_idempotent(
    client: TestClient, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    """Second startup inserts nothing and keeps rows intact."""
    import cquant.api_server.data_scheduler as ds

    monkeypatch.setattr(ds, "start_data_scheduler", lambda cat: None)
    with TestClient(app):
        pass
    rows = list_catalog(catalog)
    assert len(rows) == 2
