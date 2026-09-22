"""cquant.datahub.universe — Point-in-time universe construction.

Constructs survivorship-bias-free stock universes using list_date / delist_date
to ensure backtests only include stocks that were actually tradeable at each
point in time.

Usage::

    from cquant.datahub.universe import PointInTimeUniverse

    universe = PointInTimeUniverse(catalog)
    stocks = universe.get_universe("2024-06-15")  # active stocks on that date
    universe_df = universe.build_universe_series("2024-01-01", "2025-06-30")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

# Sector/block indices (TDX 880xxx/881xxx) are stored alongside stocks with a
# bare '88' symbol prefix after the exchange tag (e.g. 'SSE:880004'). They are
# excluded from stock universes by default to avoid polluting coverage counts
# and cross-sectional statistics.
#
# Public constant: engine-side consumers (backtest_vector universe resolver,
# IC computation routes) import this to keep the exclusion predicate from
# drifting between datahub and engine layers.
INDEX_EXCLUSION_SQL = "asset_id NOT LIKE '%:88%'"

# Backwards-compatible private alias (legacy intra-module references).
_INDEX_EXCLUSION = INDEX_EXCLUSION_SQL


@dataclass
class UniverseEntry:
    """A single stock entry in the universe."""
    asset_id: str
    list_date: str
    delist_date: str | None = None
    name: str | None = None
    sector: str | None = None
    is_active: bool = True


@dataclass
class UniverseSnapshot:
    """Universe at a specific point in time."""
    date: str
    active_stocks: list[UniverseEntry] = field(default_factory=list)
    total_listed: int = 0
    total_delisted: int = 0
    total_active: int = 0


class PointInTimeUniverse:
    """Construct survivorship-bias-free stock universes.

    Uses list_date and delist_date from the stock metadata table to determine
    which stocks were tradeable at any given date.

    Parameters
    ----------
    catalog : Any
        A DuckDB Catalog instance.
    metadata_table : str
        Table containing stock metadata with list_date, delist_date columns.
        Default is ``silver_assets``.
    """

    def __init__(
        self,
        catalog: Any,
        metadata_table: str = "silver_assets",
    ):
        _ALLOWED_TABLES = {"silver_assets", "silver_fundamentals"}
        if metadata_table not in _ALLOWED_TABLES:
            raise ValueError(f"metadata_table '{metadata_table}' not in allowed tables: {_ALLOWED_TABLES}")
        self.catalog = catalog
        self.metadata_table = metadata_table

    def get_universe(
        self,
        as_of_date: str,
        include_delisted: bool = False,
        include_indices: bool = False,
    ) -> list[UniverseEntry]:
        """Get the active stock universe at a specific date.

        Parameters
        ----------
        as_of_date : str
            The date to check (YYYY-MM-DD).
        include_delisted : bool
            If True, include stocks that were delisted after as_of_date
            but were active on that date.
        include_indices : bool
            If True, include sector/block indices ('88' symbol prefix,
            e.g. 'SSE:880004'). Excluded by default.

        Returns
        -------
        list[UniverseEntry]
        """
        try:
            # Query: stocks listed before or on as_of_date
            # and either not delisted, or delisted after as_of_date
            index_cond = "" if include_indices else f"AND {_INDEX_EXCLUSION}"
            query = (
                f"SELECT asset_id, list_date, delist_date, name, sector "
                f"FROM {self.metadata_table} "
                f"WHERE list_date <= ? "
                f"AND (delist_date IS NULL OR delist_date > ?) "
                f"{index_cond} "
                f"ORDER BY asset_id"
            )
            df = self.catalog.query(query, [as_of_date, as_of_date])
        except Exception as e:
            logger.warning("Failed to query universe: %s", e)
            return []

        entries = []
        for row in df.iter_rows(named=True):
            entries.append(UniverseEntry(
                asset_id=row["asset_id"],
                list_date=str(row["list_date"] or ""),
                delist_date=str(row["delist_date"]) if row["delist_date"] else None,
                name=row.get("name"),
                sector=row.get("sector"),
                is_active=True,
            ))

        return entries

    def build_universe_series(
        self,
        start_date: str,
        end_date: str,
        freq: str = "monthly",
    ) -> pl.DataFrame:
        """Build a time series of universe membership.

        Parameters
        ----------
        start_date, end_date : str
            Date range.
        freq : str
            Frequency of snapshots: "daily", "weekly", "monthly".

        Returns
        -------
        pl.DataFrame
            Columns: date, asset_id, is_active
        """
        # Load all stock info (sector indices excluded from stock universes)
        try:
            info_df = self.catalog.query(
                f"SELECT asset_id, list_date, delist_date FROM {self.metadata_table} "
                f"WHERE {_INDEX_EXCLUSION}"
            )
        except Exception as e:
            logger.warning("Failed to load stock info: %s", e)
            return pl.DataFrame({"date": [], "asset_id": [], "is_active": []})

        if info_df.is_empty():
            return pl.DataFrame({"date": [], "asset_id": [], "is_active": []})

        # Generate snapshot dates
        snapshot_dates = self._generate_dates(start_date, end_date, freq)

        # Build membership matrix
        records = []
        for snap_date in snapshot_dates:
            for row in info_df.iter_rows(named=True):
                list_d = str(row["list_date"] or "")
                delist_d = str(row["delist_date"] or "") if row["delist_date"] else ""

                # Is active on snap_date?
                listed = list_d <= snap_date if list_d else False
                not_delisted = (not delist_d) or (delist_d > snap_date)
                is_active = listed and not_delisted

                if is_active:
                    records.append({
                        "date": snap_date,
                        "asset_id": row["asset_id"],
                        "is_active": True,
                    })

        if not records:
            return pl.DataFrame({"date": [], "asset_id": [], "is_active": []})

        return pl.DataFrame(records)

    def get_universe_stats(
        self,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """Get universe statistics over a date range.

        Returns dict with:
        - total_unique_stocks: total distinct stocks ever in universe
        - avg_universe_size: average number of active stocks per snapshot
        - new_listings: count of stocks that listed during the range
        - delistings: count of stocks that delisted during the range
        - survivorship_rate: fraction of start-date stocks still active at end
        """
        try:
            info_df = self.catalog.query(
                f"SELECT asset_id, list_date, delist_date FROM {self.metadata_table} "
                f"WHERE {_INDEX_EXCLUSION}"
            )
        except Exception:
            return {}

        if info_df.is_empty():
            return {}

        start_stocks = self.get_universe(start_date)
        end_stocks = self.get_universe(end_date)
        start_ids = {s.asset_id for s in start_stocks}
        end_ids = {s.asset_id for s in end_stocks}

        new_listings = sum(
            1 for row in info_df.iter_rows(named=True)
            if str(row["list_date"] or "") >= start_date
            and str(row["list_date"] or "") <= end_date
        )

        delistings = sum(
            1 for row in info_df.iter_rows(named=True)
            if row["delist_date"]
            and str(row["delist_date"]) >= start_date
            and str(row["delist_date"]) <= end_date
        )

        survived = start_ids & end_ids
        survivorship_rate = len(survived) / len(start_ids) if start_ids else 0.0

        return {
            "total_unique_stocks": len(info_df),
            "start_universe_size": len(start_stocks),
            "end_universe_size": len(end_stocks),
            "new_listings": new_listings,
            "delistings": delistings,
            "survivorship_rate": round(survivorship_rate, 4),
            "start_date": start_date,
            "end_date": end_date,
        }

    def _generate_dates(
        self, start_date: str, end_date: str, freq: str
    ) -> list[str]:
        """Generate snapshot dates at the given frequency."""
        from datetime import datetime, timedelta

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        dates = []
        current = start
        if freq == "daily":
            delta = timedelta(days=1)
        elif freq == "weekly":
            delta = timedelta(weeks=1)
        else:  # monthly
            delta = timedelta(days=30)

        while current <= end:
            dates.append(current.strftime("%Y-%m-%d"))
            current += delta

        return dates
