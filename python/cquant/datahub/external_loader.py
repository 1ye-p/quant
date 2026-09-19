"""PIT loader for external indicators.

Enforces the point-in-time hard constraint: only rows whose
``available_date <= as_of_date`` are ever returned. All downstream consumers
(factor materialization, Phase 3 regime evaluation, backtests) must read
external indicators through this loader — never query
``silver_external_indicators`` directly.
"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from cquant.datahub.catalog import Catalog

logger = logging.getLogger(__name__)

MARKET_SENTINEL = "__MARKET__"

_LOAD_SQL = """
SELECT trade_date, value
FROM silver_external_indicators
WHERE indicator_key = ?
  AND asset_id = ?
  AND available_date <= ?
ORDER BY trade_date
"""


def load_external_series(
    catalog: Catalog,
    indicator_key: str,
    as_of_date: date,
    asset_id: str = MARKET_SENTINEL,
) -> pl.DataFrame:
    """Load a PIT-safe external indicator series.

    Args:
        catalog: datahub Catalog.
        indicator_key: indicator identifier (e.g. ``sh_index_turnover``).
        as_of_date: point-in-time cutoff — rows with ``available_date`` after
            this date are NOT returned (look-ahead protection).
        asset_id: defaults to the ``__MARKET__`` sentinel for market-level
            indicators (decision D1-A).

    Returns:
        DataFrame with columns ``trade_date``, ``value`` ordered by trade_date.
    """
    return catalog.query(_LOAD_SQL, [indicator_key, asset_id, as_of_date])
