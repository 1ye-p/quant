"""Regression tests for backtests route fixes (fix round 1).

Covers:
1. ``GET /{run_id}/return-distribution`` is routed (decorator was accidentally
   removed when the regime-timeline endpoint was inserted) and returns 200.
2. Regime history parquet with null ``actual_scale`` values still yields an
   applicable timeline (None values pass through instead of raising and
   discarding the whole series).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.backtest_vector.regime_timeline import is_meaningful_scale_history


def _catalog_with_snapshots() -> MagicMock:
    cat = MagicMock()

    def mock_query(sql, params=None):
        if "gold_portfolio_snapshots" in sql:
            return pl.DataFrame(
                {
                    "trade_date": ["2025-01-01", "2025-01-02", "2025-01-03"],
                    "portfolio_return": [0.01, -0.02, 0.03],
                }
            )
        if "gold_backtest_runs" in sql:
            return pl.DataFrame(
                {"run_id": ["run-123"], "benchmark_asset_id": ["000300.SH"]}
            )
        return pl.DataFrame()

    cat.query.side_effect = mock_query
    cat.execute.return_value = None
    return cat


@pytest.fixture()
def client():
    app.dependency_overrides[deps.get_catalog] = _catalog_with_snapshots
    with TestClient(app) as c:
        yield c
    app.dependency_overrides = {}


class TestReturnDistributionRoute:
    def test_endpoint_registered_and_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v1/backtests/run-123/return-distribution")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "run-123"
        assert body["bins"] == 50
        assert len(body["data"]) == 50
        assert "mean" in body["stats"]


class TestRegimeHistoryNullSafety:
    def test_is_meaningful_scale_history_tolerates_none(self) -> None:
        assert is_meaningful_scale_history([1.0, None, 1.0, None]) is False
        assert is_meaningful_scale_history([1.0, None, 0.5, 1.0]) is True

    def test_null_actual_scale_parquet_keeps_history(
        self, client: TestClient, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        artifacts = tmp_path / "data" / "backtest_artifacts"
        artifacts.mkdir(parents=True)
        pl.DataFrame(
            {
                "trade_date": [f"2025-01-{d:02d}" for d in range(1, 13)],
                "desired_scale": [1.0] * 4 + [0.0] * 4 + [1.0] * 4,
                "actual_scale": [1.0] * 3 + [None] * 5 + [1.0] * 4,
            }
        ).write_parquet(artifacts / "run-null-regime_regime.parquet")

        resp = client.get("/api/v1/backtests/run-null-regime/regime-timeline")
        assert resp.status_code == 200
        body = resp.json()
        assert body["applicable"] is True, (
            "null actual_scale must not discard the whole regime history"
        )
        scale_series = body["scale_series"]
        assert len(scale_series) == 12
        nulls = [s for s in scale_series if s["actual"] is None]
        assert len(nulls) == 5, "null actual_scale values must pass through as None"
