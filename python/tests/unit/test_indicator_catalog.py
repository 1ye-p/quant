"""External indicator catalog repository (P1-2) unit tests."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.indicator_catalog import (
    CatalogEntryInput,
    backfill_catalog_from_data,
    delete_catalog_entry,
    get_catalog_entry,
    list_catalog,
    mark_refresh_result,
    upsert_catalog_entry,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _insert_price(catalog: Catalog, d: date) -> None:
    catalog.execute(
        "INSERT INTO silver_prices_1d (asset_id, trade_date, open, high, low, "
        "close, volume, source) VALUES ('SSE:600000', ?, 1, 1, 1, 1, 0, 'test') "
        "ON CONFLICT DO NOTHING",
        [d],
    )


def _insert_indicator(
    catalog: Catalog,
    source: str,
    key: str,
    d: date,
    updated_at: str,
    value: float = 1.0,
) -> None:
    catalog.execute(
        "INSERT INTO silver_external_indicators "
        "(source, indicator_key, asset_id, trade_date, value, available_date, updated_at) "
        "VALUES (?, ?, '__MARKET__', ?, ?, ?, ?) "
        "ON CONFLICT (source, indicator_key, asset_id, trade_date) DO UPDATE "
        "SET value = excluded.value, updated_at = excluded.updated_at",
        [source, key, d, value, d, updated_at],
    )


# ── backfill ────────────────────────────────────────────────────────────────


def test_backfill_migrates_existing_csv_keys_idempotent(tmp_path):
    cat = _make_catalog(tmp_path)
    # same key from two sources: source 'b' has the latest updated_at row
    _insert_indicator(cat, "src_a", "gdp_yoy", date(2026, 1, 5), "2026-01-06 10:00:00")
    _insert_indicator(cat, "src_b", "gdp_yoy", date(2026, 1, 6), "2026-02-01 09:00:00")
    _insert_indicator(cat, "src_a", "cpi_yoy", date(2026, 1, 7), "2026-01-08 10:00:00")

    inserted = backfill_catalog_from_data(cat)
    assert inserted == 2

    rows = {r["indicator_key"]: r for r in list_catalog(cat)}
    assert set(rows) == {"gdp_yoy", "cpi_yoy"}
    gdp = rows["gdp_yoy"]
    assert gdp["source_type"] == "csv"
    assert gdp["source_name"] == "src_b"  # latest updated_at wins
    assert gdp["display_name"] == "gdp_yoy"
    assert gdp["available_date_rule"] == "B"
    assert gdp["last_status"] == "never_run"
    assert rows["cpi_yoy"]["source_name"] == "src_a"

    # idempotent: second run inserts nothing
    assert backfill_catalog_from_data(cat) == 0
    assert backfill_catalog_from_data(cat) == 0


# ── list_catalog freshness ─────────────────────────────────────────────────


def test_list_catalog_reports_live_freshness(tmp_path):
    cat = _make_catalog(tmp_path)
    anchor = date(2026, 3, 10)
    _insert_price(cat, anchor)
    _insert_price(cat, anchor - timedelta(days=1))
    _insert_indicator(cat, "src", "gdp_yoy", anchor, "2026-03-10 09:00:00")

    backfill_catalog_from_data(cat)
    rows = list_catalog(cat)
    assert len(rows) == 1
    r = rows[0]
    assert r["latest_trade_date"] == anchor
    assert r["stale"] is False


def test_list_catalog_stale_when_behind_anchor_beyond_tolerance(tmp_path):
    cat = _make_catalog(tmp_path)
    anchor = date(2026, 3, 31)
    # anchor + 5 trading days after the indicator's latest day (daily tolerance = 3)
    for i in range(1, 6):
        _insert_price(cat, anchor - timedelta(days=i))
    _insert_price(cat, anchor)
    latest = anchor - timedelta(days=5)
    _insert_indicator(cat, "src", "gdp_yoy", latest, "2026-03-26 09:00:00")

    backfill_catalog_from_data(cat)
    r = list_catalog(cat)[0]
    assert r["latest_trade_date"] == latest
    assert r["stale"] is True


def test_stale_never_uses_current_date(tmp_path):
    """B2 regression: anchor is silver_prices_1d max(trade_date), not wall clock.

    Indicator data is 'yesterday' by wall clock but the prices store itself is
    stale (anchor 30 days ago). Correct anchoring → NOT stale; a CURRENT_DATE
    anchor would wrongly flag it.
    """
    cat = _make_catalog(tmp_path)
    anchor = date.today() - timedelta(days=30)
    _insert_price(cat, anchor)
    _insert_indicator(cat, "src", "gdp_yoy", date.today() - timedelta(days=1),
                      "2026-09-29 09:00:00")

    backfill_catalog_from_data(cat)
    r = list_catalog(cat)[0]
    assert r["latest_trade_date"] == date.today() - timedelta(days=1)
    assert r["latest_trade_date"] > anchor
    assert r["stale"] is False


def test_list_catalog_no_data_rows_is_stale(tmp_path):
    cat = _make_catalog(tmp_path)
    _insert_price(cat, date(2026, 3, 10))
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="no_data_key"))

    r = list_catalog(cat)[0]
    assert r["latest_trade_date"] is None
    assert r["stale"] is True


# ── get / preview ──────────────────────────────────────────────────────────


def test_get_catalog_entry_missing_returns_none(tmp_path):
    cat = _make_catalog(tmp_path)
    assert get_catalog_entry(cat, "nope") is None


def test_get_catalog_entry_preview_tail_30_ascending(tmp_path):
    cat = _make_catalog(tmp_path)
    start = date(2026, 1, 1)
    for i in range(40):
        _insert_indicator(cat, "src", "gdp_yoy", start + timedelta(days=i),
                          "2026-02-20 09:00:00", value=float(i))
    backfill_catalog_from_data(cat)

    entry = get_catalog_entry(cat, "gdp_yoy")
    assert entry is not None
    assert entry["indicator_key"] == "gdp_yoy"
    assert len(entry["preview"]) == 30
    # ascending order, tail of the series
    dates = [p["trade_date"] for p in entry["preview"]]
    assert dates == sorted(dates)
    assert dates[0] == start + timedelta(days=10)
    assert dates[-1] == start + timedelta(days=39)
    assert entry["preview"][-1]["value"] == 39.0


# ── upsert ─────────────────────────────────────────────────────────────────


def test_upsert_rejects_invalid_key(tmp_path):
    cat = _make_catalog(tmp_path)
    with pytest.raises(ValueError):
        upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="Bad-Key!"))
    with pytest.raises(ValueError):
        upsert_catalog_entry(cat, CatalogEntryInput(indicator_key=""))


def test_upsert_insert_then_partial_update(tmp_path):
    cat = _make_catalog(tmp_path)
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="pmi", unit="%"))
    entry = get_catalog_entry(cat, "pmi")
    assert entry["display_name"] == "pmi"  # insert default
    assert entry["unit"] == "%"
    assert entry["enabled"] is True

    # partial update: display_name changes, unit is preserved (None = keep)
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="pmi", display_name="PMI"))
    entry = get_catalog_entry(cat, "pmi")
    assert entry["display_name"] == "PMI"
    assert entry["unit"] == "%"

    # disabling then a None-enabled update must NOT re-enable
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="pmi", enabled=False))
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="pmi", description="x"))
    entry = get_catalog_entry(cat, "pmi")
    assert entry["enabled"] is False
    assert entry["description"] == "x"


# ── delete ─────────────────────────────────────────────────────────────────


def test_delete_purge_removes_data_rows(tmp_path):
    cat = _make_catalog(tmp_path)
    _insert_indicator(cat, "src", "gdp_yoy", date(2026, 1, 5), "2026-01-06 09:00:00")
    _insert_indicator(cat, "src", "gdp_yoy", date(2026, 1, 6), "2026-01-07 09:00:00")
    backfill_catalog_from_data(cat)

    def data_count() -> int:
        return cat.query(
            "SELECT COUNT(*) AS n FROM silver_external_indicators "
            "WHERE indicator_key = 'gdp_yoy'"
        ).item(0, "n")

    # purge=False: catalog row gone, data kept
    delete_catalog_entry(cat, "gdp_yoy", purge_data=False)
    assert get_catalog_entry(cat, "gdp_yoy") is None
    assert data_count() == 2

    # purge=True on a re-backfilled entry: data gone too
    backfill_catalog_from_data(cat)
    delete_catalog_entry(cat, "gdp_yoy", purge_data=True)
    assert get_catalog_entry(cat, "gdp_yoy") is None
    assert data_count() == 0


# ── mark_refresh_result ────────────────────────────────────────────────────


def test_mark_refresh_result_updates_catalog_row(tmp_path):
    cat = _make_catalog(tmp_path)
    upsert_catalog_entry(cat, CatalogEntryInput(indicator_key="gdp_yoy"))
    assert get_catalog_entry(cat, "gdp_yoy")["last_status"] is None

    mark_refresh_result(cat, "gdp_yoy", status="success", source_name="src_http")
    entry = get_catalog_entry(cat, "gdp_yoy")
    assert entry["last_status"] == "success"
    assert entry["last_error"] is None
    assert entry["last_refresh_at"] is not None
    assert entry["source_name"] == "src_http"

    mark_refresh_result(cat, "gdp_yoy", status="failed", error="timeout")
    entry = get_catalog_entry(cat, "gdp_yoy")
    assert entry["last_status"] == "failed"
    assert entry["last_error"] == "timeout"
    # source_name untouched when not passed (None = keep)
    assert entry["source_name"] == "src_http"
