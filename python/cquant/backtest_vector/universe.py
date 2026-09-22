"""Universe resolver: maps universe IDs to asset_id lists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cquant.datahub.universe import INDEX_EXCLUSION_SQL

if TYPE_CHECKING:
    from cquant.datahub.catalog import Catalog

# Anchor to the data's latest trade_date (not CURRENT_DATE) so presets still
# resolve on stale data. Same pattern as api_server/routes/datasets.py.
_DATA_ANCHOR_SQL = (
    "(SELECT MAX(trade_date) FROM silver_prices_1d) - INTERVAL '30 days'"
)


UNIVERSE_PRESETS: dict[str, dict] = {
    "all": {"type": "none"},
    "sse": {"type": "prefix", "prefix": "SSE:"},
    "szse": {"type": "prefix", "prefix": "SZSE:"},
    "cyb": {"type": "like", "pattern": "SZSE:3%"},
    "kcb": {"type": "like", "pattern": "SSE:688%"},
    "bse": {"type": "like", "pattern": "BSE:%"},
    "idx_sse": {"type": "index", "index_code": "000001"},
    "idx_szse": {"type": "index", "index_code": "399001"},
    "idx_hs300": {"type": "index", "index_code": "000300"},
    "idx_zz500": {"type": "index", "index_code": "000905"},
    "idx_zz1000": {"type": "index", "index_code": "000852"},
    "idx_cyb": {"type": "index", "index_code": "399006"},
    "idx_kcb50": {"type": "index", "index_code": "000688"},
}

INDEX_CONSTITUENTS_DDL = """
CREATE TABLE IF NOT EXISTS meta_index_constituents (
    index_code  VARCHAR NOT NULL,
    asset_id    VARCHAR NOT NULL,
    entry_date  DATE,
    is_current  BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (index_code, asset_id)
);
"""


def resolve_universe(
    catalog: "Catalog", universe_id: str
) -> list[str] | None:
    """Resolve a universe ID to a list of asset_ids.

    Default universe ('all' / unknown preset): all assets in the last 30 days
    of data, excluding sector/block indices (880xxx/881xxx) — the default
    universe never contains indices. Explicit index inclusion is opt-in via
    the idx_* presets ("index" type below). Returns an empty list for no
    matches.
    """
    preset = UNIVERSE_PRESETS.get(universe_id)
    if not preset or preset["type"] == "none":
        df = catalog.query(
            "SELECT DISTINCT asset_id FROM silver_prices_1d "
            f"WHERE {INDEX_EXCLUSION_SQL} AND trade_date >= {_DATA_ANCHOR_SQL}"
        )
        return df["asset_id"].to_list() if not df.is_empty() else []

    if preset["type"] == "prefix":
        df = catalog.query(
            "SELECT DISTINCT asset_id FROM silver_prices_1d "
            f"WHERE asset_id LIKE ? AND {INDEX_EXCLUSION_SQL} "
            f"AND trade_date >= {_DATA_ANCHOR_SQL}",
            [f"{preset['prefix']}%"],
        )
        return df["asset_id"].to_list() if not df.is_empty() else []

    if preset["type"] == "like":
        df = catalog.query(
            "SELECT DISTINCT asset_id FROM silver_prices_1d "
            f"WHERE asset_id LIKE ? AND {INDEX_EXCLUSION_SQL} "
            f"AND trade_date >= {_DATA_ANCHOR_SQL}",
            [preset["pattern"]],
        )
        return df["asset_id"].to_list() if not df.is_empty() else []

    if preset["type"] == "index":
        try:
            df = catalog.query(
                "SELECT asset_id FROM meta_index_constituents "
                "WHERE index_code = ? AND is_current = TRUE",
                [preset["index_code"]],
            )
            return df["asset_id"].to_list() if not df.is_empty() else []
        except Exception:
            return None

    return None
