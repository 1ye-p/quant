"""Tests for advanced factor evaluation: Rank IC decay, turnover, quantile returns."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from cquant.factorlab.evaluation import FactorEvaluator


def _make_factor_data(n_dates: int = 20, n_assets: int = 50, seed: int = 42) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(n_dates)]
    assets = [f"A{i:03d}" for i in range(n_assets)]
    rows = []
    for d in dates:
        for a in assets:
            rows.append({"asset_id": a, "trade_date": d, "factor": rng.normal(0, 1)})
    return pl.DataFrame(rows)


def _make_return_data(factor_df: pl.DataFrame, noise: float = 0.5, seed: int = 99) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    noise_arr = rng.normal(0, noise, len(factor_df))
    return factor_df.with_columns(
        (pl.col("factor") * 0.1 + pl.Series("_n", noise_arr)).alias("ret_5d")
    ).select(["asset_id", "trade_date", "ret_5d"])


class TestRankICDecay:
    def test_returns_dataframe_with_lag_and_ic_columns(self) -> None:
        factors = _make_factor_data(20, 50)
        returns = _make_return_data(factors)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.rank_ic_decay(factors, returns, max_lag=3)
        assert "lag" in result.columns
        assert "ic" in result.columns
        assert len(result) <= 3

    def test_lag_1_returns_at_most_max_lag_rows(self) -> None:
        factors = _make_factor_data(20, 50)
        returns = _make_return_data(factors)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.rank_ic_decay(factors, returns, max_lag=5)
        assert len(result) <= 5

    def test_ic_values_are_bounded(self) -> None:
        factors = _make_factor_data(20, 50)
        returns = _make_return_data(factors)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.rank_ic_decay(factors, returns, max_lag=3)
        for ic_val in result["ic"].to_list():
            assert -1.0 <= ic_val <= 1.0


class TestFactorTurnover:
    def test_returns_float_between_0_and_1(self) -> None:
        factors = _make_factor_data(20, 50)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.factor_turnover(factors, top_pct=0.2)
        assert 0.0 <= result <= 1.0

    def test_stable_rankings_give_low_turnover(self) -> None:
        dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(10)]
        assets = [f"A{i:03d}" for i in range(50)]
        rows = []
        asset_vals = {a: float(i) for i, a in enumerate(assets)}
        for d in dates:
            for a in assets:
                rows.append({"asset_id": a, "trade_date": d, "factor": asset_vals[a]})
        factors = pl.DataFrame(rows)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.factor_turnover(factors, top_pct=0.2)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_random_rankings_give_higher_turnover(self) -> None:
        rng = np.random.default_rng(42)
        dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(20)]
        assets = [f"A{i:03d}" for i in range(100)]
        rows = []
        for d in dates:
            for a in assets:
                rows.append({"asset_id": a, "trade_date": d, "factor": float(rng.normal(0, 1))})
        factors = pl.DataFrame(rows)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.factor_turnover(factors, top_pct=0.2)
        assert result > 0.2


class TestQuantileReturns:
    def test_returns_dataframe_with_quantile_and_mean_return_columns(self) -> None:
        factors = _make_factor_data(20, 50)
        returns = _make_return_data(factors)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.quantile_returns(factors, returns, n_quantiles=5)
        assert "quantile" in result.columns
        assert "mean_return" in result.columns
        assert len(result) == 5

    def test_quantile_numbers_are_1_to_n(self) -> None:
        factors = _make_factor_data(20, 50)
        returns = _make_return_data(factors)
        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.quantile_returns(factors, returns, n_quantiles=5)
        assert set(result["quantile"].to_list()) == {1, 2, 3, 4, 5}

    def test_top_quantile_higher_return_than_bottom_with_strong_signal(self) -> None:
        rng = np.random.default_rng(42)
        n_dates, n_assets = 30, 100
        dates = [date(2025, 1, 1) + timedelta(days=i * 5) for i in range(n_dates)]
        assets = [f"A{i:03d}" for i in range(n_assets)]
        rows = []
        for d in dates:
            for a in assets:
                f_val = rng.normal(0, 1)
                rows.append({
                    "asset_id": a,
                    "trade_date": d,
                    "factor": f_val,
                    "ret_5d": f_val * 0.5 + rng.normal(0, 0.1),
                })
        df = pl.DataFrame(rows)
        factors_df = df.select(["asset_id", "trade_date", "factor"])
        returns_df = df.select(["asset_id", "trade_date", "ret_5d"])

        ev = FactorEvaluator(factor_col="factor", return_col="ret_5d")
        result = ev.quantile_returns(factors_df, returns_df, n_quantiles=5)
        q1_ret = result.filter(pl.col("quantile") == 1)["mean_return"][0]
        q5_ret = result.filter(pl.col("quantile") == 5)["mean_return"][0]
        assert q5_ret > q1_ret
