"""PIT universe tests — silver_assets table fix + sector-index exclusion."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from cquant.api_server.routes.datasets import (
    get_point_in_time_universe,
    get_universe_stats,
)
from cquant.datahub.catalog import Catalog
from cquant.datahub.universe import PointInTimeUniverse

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()
    rows = [
        # (asset_id, list_date, delist_date)
        ("SSE:600036", "2003-11-05", None),          # active stock
        ("SZSE:000001", "1991-04-03", None),          # active stock
        ("SZSE:000003", "1991-07-03", "2023-01-06"),  # delisted before as_of
        ("SSE:688999", "2026-06-01", None),           # listed after as_of
        ("SSE:880004", "2020-01-01", None),           # sector index (880xxx)
        ("SSE:881193", "2020-01-01", None),           # sector index (881xxx)
    ]
    for aid, ld, dd in rows:
        conn.execute(
            "INSERT INTO silver_assets (asset_id, symbol, exchange, asset_class, "
            "currency, name, status, list_date, delist_date, industry, sector, "
            "effective_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [aid, aid.split(":")[1], aid.split(":")[0], "stock", "CNY", "", "active",
             date.fromisoformat(ld), date.fromisoformat(dd) if dd else None,
             None, None, date.fromisoformat(ld)],
        )
    return cat


class TestPointInTimeUniverse:
    def test_active_stocks_on_date(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        stocks = universe.get_universe("2025-06-30")
        ids = {s.asset_id for s in stocks}
        assert ids == {"SSE:600036", "SZSE:000001"}

    def test_indices_excluded_by_default(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        stocks = universe.get_universe("2025-06-30")
        assert all(not s.asset_id.endswith(":880004") for s in stocks)
        assert all(not s.asset_id.endswith(":881193") for s in stocks)

    def test_indices_included_on_request(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        stocks = universe.get_universe("2025-06-30", include_indices=True)
        ids = {s.asset_id for s in stocks}
        assert "SSE:880004" in ids
        assert "SSE:881193" in ids
        assert len(ids) == 4  # 2 stocks + 2 indices (delisted/unlisted still out)

    def test_delisted_and_unlisted_excluded(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        ids = {s.asset_id for s in universe.get_universe("2025-06-30")}
        assert "SZSE:000003" not in ids  # delisted 2023
        assert "SSE:688999" not in ids   # lists 2026-06

    def test_stats_non_empty(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        stats = universe.get_universe_stats("2024-01-01", "2025-06-30")
        assert stats != {}
        assert stats["start_universe_size"] == 2
        assert stats["end_universe_size"] == 2
        assert stats["total_unique_stocks"] == 4  # indices filtered out

    def test_series_excludes_indices(self, catalog) -> None:
        universe = PointInTimeUniverse(catalog)
        df = universe.build_universe_series("2025-01-01", "2025-03-01", freq="monthly")
        assert not df.is_empty()
        assert set(df["asset_id"].to_list()) == {"SSE:600036", "SZSE:000001"}


class TestUniverseRoutes:
    def test_pit_route_returns_count(self, catalog) -> None:
        resp = asyncio.run(get_point_in_time_universe(catalog, "2025-06-30"))
        assert resp["count"] == 2
        assert {s["asset_id"] for s in resp["stocks"]} == {"SSE:600036", "SZSE:000001"}

    def test_stats_route_returns_dict(self, catalog) -> None:
        resp = asyncio.run(get_universe_stats(catalog, "2024-01-01", "2025-06-30"))
        assert resp["start_universe_size"] == 2
        assert resp["total_unique_stocks"] == 4
