"""Quality report fixes — data-anchored windows, real whitelist, visible errors."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from fastapi import HTTPException

from cquant.api_server.routes.datasets import (
    get_anomalies,
    get_data_quality,
    get_dataset_quality,
    list_universes,
)
from cquant.datahub.catalog import Catalog
from cquant.datahub.quality_scorer import DataQualityScorer, QualityQueryError

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ~262 days stale relative to today
_LAST_DATA_DATE = date.today() - timedelta(days=262)


def _insert_prices(cat: Catalog, assets: list[str], days: int = 40) -> None:
    rows = []
    for ai, aid in enumerate(assets):
        p = 50.0 + ai * 10
        for k in range(days):
            d = _LAST_DATA_DATE - timedelta(days=(days - 1 - k))
            # One 30% jump on an index asset so the anomaly filter is testable.
            px = p * (1.30 if (aid.endswith("880004") and k == days - 1) else 1.0)
            rows.append({
                "asset_id": aid, "trade_date": d,
                "open": p, "high": max(p, px) * 1.01, "low": min(p, px) * 0.99,
                "close": px, "volume": 1e6, "amount": px * 1e6,
                "adj_factor": 1.0, "adj_close": px, "is_suspended": False,
                "limit_up": None, "limit_down": None,
                "source": "test", "ingestion_id": None,
            })
            p = px
    df = pl.DataFrame(rows)
    conn = cat._get_conn()
    conn.register("_qp", df.to_arrow())
    conn.execute("INSERT INTO silver_prices_1d SELECT * FROM _qp")
    conn.unregister("_qp")


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    _insert_prices(cat, ["SSE:600036", "SZSE:000001", "SSE:880004"])
    return cat


class TestAnchoredQuality:
    def test_stale_data_still_reports_coverage(self, catalog) -> None:
        """262-day-stale data must not produce empty coverage windows."""
        resp = asyncio.run(get_dataset_quality(catalog))
        stats = resp["stats"]
        assert stats["n_assets"] == 2  # index excluded
        assert stats["total_rows"] > 0
        assert stats["recent_assets"] == 2  # anchored to max(trade_date), not today
        assert resp["daily_coverage"], "daily coverage must be non-empty for stale data"
        assert all(r["n_assets"] == 2 for r in resp["daily_coverage"])

    def test_bottom_assets_excludes_indices(self, catalog) -> None:
        resp = asyncio.run(get_dataset_quality(catalog))
        assets = {r["asset_id"] for r in resp["bottom_assets"]}
        assert "SSE:880004" not in assets

    def test_universes_total_assets_anchored(self, catalog) -> None:
        resp = asyncio.run(list_universes(catalog))
        assert resp["total_assets"] == 2  # stale data + index exclusion


class TestWhitelist:
    def test_silver_prices_1d_allowed(self, catalog) -> None:
        resp = asyncio.run(
            get_data_quality(
                "silver_prices_1d", catalog,
                start_date="2020-01-01",
                end_date=str(_LAST_DATA_DATE + timedelta(days=1)),
            )
        )
        assert resp["table_name"] == "silver_prices_1d"
        assert resp["completeness"]["total_expected_cells"] > 0
        assert resp["overall_score"] > 0

    def test_silver_daily_rejected(self, catalog) -> None:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_data_quality("silver_daily", catalog))
        assert exc_info.value.status_code == 400

    def test_silver_stock_info_rejected(self, catalog) -> None:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_data_quality("silver_stock_info", catalog))
        assert exc_info.value.status_code == 400


class TestScorerErrors:
    def test_query_failure_raises(self, tmp_path) -> None:
        cat = Catalog(db_path=tmp_path / "t.duckdb", repo_root=_REPO_ROOT)
        cat.initialize()
        scorer = DataQualityScorer(cat)
        with pytest.raises(QualityQueryError, match="missing_table_xyz"):
            scorer.score("missing_table_xyz", "2024-01-01", "2025-12-31")

    def test_route_surfaces_500_on_scorer_failure(self, catalog) -> None:
        """Query failure must surface, not return an all-zero fake report."""
        from unittest.mock import patch

        original = catalog.query

        def failing(query, params=None):
            if "silver_fundamentals" in query:
                raise RuntimeError("boom: table locked")
            return original(query, params)

        with patch.object(catalog, "query", side_effect=failing):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(
                    get_data_quality("silver_fundamentals", catalog, "2024-01-01", "2025-12-31")
                )
        assert exc_info.value.status_code == 500
        assert "silver_fundamentals" in exc_info.value.detail

    def test_error_mention_table_and_reason(self, tmp_path) -> None:
        cat = Catalog(db_path=tmp_path / "t.duckdb", repo_root=_REPO_ROOT)
        cat.initialize()
        scorer = DataQualityScorer(cat)
        with pytest.raises(QualityQueryError) as exc_info:
            scorer.score("missing_table_xyz", "2024-01-01", "2025-12-31")
        assert "missing_table_xyz" in str(exc_info.value)


class TestAnomalies:
    def test_anomalies_exclude_indices(self, catalog) -> None:
        """The 30% jump lives on an 880xxx index — must not appear."""
        resp = asyncio.run(get_anomalies("any", catalog))
        ids = {item["asset_id"] for item in resp["items"]}
        assert "SSE:880004" not in ids

    def test_anomalies_include_real_stocks(self, catalog) -> None:
        # Give a real stock a genuine anomaly (rebuild with a jump).
        cat2_assets = ["SSE:600036", "SSE:880004"]
        # insert one big jump for the real stock via direct SQL
        conn = catalog._get_conn()
        conn.execute(
            "INSERT INTO silver_prices_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["SSE:600036", _LAST_DATA_DATE + timedelta(days=1),
             50, 70, 45, 65, 1e6, 6.5e7, 1.0, 65, False, None, None, "test", None],
        )
        resp = asyncio.run(get_anomalies("any", catalog))
        ids = {item["asset_id"] for item in resp["items"]}
        assert "SSE:600036" in ids
        assert "SSE:880004" not in ids
