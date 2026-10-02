"""External indicator CSV importer unit tests (P1-3 catalog write-through)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.external_indicator_importer import (
    ExternalIndicatorImporter,
    ImportConfig,
)
from cquant.datahub.pipelines.indicator_catalog import get_catalog_entry

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _config(**overrides) -> ImportConfig:
    cfg = ImportConfig(
        source="test_src",
        indicator_key="gdp_yoy",
        column_map={"date": "trade_date", "value": "value"},
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _frame() -> pl.DataFrame:
    return pl.DataFrame({"date": ["2026-01-05", "2026-02-10"], "value": [1.0, 2.0]})


# ── catalog write-through (P1-3) ────────────────────────────────────────────


def test_import_frame_writes_catalog_row(tmp_path):
    cat = _make_catalog(tmp_path)
    importer = ExternalIndicatorImporter(cat)

    report = importer.import_frame(_frame(), _config(available_date_rule="A"))

    assert report.inserted == 2
    entry = get_catalog_entry(cat, "gdp_yoy")
    assert entry is not None
    assert entry["source_type"] == "csv"
    assert entry["source_name"] == "test_src"
    assert entry["available_date_rule"] == "A"
    assert entry["display_name"] == "gdp_yoy"
    assert entry["last_status"] == "ok"
    assert entry["last_refresh_at"] is not None


def test_reimport_updates_existing_catalog_row(tmp_path):
    cat = _make_catalog(tmp_path)
    importer = ExternalIndicatorImporter(cat)

    importer.import_frame(_frame(), _config())
    first = get_catalog_entry(cat, "gdp_yoy")

    # second import: no error, no duplicate row, refresh fields updated
    report = importer.import_frame(_frame(), _config())
    assert report.inserted == 0  # all rows already present (deduped)

    n = cat.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicator_catalog "
        "WHERE indicator_key = 'gdp_yoy'"
    ).item(0, "n")
    assert n == 1

    second = get_catalog_entry(cat, "gdp_yoy")
    assert second["last_status"] == "ok"
    assert second["last_refresh_at"] is not None
    assert second["last_refresh_at"] >= first["last_refresh_at"]
