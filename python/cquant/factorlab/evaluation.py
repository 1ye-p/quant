"""Factor evaluation framework — IC, IC IR, and summary metrics."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl
from scipy import stats
from scipy.stats import pearsonr, spearmanr, t as student_t


@dataclass
class FactorEvaluator:
    """Compute Information Coefficient (IC) and derived metrics for a factor.

    Parameters
    ----------
    factor_col:
        Column name holding factor values in the *factors* DataFrame.
    return_col:
        Column name holding forward returns in the *returns* DataFrame.
    method:
        ``"rank"`` (default) uses Spearman rank correlation;
        ``"pearson"`` uses Pearson correlation.
    """

    factor_col: str
    return_col: str
    method: str = "rank"
    factor_name: str = field(default="")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _corr_fn(self):
        if self.method == "rank":
            return spearmanr
        elif self.method == "pearson":
            return pearsonr
        else:
            raise ValueError(f"Unknown method '{self.method}'; use 'rank' or 'pearson'")

    def _join(
        self, factors: pl.DataFrame, returns: pl.DataFrame
    ) -> pl.DataFrame:
        return factors.join(
            returns, on=["asset_id", "trade_date"], how="inner"
        )

    def _ic_per_date(self, joined: pl.DataFrame) -> list[dict]:
        corr = self._corr_fn()
        results: list[dict] = []
        for dt, group in joined.group_by("trade_date", maintain_order=True):
            fv = group[self.factor_col].to_numpy()
            ret = group[self.return_col].to_numpy()
            if len(fv) < 3:
                continue
            r, _ = corr(fv, ret)
            if np.isnan(r):
                continue
            results.append({"trade_date": dt, "ic": float(r)})
        return results

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def ic_series(self, factors: pl.DataFrame, returns: pl.DataFrame) -> pl.DataFrame:
        """Compute per-date IC.

        Returns a DataFrame with columns ``[trade_date, ic]``.
        Dates with fewer than 3 observations are skipped.
        """
        joined = self._join(factors, returns)
        rows = self._ic_per_date(joined)
        return pl.DataFrame(rows).sort("trade_date")

    def mean_ic(self, factors: pl.DataFrame, returns: pl.DataFrame) -> float:
        """Mean IC across all evaluated dates."""
        return float(self.ic_series(factors, returns)["ic"].mean())

    def net_ic(
        self,
        factors: pl.DataFrame,
        returns: pl.DataFrame,
        cost_rate: float = 0.003,
    ) -> float:
        """Net IC = mean_ic - cost_rate × factor_turnover.

        Penalizes the raw IC by the expected transaction-cost drag from
        factor turnover. High-turnover factors show a net IC noticeably
        below their raw IC; low-turnover factors stay close to raw IC.

        Parameters
        ----------
        cost_rate:
            Round-trip cost per unit of turnover (default 30 bps). The
            turnover fraction is in [0, 1], so the penalty is
            ``cost_rate * turnover`` in IC units.
        """
        raw_ic = self.mean_ic(factors, returns)
        turnover = self.factor_turnover(factors)
        return raw_ic - cost_rate * turnover

    def ic_ir(self, factors: pl.DataFrame, returns: pl.DataFrame) -> float:
        """IC Information Ratio: mean(IC) / std(IC)."""
        ic = self.ic_series(factors, returns)["ic"].to_numpy()
        if len(ic) < 2 or np.std(ic) == 0:
            return float("nan")
        return float(np.mean(ic) / np.std(ic))

    def ic_positive_pct(
        self, factors: pl.DataFrame, returns: pl.DataFrame
    ) -> float:
        """Percentage of dates where IC > 0."""
        ic = self.ic_series(factors, returns)["ic"].to_numpy()
        if len(ic) == 0:
            return 0.0
        return float(np.mean(ic > 0) * 100)

    @staticmethod
    def _newey_west_se(x: np.ndarray, max_lag: int | None = None) -> float:
        """Newey-West HAC standard error of the mean of ``x``.

        Corrects for autocorrelation in the IC series — a naive standard
        error underestimates variance (and thus overstates significance)
        when IC is persistent over time.
        """
        x = np.asarray(x, dtype=float)
        n = len(x)
        if n < 2:
            return float("nan")
        if max_lag is None:
            # Newey-West rule of thumb.
            max_lag = int(4 * (n / 100) ** (2 / 9))
            max_lag = max(1, min(max_lag, n - 1))

        x_demean = x - x.mean()
        # Gamma_0 term (n * sample variance of the mean).
        omega = float(np.sum(x_demean ** 2)) / n
        # Weighted autocovariance terms (Bartlett kernel).
        for lag in range(1, max_lag + 1):
            weight = 1.0 - lag / (max_lag + 1)
            gamma = float(np.sum(x_demean[lag:] * x_demean[:-lag])) / n
            omega += 2.0 * weight * gamma

        se = float(np.sqrt(omega / n))
        return se if se > 0 else float("nan")

    def ic_ttest(
        self,
        ic_series: pl.DataFrame | np.ndarray,
        max_lag: int | None = None,
    ) -> dict:
        """t-test for mean(IC) ≠ 0 with Newey-West HAC standard errors.

        Parameters
        ----------
        ic_series:
            Either a Polars DataFrame with an ``ic`` column (as produced by
            :meth:`ic_series`) or a 1-D numpy array of per-date IC values.
        max_lag:
            Maximum lag for the Bartlett kernel. Defaults to the Newey-West
            rule of thumb ``int(4 * (n/100) ** (2/9))``.

        Returns
        -------
        dict with keys ``t_stat``, ``p_value``, ``ci_lower``, ``ci_upper``,
        ``n``. ``p_value`` uses a two-sided test against the Student-t
        distribution with ``n - 1`` degrees of freedom.
        """
        if isinstance(ic_series, pl.DataFrame):
            ic = ic_series["ic"].to_numpy()
        else:
            ic = np.asarray(ic_series, dtype=float)
        ic = ic[~np.isnan(ic)]

        n = len(ic)
        if n == 0:
            return {
                "t_stat": float("nan"),
                "p_value": float("nan"),
                "ci_lower": float("nan"),
                "ci_upper": float("nan"),
                "n": 0,
            }

        mean_ic = float(np.mean(ic))
        se = self._newey_west_se(ic, max_lag=max_lag)
        if not np.isfinite(se):
            return {
                "t_stat": float("nan"),
                "p_value": float("nan"),
                "ci_lower": float("nan"),
                "ci_upper": float("nan"),
                "n": n,
            }

        t_stat = mean_ic / se
        df = n - 1
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df)))
        # 95% confidence interval on the mean.
        t_crit = float(student_t.ppf(0.975, df))
        return {
            "t_stat": float(t_stat),
            "p_value": p_value,
            "ci_lower": mean_ic - t_crit * se,
            "ci_upper": mean_ic + t_crit * se,
            "n": n,
        }

    def ic_significant(
        self,
        ic_series: pl.DataFrame | np.ndarray,
        alpha: float = 0.05,
    ) -> bool:
        """Whether mean(IC) is statistically significant.

        True when ``p_value < alpha`` **and** ``n >= 30`` (the n>=30 guard
        ensures the CLT-based inference is trustworthy).
        """
        res = self.ic_ttest(ic_series)
        if res["n"] < 30:
            return False
        p = res["p_value"]
        return bool(np.isfinite(p) and p < alpha)

    def half_life(
        self,
        ic_series: pl.DataFrame | np.ndarray,
    ) -> float | None:
        """Half-life of IC decay in periods (days).

        Fits ``IC(lag) = IC0 * exp(-k * lag)`` via linear regression on
        ``ln(IC(lag))`` and returns ``ln(2) / k``. Requires at least two
        strictly positive decay IC points; returns ``None`` if it cannot
        be estimated.

        Note: callers must pass the *decay* IC series (IC measured at
        increasing forward lags), not the raw per-date IC series. See
        :meth:`rank_ic_decay`.
        """
        if isinstance(ic_series, pl.DataFrame):
            arr = ic_series["ic"].to_numpy()
        else:
            arr = np.asarray(ic_series, dtype=float)
        arr = arr[np.isfinite(arr)]
        # Keep strictly positive ICs (log requires positivity).
        positive = arr[arr > 0]
        if len(positive) < 3:
            # polyfit on 2 points is unstable; need a meaningful trend.
            return None

        lags = np.arange(1, len(positive) + 1, dtype=float)
        log_ic = np.log(positive)
        # Linear fit: log(IC) = a + b*lag  =>  k = -b
        slope, _intercept = np.polyfit(lags, log_ic, 1)
        # slope >= 0 means non-decaying (or growing) IC — half-life
        # undefined. Use a small negative tolerance to reject flat / noisy
        # series that would otherwise produce an absurdly large half-life.
        if slope >= -1e-9:
            return None
        k = -slope
        return float(np.log(2) / k)


    def summary(self, factors: pl.DataFrame, returns: pl.DataFrame) -> dict:
        """Return a summary dict with all evaluation metrics."""
        ic = self.ic_series(factors, returns)["ic"].to_numpy()
        dates_evaluated = len(ic)
        mean = float(np.mean(ic)) if dates_evaluated else 0.0
        std = float(np.std(ic)) if dates_evaluated >= 2 else 0.0
        ic_ir = mean / std if std != 0 else float("nan")
        ic_pos = (
            float(np.mean(ic > 0) * 100)
            if dates_evaluated
            else 0.0
        )
        return {
            "factor_name": self.factor_name or self.factor_col,
            "method": self.method,
            "mean_ic": mean,
            "ic_ir": ic_ir,
            "ic_positive_pct": ic_pos,
            "dates_evaluated": dates_evaluated,
        }

    def rank_ic_decay(
        self,
        factors: pl.DataFrame,
        returns: pl.DataFrame,
        max_lag: int = 5,
    ) -> pl.DataFrame:
        """Compute IC at different forward lags to measure factor persistence.

        For each lag k from 1 to max_lag, computes the IC between factor
        values at date t and returns at the date k periods later.

        Returns a DataFrame with columns [lag, ic].
        """
        unique_dates = sorted(factors["trade_date"].unique().to_list())
        n_dates = len(unique_dates)
        date_to_idx = {d: i for i, d in enumerate(unique_dates)}
        corr = self._corr_fn()

        results: list[dict] = []
        for lag in range(1, max_lag + 1):
            if lag >= n_dates:
                break
            # Build date shift map: date_t -> date_{t+lag}
            date_shift = {
                d: unique_dates[date_to_idx[d] + lag]
                for d in unique_dates
                if date_to_idx[d] + lag < n_dates
            }
            if not date_shift:
                break

            # Shift factor dates forward by lag
            shifted = factors.with_columns(
                pl.col("trade_date")
                .map_elements(lambda d: date_shift.get(d), return_dtype=pl.Date)
                .alias("shifted_date")
            ).drop_nulls("shifted_date")

            # Join factors[trade_date] with returns[trade_date == shifted_date]
            joined = shifted.join(
                returns.rename({"trade_date": "shifted_date"}),
                on=["asset_id", "shifted_date"],
                how="inner",
            )
            if joined.is_empty() or len(joined) < 3:
                continue

            fv = joined[self.factor_col].to_numpy()
            ret = joined[self.return_col].to_numpy()
            r, _ = corr(fv, ret)
            if not np.isnan(r):
                results.append({"lag": lag, "ic": float(r)})

        if not results:
            return pl.DataFrame({"lag": pl.Series([], dtype=pl.Int32),
                                  "ic": pl.Series([], dtype=pl.Float64)})
        return pl.DataFrame(results).sort("lag")

    def factor_turnover(self, factors: pl.DataFrame, top_pct: float = 0.2) -> float:
        """Average fraction of top-quantile assets that change between consecutive periods.

        Each day selects the top ``max(1, int(top_pct * n_daily))`` assets, where
        ``n_daily`` is that day's non-null cross-section count (denominator =
        daily cross-section, not a global unique count).

        0.0 = perfectly stable rankings. 1.0 = complete turnover each period.
        """
        sorted_dates = sorted(factors["trade_date"].unique().to_list())
        if len(sorted_dates) < 2:
            return 0.0

        prev_top: set[str] | None = None
        turnovers: list[float] = []

        for d in sorted_dates:
            day = factors.filter(pl.col("trade_date") == d).drop_nulls([self.factor_col])
            n_daily = len(day)
            today = day.sort(self.factor_col, descending=True).head(max(1, int(top_pct * n_daily)))
            top_assets = set(today["asset_id"].to_list())

            if prev_top is not None and len(top_assets) > 0:
                overlap = len(top_assets & prev_top)
                turnover = 1.0 - overlap / len(top_assets)
                turnovers.append(turnover)

            prev_top = top_assets

        return float(np.mean(turnovers)) if turnovers else 0.0

    def quantile_returns(
        self,
        factors: pl.DataFrame,
        returns: pl.DataFrame,
        n_quantiles: int = 5,
    ) -> pl.DataFrame:
        """Compute mean return by factor quantile.

        Assets are sorted by factor value and divided into n_quantiles groups
        (1=lowest, n=highest). Returns mean return per quantile aggregated
        across all dates.

        Returns a DataFrame with columns [quantile, mean_return].
        """
        joined = self._join(factors, returns)

        rows: list[dict] = []
        for dt, group in joined.group_by("trade_date", maintain_order=True):
            n = len(group)
            if n < n_quantiles:
                continue
            sorted_group = group.sort(self.factor_col)
            q_size = n // n_quantiles
            for q in range(n_quantiles):
                start = q * q_size
                end = (q + 1) * q_size if q < n_quantiles - 1 else n
                slice_df = sorted_group.slice(start, end - start)
                mean_ret = slice_df[self.return_col].mean()
                if mean_ret is not None:
                    rows.append({"quantile": q + 1, "mean_return": float(mean_ret)})

        if not rows:
            return pl.DataFrame({
                "quantile": pl.Series([], dtype=pl.Int32),
                "mean_return": pl.Series([], dtype=pl.Float64),
            })

        df = pl.DataFrame(rows)
        return (
            df.group_by("quantile")
            .agg(pl.col("mean_return").mean())
            .sort("quantile")
        )

    def qlib_risk_analysis(self, returns: pl.Series) -> dict | None:
        """使用 Qlib 计算年化风险指标。

        通过 ``cquant.qlib_bridge.risk_analysis.qlib_risk_analysis()``
        桥接层调用 Qlib 的 ``risk_analysis()``，不直接 import qlib。

        Parameters
        ----------
        returns:
            每日收益率 Polars Series。

        Returns
        -------
        包含以下键的字典，或在 returns 为空时返回 ``None``：

        - ``annualized_return``: 年化收益率
        - ``information_ratio``: 信息比率（夏普，无风险=0）
        - ``max_drawdown``: 最大回撤（负值）
        - ``mean``: 日均收益
        - ``std``: 日收益标准差
        """
        if returns.is_empty():
            return None

        from cquant.qlib_bridge.risk_analysis import (
            qlib_risk_analysis as _qlib_risk_analysis,
        )

        return _qlib_risk_analysis(returns.to_numpy())
