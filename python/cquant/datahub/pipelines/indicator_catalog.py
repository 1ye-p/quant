"""External indicator catalog repository (P1-2).

Functional CRUD over ``silver_external_indicator_catalog`` — no ORM. The
catalog is a pure metadata table: derived values (``latest_trade_date``,
``stale``) are computed live by :func:`list_catalog` / :func:`get_catalog_entry`
and never persisted (B2 lesson: no stale materialized columns).

Anchoring rule (B2): every date comparison — including stale judgement —
anchors on ``SELECT max(trade_date) FROM silver_prices_1d``. ``CURRENT_DATE``
is never used, so a stale prices store does not produce false "stale" flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cquant.datahub.catalog import Catalog

_INDICATOR_KEY_RE = re.compile(r"^[a-z_0-9]+$")

# stale tolerance in trading days, keyed by catalog `frequency`
STALE_TOLERANCE_TRADING_DAYS = {"daily": 3, "weekly": 10}
_DEFAULT_TOLERANCE = 3

_CATALOG_COLS = (
    "indicator_key, display_name, unit, description, source_type, source_name, "
    "pinned_source, source_config, available_date_rule, frequency, backfill_start, "
    "enabled, last_refresh_at, last_status, last_error, updated_at"
)


# ── backfill (migration from existing data rows) ───────────────────────────


def backfill_catalog_from_data(catalog: Catalog) -> int:
    """Idempotent migration: ``silver_external_indicators`` DISTINCT keys → catalog rows.

    Controller ruling (same key from multiple sources): catalog PK is
    ``indicator_key`` with a single active source — the source whose data row
    has the latest ``updated_at`` for that key wins as ``source_name``.

    New rows are written with ``source_type='csv'``, ``display_name=indicator_key``,
    ``available_date_rule='B'``, ``last_status='never_run'``. Uses
    ``INSERT OR IGNORE`` so re-running is a no-op for existing keys.

    Returns the number of rows actually inserted by this call.
    """
    before = _catalog_count(catalog)
    catalog.execute(
        """
        INSERT OR IGNORE INTO silver_external_indicator_catalog
            (indicator_key, display_name, source_type, source_name,
             available_date_rule, last_status)
        SELECT key, key, 'csv', src, 'B', 'never_run'
        FROM (
            SELECT indicator_key AS key,
                   arg_max(source, updated_at) AS src
            FROM silver_external_indicators
            GROUP BY indicator_key
        )
        """
    )
    return _catalog_count(catalog) - before


def _catalog_count(catalog: Catalog) -> int:
    return catalog.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicator_catalog"
    ).item(0, "n")


# ── listing with live freshness ─────────────────────────────────────────────


def list_catalog(catalog: Catalog) -> list[dict]:
    """All catalog rows enriched with live freshness (never persisted).

    For each key: ``latest_trade_date`` = max(trade_date) of its data rows
    (None when no data). ``stale`` = the key's data lags the anchor date
    (``max(trade_date)`` of ``silver_prices_1d``) by more than the frequency
    tolerance counted in *trading days* — trading days are counted as actual
    ``silver_prices_1d`` distinct trade dates in the lag window.

    - anchor unavailable (empty prices table) → ``stale=False`` (no basis to judge)
    - no data rows for the key → ``stale=True``
    """
    rows = catalog.query(
        f"SELECT {_CATALOG_COLS} FROM silver_external_indicator_catalog "
        "ORDER BY indicator_key"
    ).rows(named=True)

    anchor = _anchor_date(catalog)
    latest_by_key = {
        r["indicator_key"]: r["max_td"]
        for r in catalog.query(
            "SELECT indicator_key, max(trade_date) AS max_td "
            "FROM silver_external_indicators GROUP BY indicator_key"
        ).rows(named=True)
    }

    out: list[dict] = []
    for row in rows:
        key = row["indicator_key"]
        latest = latest_by_key.get(key)
        stale = _is_stale(catalog, anchor, latest, row["frequency"])
        out.append({**row, "latest_trade_date": latest, "stale": stale})
    return out


def _anchor_date(catalog: Catalog):
    df = catalog.query("SELECT max(trade_date) AS a FROM silver_prices_1d")
    return df.item(0, "a")


def _trading_days_behind(catalog: Catalog, latest, anchor) -> int:
    """Trading days in (latest, anchor], counted as actual distinct
    ``silver_prices_1d`` trade dates — exact for a populated prices store."""
    return catalog.query(
        "SELECT COUNT(DISTINCT trade_date) AS n FROM silver_prices_1d "
        "WHERE trade_date > ? AND trade_date <= ?",
        [latest, anchor],
    ).item(0, "n")


def _is_stale(catalog: Catalog, anchor, latest, frequency: str | None) -> bool:
    if anchor is None:
        return False
    if latest is None:
        return True
    if latest >= anchor:
        return False
    tolerance = STALE_TOLERANCE_TRADING_DAYS.get(
        str(frequency or "").lower(), _DEFAULT_TOLERANCE
    )
    return _trading_days_behind(catalog, latest, anchor) > tolerance


# ── detail + preview ────────────────────────────────────────────────────────


def get_catalog_entry(catalog: Catalog, indicator_key: str) -> dict | None:
    """Catalog row + ``preview``: last 30 data rows (ascending) as
    ``[{trade_date, value, available_date}]``. Returns None when key absent."""
    df = catalog.query(
        f"SELECT {_CATALOG_COLS} FROM silver_external_indicator_catalog "
        "WHERE indicator_key = ?",
        [indicator_key],
    )
    if df.height == 0:
        return None
    entry = dict(df.row(0, named=True))
    preview = catalog.query(
        "SELECT trade_date, value, available_date FROM silver_external_indicators "
        "WHERE indicator_key = ? ORDER BY trade_date DESC LIMIT 30",
        [indicator_key],
    )
    entry["preview"] = list(reversed(preview.rows(named=True)))
    return entry


# ── upsert ──────────────────────────────────────────────────────────────────


@dataclass
class CatalogEntryInput:
    """Upsert payload. ``indicator_key`` required; every other field optional.

    Partial-update semantics: on conflict, the ``DO UPDATE SET`` list is built
    dynamically from provided (non-``None``) fields only — ``None`` fields are
    omitted from the SET list and keep their existing stored value, so a field
    cannot be explicitly cleared back to NULL through this interface. On fresh insert,
    NOT NULL columns fall back to defaults: ``display_name=indicator_key``,
    ``source_type='custom_http'``, ``source_name='manual'``,
    ``available_date_rule='B'``, ``frequency='daily'``, ``enabled=TRUE``.
    """

    indicator_key: str
    display_name: str | None = None
    unit: str | None = None
    description: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    pinned_source: str | None = None
    source_config: str | None = None  # JSON string
    available_date_rule: str | None = None
    frequency: str | None = None
    backfill_start: object | None = None  # date or ISO string
    enabled: bool | None = None


_UPSERTABLE_FIELDS = (
    "display_name", "unit", "description", "source_type", "source_name",
    "pinned_source", "source_config", "available_date_rule", "frequency",
    "backfill_start", "enabled",
)


def upsert_catalog_entry(catalog: Catalog, entry: CatalogEntryInput) -> None:
    """Insert or partially update one catalog row (see CatalogEntryInput).

    The DO UPDATE SET list is built from the fields actually provided, so a
    ``None`` field never overwrites its stored value (COALESCE alone can't
    express this for NOT NULL / boolean columns where the INSERT side carries
    defaults).
    """
    if not _INDICATOR_KEY_RE.match(entry.indicator_key or ""):
        raise ValueError(
            f"indicator_key 非法：'{entry.indicator_key}'，仅允许小写字母/数字/下划线 [a-z_0-9]+"
        )
    provided = [f for f in _UPSERTABLE_FIELDS if getattr(entry, f) is not None]
    set_clause = ", ".join(f"{f} = excluded.{f}" for f in provided)
    sql = f"""
        INSERT INTO silver_external_indicator_catalog
            (indicator_key, display_name, unit, description, source_type,
             source_name, pinned_source, source_config, available_date_rule,
             frequency, backfill_start, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (indicator_key) DO UPDATE SET
            {set_clause + ", updated_at = NOW()" if set_clause else "updated_at = NOW()"}
        """
    catalog.execute(
        sql,
        [
            entry.indicator_key,
            entry.display_name or entry.indicator_key,
            entry.unit,
            entry.description,
            entry.source_type or "custom_http",
            entry.source_name or "manual",
            entry.pinned_source,
            entry.source_config,
            entry.available_date_rule or "B",
            entry.frequency or "daily",
            entry.backfill_start,
            True if entry.enabled is None else entry.enabled,
        ],
    )


# ── delete ──────────────────────────────────────────────────────────────────


def delete_catalog_entry(
    catalog: Catalog, indicator_key: str, purge_data: bool = False
) -> None:
    """Delete a catalog row. ``purge_data=True`` also deletes all of the key's
    rows in ``silver_external_indicators``."""
    catalog.execute(
        "DELETE FROM silver_external_indicator_catalog WHERE indicator_key = ?",
        [indicator_key],
    )
    if purge_data:
        catalog.execute(
            "DELETE FROM silver_external_indicators WHERE indicator_key = ?",
            [indicator_key],
        )


# ── refresh result marking (P2 callers will use this) ──────────────────────


def mark_refresh_result(
    catalog: Catalog,
    indicator_key: str,
    *,
    status: str,
    error: str | None = None,
    source_name: str | None = None,
) -> None:
    """Update the catalog row's last refresh outcome (signature only — no
    refresh/scheduling logic lives here in P1)."""
    catalog.execute(
        """
        UPDATE silver_external_indicator_catalog
        SET last_status = ?, last_error = ?, last_refresh_at = NOW(),
            source_name = COALESCE(?, source_name), updated_at = NOW()
        WHERE indicator_key = ?
        """,
        [status, error, source_name, indicator_key],
    )
