"""Tests for FactorEvaluator.factor_turnover top-pct quantile semantics (D1-A)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from cquant.factorlab.evaluation import FactorEvaluator


def _make_factor_data(n_dates: int, n_assets: int, seed: int = 42) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(n_dates)]
    assets = [f"A{i:03d}" for i in range(n_assets)]
    rows = []
    for d in dates:
        for a in assets:
            rows.append({"asset_id": a, "trade_date": d, "factor": rng.normal(0, 1)})
    return pl.DataFrame(rows)


def _stable_factor_data(n_dates: int, n_assets: int) -> pl.DataFrame:
    dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(n_dates)]
    assets = [f"A{i:03d}" for i in range(n_assets)]
    rows = []
    for d in dates:
        for i, a in enumerate(assets):
            rows.append({"asset_id": a, "trade_date": d, "factor": float(i)})
    return pl.DataFrame(rows)


def _ev() -> FactorEvaluator:
    return FactorEvaluator(factor_col="factor", return_col="ret_5d")


def _old_top_n_turnover(factors: pl.DataFrame, top_n: int) -> float:
    """Reference implementation of the pre-D1 fixed top_n semantics (anchor)."""
    sorted_dates = sorted(factors["trade_date"].unique().to_list())
    prev_top: set[str] | None = None
    turnovers: list[float] = []
    for d in sorted_dates:
        today = (
            factors.filter(pl.col("trade_date") == d)
            .drop_nulls(["factor"])
            .sort("factor", descending=True)
            .head(top_n)
        )
        top_assets = set(today["asset_id"].to_list())
        if prev_top is not None and len(top_assets) > 0:
            overlap = len(top_assets & prev_top)
            turnovers.append(1.0 - overlap / len(top_assets))
        prev_top = top_assets
    return float(np.mean(turnovers)) if turnovers else 0.0


class TestFactorTurnoverTopPct:
    def test_top20_quantile_n400(self) -> None:
        """N=400 daily cross-section with top_pct=0.2 selects 80 assets."""
        factors = _make_factor_data(n_dates=10, n_assets=400)
        result = _ev().factor_turnover(factors, top_pct=0.2)
        assert 0.0 <= result <= 1.0
        # Quantile width 80 == old fixed top_n=80 on a constant-width panel.
        assert result == pytest.approx(_old_top_n_turnover(factors, top_n=80))
        # And differs from the old default fixed top_n=100 (narrower top -> less churn).
        assert result != pytest.approx(_old_top_n_turnover(factors, top_n=100))

    def test_small_cross_section_min1(self) -> None:
        """N<5 cross-section still selects at least 1 asset (no empty top sets)."""
        factors = _make_factor_data(n_dates=6, n_assets=4)
        result = _ev().factor_turnover(factors, top_pct=0.2)
        # max(1, int(0.2*4)) = 1 asset per day -> turnover well-defined on multi-date data.
        assert 0.0 <= result <= 1.0
        assert result == pytest.approx(_old_top_n_turnover(factors, top_n=1))

    def test_daily_missing_keeps_quantile(self) -> None:
        """Per-day nulls shrink that day's denominator: top set recomputed from
        the surviving cross-section, not from a fixed global width."""
        n_dates, n_assets = 6, 10
        factors = _stable_factor_data(n_dates, n_assets)
        # Higher factor value = better rank: top 20% = assets ranked 9, 8.
        # On day index 2, null out the top asset (A009) -> that day's non-null
        # cross-section is 9, so the quantile head is max(1, int(0.2*9)) = 1 asset
        # ({A008}); full days keep top 2 ({A009, A008}).
        mid_date = date(2025, 1, 1) + timedelta(days=2 * 5)
        factors = factors.with_columns(
            pl.when((pl.col("trade_date") == mid_date) & (pl.col("asset_id") == "A009"))
            .then(None)
            .otherwise(pl.col("factor"))
            .alias("factor")
        )
        result = _ev().factor_turnover(factors, top_pct=0.2)
        # Transitions: d0->d1: {9,8}->{9,8} = 0; d1->d2: {9,8}->{8} = 0 (subset);
        # d2->d3: {8}->{9,8} = 0.5; d3->d4, d4->d5: 0. Mean = 0.1.
        assert result == pytest.approx(0.1)

    def test_anchor_n500_equals_old_top100(self) -> None:
        """N=500 with top_pct=0.2 matches old fixed top_n=100 (calibration anchor)."""
        factors = _make_factor_data(n_dates=12, n_assets=500)
        result = _ev().factor_turnover(factors, top_pct=0.2)
        assert result == pytest.approx(_old_top_n_turnover(factors, top_n=100))
