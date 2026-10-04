"""Tests for fixed-deadline announce_date derivation + backfill (D4-A)."""
from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import pytest

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.announce_backfill import (
    backfill_announce_dates,
    derive_conservative_announce,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# derive_conservative_announce — pure function boundary cases
# ---------------------------------------------------------------------------

def test_q1_boundary():
    assert derive_conservative_announce(date(2025, 3, 31)) == date(2025, 4, 30)


def test_h1_boundary():
    assert derive_conservative_announce(date(2025, 6, 30)) == date(2025, 8, 31)


def test_q3_boundary():
    assert derive_conservative_announce(date(2025, 9, 30)) == date(2025, 10, 31)


def test_annual_crosses_year():
    # 12-31 → NEXT year's 04-30 — the classic off-by-one-year trap.
    assert derive_conservative_announce(date(2024, 12, 31)) == date(2025, 4, 30)
    assert derive_conservative_announce(date(2020, 12, 31)) == date(2021, 4, 30)  # leap year


@pytest.mark.parametrize("month", range(1, 13))
def test_strictly_after_report_date(month):
    year = 2025
    last_day = calendar.monthrange(year, month)[1]
    report = date(year, month, last_day)
    assert derive_conservative_announce(report) > report


def test_non_standard_period_end_falls_back_to_monthend_plus_120():
    # Non-standard period end (theoretically absent): month-end + 120 days.
    report = date(2025, 5, 31)
    import datetime as dt

    assert derive_conservative_announce(report) == report + dt.timedelta(days=120)


# ---------------------------------------------------------------------------
# backfill_announce_dates — catalog fixture tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "announce_test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    # Three classes of rows:
    #  - akshare lookahead: announce_date = report_date
    #  - akshare NULL:      announce_date IS NULL
    #  - tushare normal:    real disclosure date > report_date
    rows = [
        # akshare lookahead rows
        ("SSE:600000", date(2025, 3, 31), date(2025, 3, 31), "akshare"),
        ("SSE:600000", date(2024, 12, 31), date(2024, 12, 31), "akshare"),
        ("SZSE:000001", date(2025, 6, 30), date(2025, 6, 30), "akshare"),
        # akshare NULL rows
        ("SSE:600000", date(2025, 6, 30), None, "akshare"),
        ("SZSE:000001", date(2024, 12, 31), None, "akshare"),
        # tushare rows with genuine announce dates — must not be touched
        ("SSE:600036", date(2025, 3, 31), date(2025, 4, 25), "tushare"),
        ("SSE:600036", date(2024, 12, 31), date(2025, 3, 28), "tushare"),
    ]
    cat.executemany(
        "INSERT INTO silver_fundamentals (asset_id, report_date, announce_date, source) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    yield cat
    cat._backend.close()


def _fetch(cat, asset_id, report_date):
    df = cat.query(
        "SELECT announce_date FROM silver_fundamentals "
        "WHERE asset_id = ? AND report_date = ?",
        [asset_id, report_date],
    )
    return df["announce_date"][0] if len(df) else None


def test_backfill_targets_akshare_lookahead_and_null(catalog):
    result = backfill_announce_dates(catalog)
    assert result["candidates"] == 5
    assert result["updated"] == 5
    # Lookahead rows fixed to fixed-deadline tier values
    assert _fetch(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 4, 30)
    assert _fetch(catalog, "SSE:600000", date(2024, 12, 31)) == date(2025, 4, 30)
    assert _fetch(catalog, "SZSE:000001", date(2025, 6, 30)) == date(2025, 8, 31)
    # NULL rows backfilled with the same fixed-deadline values
    assert _fetch(catalog, "SSE:600000", date(2025, 6, 30)) == date(2025, 8, 31)
    assert _fetch(catalog, "SZSE:000001", date(2024, 12, 31)) == date(2025, 4, 30)
    assert result["violations_after"] == 0


def test_backfill_ignores_tushare_rows(catalog):
    backfill_announce_dates(catalog)
    assert _fetch(catalog, "SSE:600036", date(2025, 3, 31)) == date(2025, 4, 25)
    assert _fetch(catalog, "SSE:600036", date(2024, 12, 31)) == date(2025, 3, 28)


def test_backfill_idempotent(catalog):
    first = backfill_announce_dates(catalog)
    assert first["updated"] == 5
    second = backfill_announce_dates(catalog)
    assert second["candidates"] == 0
    assert second["updated"] == 0
    assert second["violations_after"] == 0


def test_backfill_dry_run_writes_nothing(catalog):
    result = backfill_announce_dates(catalog, dry_run=True)
    assert result["candidates"] == 5
    assert result["updated"] == 0
    # Nothing written: lookahead/NULL rows unchanged
    assert _fetch(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 3, 31)
    assert _fetch(catalog, "SSE:600000", date(2025, 6, 30)) is None


# ---------------------------------------------------------------------------
# scripts/migrate_fundamentals_pit.py — thin wrapper (single-impl delegation)
# ---------------------------------------------------------------------------


def _load_script():
    import importlib.util

    path = _REPO_ROOT / "scripts" / "migrate_fundamentals_pit.py"
    spec = importlib.util.spec_from_file_location("migrate_fundamentals_pit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_migrate_delegates_to_backfill(catalog):
    mod = _load_script()
    result = mod.migrate(catalog)
    # stats shape + real writes match the shared impl
    expected_keys = {
        "candidates", "updated", "by_source", "violations_after", "tushare_violations",
    }
    assert expected_keys <= set(result)
    assert result["updated"] == 5
    assert _fetch(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 4, 30)
    assert _fetch(catalog, "SZSE:000001", date(2024, 12, 31)) == date(2025, 4, 30)


def test_script_migrate_dry_run_writes_nothing(catalog):
    mod = _load_script()
    result = mod.migrate(catalog, dry_run=True)
    assert result["candidates"] == 5
    assert result["updated"] == 0
    assert _fetch(catalog, "SSE:600000", date(2025, 3, 31)) == date(2025, 3, 31)
