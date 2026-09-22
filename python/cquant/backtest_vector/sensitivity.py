"""cquant.backtest_vector.sensitivity — Parameter sensitivity analysis for backtests.

Provides GridSearchSensitivity for systematic parameter exploration across
backtest strategies, enabling robustness assessment and parameter optimization.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import polars as pl

from cquant.backtest_vector.engine import BacktestResult, BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.metrics import BacktestMetrics

logger = logging.getLogger(__name__)


@dataclass
class ParameterGrid:
    """Defines a grid of parameters to explore.

    Example::

        grid = ParameterGrid({
            "top_n": [5, 10, 15, 20],
            "lookback": [20, 40, 60],
        })
    """

    params: dict[str, list[Any]]

    def combinations(self) -> list[dict[str, Any]]:
        """Generate all parameter combinations."""
        keys = list(self.params.keys())
        values = list(self.params.values())
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]

    def __len__(self) -> int:
        """Total number of combinations."""
        count = 1
        for v in self.params.values():
            count *= len(v)
        return count


@dataclass
class SensitivityResult:
    """Result of a parameter sensitivity analysis."""

    # Parameter combinations tested
    combinations: list[dict[str, Any]]
    # Metrics for each combination
    metrics: list[dict[str, float]]
    # Best combination by primary metric
    best_params: dict[str, Any]
    best_metric_value: float
    # Robustness score (0-1, higher is more robust)
    robustness_score: float
    # DataFrame with all results
    results_df: pl.DataFrame

    def summary(self) -> dict:
        """Return summary statistics of the sensitivity analysis."""
        return {
            "total_combinations": len(self.combinations),
            "best_params": self.best_params,
            "best_metric_value": self.best_metric_value,
            "robustness_score": self.robustness_score,
            "metric_std": float(self.results_df["primary_metric"].std()) if not self.results_df.is_empty() else 0.0,
            "metric_range": float(
                self.results_df["primary_metric"].max() - self.results_df["primary_metric"].min()
            ) if not self.results_df.is_empty() else 0.0,
        }


class GridSearchSensitivity:
    """Grid search parameter sensitivity analysis for backtests.

    Runs backtests for all combinations of parameters in a grid and computes
    robustness metrics to identify stable parameter regions.

    Usage::

        grid = ParameterGrid({
            "top_n": [5, 10, 15, 20],
            "sort_factor": ["ret_20d", "ret_5d"],
        })

        analyzer = GridSearchSensitivity(
            base_spec=spec,
            param_grid=grid,
            primary_metric="sharpe_ratio",
        )

        result = analyzer.run(catalog)
        print(result.summary())
    """

    def __init__(
        self,
        base_spec: BacktestSpec,
        param_grid: ParameterGrid,
        primary_metric: str = "sharpe_ratio",
        engine: VectorBacktestEngine | None = None,
        max_workers: int = 1,
        regime_sm_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Initialize sensitivity analyzer.

        Args:
            base_spec: Base backtest specification to modify for each run.
            param_grid: Grid of parameters to explore.
            primary_metric: Metric to optimize (default: sharpe_ratio).
            engine: Backtest engine instance (creates new one if None).
            max_workers: Maximum parallel workers (default: 1, sequential).
            regime_sm_factory: Optional factory producing a fresh regime state
                machine per variant spec. Variants run sequentially on one
                engine, so the state machine must NOT be shared across them
                (latch/hold state would leak) — pass a factory (e.g.
                ``lambda: runner._regime_sm_for_strategy(strategy)``) instead
                of reusing ``base_spec.regime_sm`` directly.
        """
        self._base_spec = base_spec
        self._param_grid = param_grid
        self._primary_metric = primary_metric
        self._engine = engine or VectorBacktestEngine()
        self._max_workers = max_workers
        self._regime_sm_factory = regime_sm_factory

    def _create_spec_with_params(self, params: dict[str, Any]) -> BacktestSpec:
        """Create a new BacktestSpec with modified parameters.

        Args:
            params: Parameter values to apply.

        Returns:
            Modified BacktestSpec.
        """
        # Deep copy the base spec by recreating it
        # Strategy parameters are passed via extra dict
        new_extra = {**self._base_spec.extra, **params}

        return BacktestSpec(
            strategy=self._base_spec.strategy,
            prices=self._base_spec.prices,
            start_date=self._base_spec.start_date,
            end_date=self._base_spec.end_date,
            initial_cash=self._base_spec.initial_cash,
            cost_model=self._base_spec.cost_model,
            sizer=self._base_spec.sizer,
            risk_policies=self._base_spec.risk_policies,
            rebalance_frequency=self._base_spec.rebalance_frequency,
            benchmark_asset_id=self._base_spec.benchmark_asset_id,
            universe_id=self._base_spec.universe_id,
            features=self._base_spec.features,
            tags=self._base_spec.tags,
            optimizer=self._base_spec.optimizer,
            extra=new_extra,
            random_seed=self._base_spec.random_seed,
            # regime_sm: fresh instance per variant via factory (variants run
            # sequentially — sharing one machine would leak latch state).
            regime_sm=(
                self._regime_sm_factory()
                if self._regime_sm_factory is not None
                else None
            ),
        )

    def _extract_metrics(self, result: BacktestResult) -> dict[str, float]:
        """Extract key metrics from a backtest result.

        Args:
            result: Completed backtest result.

        Returns:
            Dictionary of metric name to value.
        """
        m = result.metrics
        return {
            "total_return": m.total_return,
            "annualized_return": m.annualized_return,
            "sharpe_ratio": m.sharpe_ratio,
            "sortino_ratio": m.sortino_ratio,
            "max_drawdown": m.max_drawdown,
            "calmar_ratio": m.calmar_ratio,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor if m.profit_factor != float("inf") else 999.0,
            "var_95": m.var_95,
            "cvar_95": m.cvar_95,
            "total_trades": float(m.total_trades),
            "trading_days": float(m.trading_days),
        }

    def _compute_robustness_score(
        self,
        metrics_list: list[dict[str, float]],
        best_value: float,
    ) -> float:
        """Compute robustness score for the parameter space.

        Robustness is measured as the fraction of parameter combinations that
        achieve at least 80% of the best metric value, combined with the
        coefficient of variation of the primary metric.

        Args:
            metrics_list: List of metrics for each combination.
            best_value: Best value of the primary metric.

        Returns:
            Robustness score between 0 and 1.
        """
        if not metrics_list or best_value == 0:
            return 0.0

        primary_values = [m[self._primary_metric] for m in metrics_list]

        # Fraction of combinations achieving >= 80% of best
        threshold = best_value * 0.8
        above_threshold = sum(1 for v in primary_values if v >= threshold)
        fraction_above = above_threshold / len(primary_values)

        # Coefficient of variation (lower is more robust)
        mean_val = np.mean(primary_values)
        std_val = np.std(primary_values)
        cv = std_val / abs(mean_val) if abs(mean_val) > 1e-10 else float("inf")

        # Robustness score: high fraction above threshold + low CV
        # Normalize CV to 0-1 range (CV of 1.0 = 0.5 score)
        cv_score = 1.0 / (1.0 + cv)

        # Combine: 60% fraction + 40% CV score
        robustness = 0.6 * fraction_above + 0.4 * cv_score

        return min(1.0, max(0.0, robustness))

    def run(self, catalog=None) -> SensitivityResult:
        """Run sensitivity analysis for all parameter combinations.

        Args:
            catalog: Optional catalog for data access.

        Returns:
            SensitivityResult with all results and robustness metrics.
        """
        combinations = self._param_grid.combinations()
        total = len(combinations)

        logger.info(
            "Starting sensitivity analysis: %d combinations, primary_metric=%s",
            total, self._primary_metric,
        )

        results = []
        metrics_list = []

        for i, params in enumerate(combinations):
            logger.debug("Running combination %d/%d: %s", i + 1, total, params)

            try:
                spec = self._create_spec_with_params(params)
                result = self._engine.run(spec)

                metrics = self._extract_metrics(result)
                metrics_list.append(metrics)

                # Build result row
                row = {**params, **metrics, "primary_metric": metrics.get(self._primary_metric, 0.0)}
                results.append(row)

            except Exception as e:
                logger.warning("Combination %d failed: %s", i + 1, e)
                # Add failed result with NaN metrics
                row = {**params, "primary_metric": float("nan")}
                for key in ["total_return", "annualized_return", "sharpe_ratio",
                           "sortino_ratio", "max_drawdown", "calmar_ratio",
                           "win_rate", "profit_factor", "var_95", "cvar_95",
                           "total_trades", "trading_days"]:
                    row[key] = float("nan")
                results.append(row)
                metrics_list.append({})

        # Build results DataFrame
        if results:
            results_df = pl.DataFrame(results)
        else:
            results_df = pl.DataFrame()

        # Find best combination
        if metrics_list and any(m for m in metrics_list):
            valid_metrics = [m for m in metrics_list if m and self._primary_metric in m]
            if valid_metrics:
                best_idx = max(
                    range(len(valid_metrics)),
                    key=lambda i: valid_metrics[i].get(self._primary_metric, float("-inf"))
                )
                best_params = combinations[best_idx]
                best_metric_value = valid_metrics[best_idx][self._primary_metric]
            else:
                best_params = {}
                best_metric_value = 0.0
        else:
            best_params = {}
            best_metric_value = 0.0

        # Compute robustness score
        valid_metrics = [m for m in metrics_list if m and self._primary_metric in m]
        robustness_score = self._compute_robustness_score(valid_metrics, best_metric_value)

        logger.info(
            "Sensitivity analysis complete: best_%s=%.4f, robustness=%.3f",
            self._primary_metric, best_metric_value, robustness_score,
        )

        return SensitivityResult(
            combinations=combinations,
            metrics=metrics_list,
            best_params=best_params,
            best_metric_value=best_metric_value,
            robustness_score=robustness_score,
            results_df=results_df,
        )


def compute_robustness_score(
    results_df: pl.DataFrame,
    primary_metric: str = "sharpe_ratio",
) -> float:
    """Compute robustness score from a sensitivity analysis results DataFrame.

    This is a standalone function for computing robustness from pre-computed results.

    Args:
        results_df: DataFrame with sensitivity analysis results.
        primary_metric: Name of the primary metric column.

    Returns:
        Robustness score between 0 and 1.
    """
    if results_df.is_empty() or primary_metric not in results_df.columns:
        return 0.0

    values = results_df[primary_metric].drop_nulls().to_list()
    if not values:
        return 0.0

    best_value = max(values)
    if best_value == 0:
        return 0.0

    # Fraction achieving >= 80% of best
    threshold = best_value * 0.8
    above_threshold = sum(1 for v in values if v >= threshold)
    fraction_above = above_threshold / len(values)

    # Coefficient of variation
    mean_val = np.mean(values)
    std_val = np.std(values)
    cv = std_val / abs(mean_val) if abs(mean_val) > 1e-10 else float("inf")

    cv_score = 1.0 / (1.0 + cv)

    robustness = 0.6 * fraction_above + 0.4 * cv_score
    return min(1.0, max(0.0, robustness))
