"""Fixed-deadline announce_date derivation + backfill for silver_fundamentals.

D4-A decision — replaces the legacy ``+N days`` offset (migrate_fundamentals_pit.py)
with statutory disclosure *deadlines*:

    03-31 → same year 04-30  (Q1 report)
    06-30 → same year 08-31  (semi-annual)
    09-30 → same year 10-31  (Q3 report)
    12-31 → next  year 04-30 (annual report)

A record becomes visible on its deadline, which is conservative (no lookahead):
the true disclosure date can only be on or before the deadline.

Shared by three call sites (updater / migration script / API endpoint) — keep
this module free of caller-specific logic.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
from datetime import date

logger = logging.getLogger(__name__)

__all__ = ["derive_conservative_announce", "backfill_announce_dates"]

# Standard report-period ends → fixed statutory disclosure deadlines.
_FIXED_TIERS: dict[tuple[int, int], tuple[int, int, int | None]] = {
    (3, 31): (4, 30, None),   # Q1     → same year 04-30
    (6, 30): (8, 31, None),   # H1     → same year 08-31
    (9, 30): (10, 31, None),  # Q3     → same year 10-31
    (12, 31): (4, 30, 1),     # Annual → NEXT year 04-30
}


def derive_conservative_announce(report_date: date) -> date:
    """Disclosure-rule fixed deadline (D4-A, replaces the legacy +N-day offset):

    03-31 → same year 04-30 (Q1)
    06-30 → same year 08-31 (H1)
    09-30 → same year 10-31 (Q3)
    12-31 → next  year 04-30 (annual)
    Non-standard period ends (theoretically absent): degrade to month-end
    + 120 days as a conservative upper bound.

    The returned value is strictly > report_date.
    """
    tier = _FIXED_TIERS.get((report_date.month, report_date.day))
    if tier is not None:
        month, day, year_offset = tier
        return date(report_date.year + (year_offset or 0), month, day)
    # Non-standard period end: month-end + 120 days (conservative upper bound).
    last_day = calendar.monthrange(report_date.year, report_date.month)[1]
    month_end = date(report_date.year, report_date.month, last_day)
    return month_end + dt.timedelta(days=120)


# SQL mirror of derive_conservative_announce for set-based backfill.
_DERIVE_SQL = """
CASE
    WHEN EXTRACT(MONTH FROM report_date) = 3
         AND EXTRACT(DAY FROM report_date) = 31
        THEN make_date(EXTRACT(YEAR FROM report_date)::INT, 4, 30)
    WHEN EXTRACT(MONTH FROM report_date) = 6
         AND EXTRACT(DAY FROM report_date) = 30
        THEN make_date(EXTRACT(YEAR FROM report_date)::INT, 8, 31)
    WHEN EXTRACT(MONTH FROM report_date) = 9
         AND EXTRACT(DAY FROM report_date) = 30
        THEN make_date(EXTRACT(YEAR FROM report_date)::INT, 10, 31)
    WHEN EXTRACT(MONTH FROM report_date) = 12
         AND EXTRACT(DAY FROM report_date) = 31
        THEN make_date((EXTRACT(YEAR FROM report_date) + 1)::INT, 4, 30)
    ELSE (date_trunc('month', report_date) + INTERVAL 1 MONTH - INTERVAL 1 DAY)
         + INTERVAL 120 DAY
END
"""

# Rows eligible for backfill (idempotent):
#   (1) source='akshare' AND announce_date = report_date  — lookahead rows
#   (2) announce_date IS NULL                             — legacy responsibility
_CANDIDATE_WHERE = (
    "(source = 'akshare' AND announce_date = report_date) "
    "OR announce_date IS NULL"
)


def backfill_announce_dates(catalog, *, dry_run: bool = False) -> dict:
    """Backfill announce_date on silver_fundamentals covering two row classes
    (idempotent):

    (1) source='akshare' AND announce_date = report_date (lookahead rows)
    (2) announce_date IS NULL (legacy-script responsibility; unified to the
        fixed-deadline tiers)

    tushare rows are never modified. ``dry_run`` only counts, never writes.

    Returns ``{candidates, updated, by_source, violations_after}`` where
    ``violations_after`` counts rows with ``announce_date <= report_date``
    after the backfill (expected 0). tushare rows violating that invariant
    are NOT modified, only counted and reported separately under
    ``tushare_violations``.
    """
    stats = catalog.query(
        "SELECT source, COUNT(*) AS n FROM silver_fundamentals "
        f"WHERE {_CANDIDATE_WHERE} GROUP BY source"
    )
    by_source = {stats["source"][i]: stats["n"][i] for i in range(len(stats))}
    candidates = int(sum(by_source.values()))
    logger.info("announce backfill candidates=%d by_source=%s", candidates, by_source)

    updated = 0
    if candidates > 0 and not dry_run:
        catalog.execute(
            "UPDATE silver_fundamentals "
            f"SET announce_date = {_DERIVE_SQL} "
            f"WHERE {_CANDIDATE_WHERE}"
        )
        remaining = catalog.query(
            "SELECT COUNT(*) AS n FROM silver_fundamentals "
            f"WHERE {_CANDIDATE_WHERE}"
        )
        updated = candidates - int(remaining["n"][0])
        logger.info("announce backfill updated=%d rows", updated)

    violations = catalog.query(
        "SELECT source, COUNT(*) AS n FROM silver_fundamentals "
        "WHERE announce_date <= report_date GROUP BY source"
    )
    violations_by_source = {
        violations["source"][i]: violations["n"][i] for i in range(len(violations))
    }
    violations_after = int(sum(violations_by_source.values()))
    tushare_violations = int(violations_by_source.get("tushare", 0))
    if tushare_violations:
        logger.warning(
            "tushare rows with announce_date <= report_date (NOT modified): %d",
            tushare_violations,
        )

    return {
        "candidates": candidates,
        "updated": updated,
        "by_source": by_source,
        "violations_after": violations_after,
        "tushare_violations": tushare_violations,
    }
