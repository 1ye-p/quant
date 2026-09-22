"""cquant.bt_analyzer.walk_forward_refit — Walk-forward analysis with strategy re-fitting.

Provides WalkForwardRefit for rigorous out-of-sample testing with periodic
strategy re-fitting, enabling robust performance estimation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
import polars as pl

from cquant.backtest_vector.engine import BacktestResult, BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.metrics import BacktestMetrics, compute_metrics

logger = logging.getLogger(__name__)


@dataclass
class FoldResult:
    """Result of a single walk-forward fold."""

    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    # Training period metrics (IS)
    train_metrics: dict[str, float]
    # Test period metrics (OOS)
    test_metrics: dict[str, float]
    # Strategy parameters used for this fold
    params: dict[str, Any] = field(default_factory=dict)
    # Whether the fold completed successfully
    success: bool = True
    error: str | None = None


@dataclass
class WalkForwardResult:
    """Result of walk-forward re-fit analysis."""

    # Per-fold results
    folds: list[FoldResult]
    # Aggregated OOS metrics across all folds
    aggregated_metrics: dict[str, float]
    # Consistency metrics
    oos_sharpe_mean: float
    oos_sharpe_std: float
    oos_sharpe_consistency: float  # Fraction of folds with positive OOS Sharpe
    # Overall statistics
    total_folds: int
    successful_folds: int
    # DataFrame with fold-level results
    folds_df: pl.DataFrame

    def summary(self) -> dict:
        """Return summary of walk-forward analysis."""
        return {
            "total_folds": self.total_folds,
            "successful_folds": self.successful_folds,
            "oos_sharpe_mean": self.oos_sharpe_mean,
            "oos_sharpe_std": self.oos_sharpe_std,
            "oos_sharpe_consistency": self.oos_sharpe_consistency,
            "aggregated_metrics": self.aggregated_metrics,
        }

    def is_robust(self, min_consistency: float = 0.6, min_sharpe: float = 0.0) -> bool:
        """Check if the strategy shows robust out-of-sample performance.

        Args:
            min_consistency: Minimum fraction of folds with positive OOS Sharpe.
            min_sharpe: Minimum mean OOS Sharpe ratio.

        Returns:
            True if strategy passes robustness checks.
        """
        return (
            self.oos_sharpe_consistency >= min_consistency
            and self.oos_sharpe_mean >= min_sharpe
        )


class WalkForwardRefit:
    """Walk-forward analysis with periodic strategy re-fitting.

    Splits the backtest period into N folds, re-fits the strategy on each
    training fold, and evaluates on the subsequent out-of-sample period.

    This provides a more rigorous assessment of strategy robustness by
    testing whether the strategy can adapt to changing market conditions.

    Usage::

        refit = WalkForwardRefit(
            base_spec=spec,
            n_folds=5,
            train_ratio=0.7,
            refit_callback=my_refit_function,
        )

        result = refit.run()
        print(result.summary())
    """

    def __init__(
        self,
        base_spec: BacktestSpec,
        n_folds: int = 5,
        train_ratio: float = 0.7,
        gap_days: int = 0,
        refit_callback: Callable[[BacktestSpec, date, date], BacktestSpec] | None = None,
        engine: VectorBacktestEngine | None = None,
        min_fold_days: int = 20,
    ) -> None:
        """Initialize walk-forward re-fit analyzer.

        Args:
            base_spec: Base backtest specification.
            n_folds: Number of walk-forward folds.
            train_ratio: Fraction of each fold used for training.
            gap_days: Gap between train and test periods (to avoid lookahead bias).
            refit_callback: Function to re-fit strategy parameters.
                          Takes (base_spec, train_start, train_end) and returns modified spec.
                          If None, uses the base spec without re-fitting.
            engine: Backtest engine instance.
            min_fold_days: Minimum number of days in each fold.
        """
        if n_folds < 2:
            raise ValueError("n_folds must be >= 2")
        if not 0.1 <= train_ratio <= 0.9:
            raise ValueError("train_ratio must be between 0.1 and 0.9")

        self._base_spec = base_spec
        self._n_folds = n_folds
        self._train_ratio = train_ratio
        self._gap_days = gap_days
        self._refit_callback = refit_callback
        self._engine = engine or VectorBacktestEngine()
        self._min_fold_days = min_fold_days

    def _split_dates(
        self, start: date, end: date
    ) -> list[tuple[date, date, date, date]]:
        """Split the date range into fold boundaries.

        Returns:
            List of (train_start, train_end, test_start, test_end) tuples.
        """
        total_days = (end - start).days
        if total_days < self._min_fold_days * 2:
            raise ValueError(
                f"Date range too short ({total_days} days) for {self._n_folds} folds"
            )

        # Calculate fold size
        fold_days = total_days // self._n_folds
        train_days = int(fold_days * self._train_ratio)
        test_days = fold_days - train_days

        if train_days < self._min_fold_days or test_days < self._min_fold_days:
            raise ValueError(
                f"Fold size too small: train={train_days} days, test={test_days} days"
            )

        folds = []
        current_start = start

        for i in range(self._n_folds):
            train_start = current_start
            train_end = date.fromordinal(train_start.toordinal() + train_days - 1)
            test_start = date.fromordinal(train_end.toordinal() + self._gap_days + 1)
            test_end = date.fromordinal(test_start.toordinal() + test_days - 1)

            # Adjust last fold to include remaining days
            if i == self._n_folds - 1:
                test_end = end

            # Ensure we don't exceed the overall end date
            if test_start > end:
                break
            test_end = min(test_end, end)

            folds.append((train_start, train_end, test_start, test_end))

            # Move to next fold
            current_start = date.fromordinal(test_end.toordinal() + 1)

        return folds

    def _run_fold(
        self,
        fold_id: int,
        train_start: date,
        train_end: date,
        test_start: date,
        test_end: date,
    ) -> FoldResult:
        """Run a single walk-forward fold.

        Args:
            fold_id: Fold identifier.
            train_start: Training period start date.
            train_end: Training period end date.
            test_start: Test period start date.
            test_end: Test period end date.

        Returns:
            FoldResult with fold metrics.
        """
        try:
            # Re-fit strategy if callback provided
            if self._refit_callback:
                spec = self._refit_callback(self._base_spec, train_start, train_end)
            else:
                spec = self._base_spec

            # Run training period backtest (for IS metrics)
            train_spec = BacktestSpec(
                strategy=spec.strategy,
                prices=spec.prices,
                start_date=train_start,
                end_date=train_end,
                # B1: regime 状态机透传（实例由 refit_callback 按 fold 重建，
                # 各 fold 从 initial 状态开始；无 regime 时为 None）
                regime_sm=spec.regime_sm,
                initial_cash=spec.initial_cash,
                cost_model=spec.cost_model,
                sizer=spec.sizer,
                risk_policies=spec.risk_policies,
                rebalance_frequency=spec.rebalance_frequency,
                benchmark_asset_id=spec.benchmark_asset_id,
                universe_id=spec.universe_id,
                features=spec.features,
                tags=spec.tags,
                optimizer=spec.optimizer,
                extra=spec.extra,
            )

            # Run test period backtest (OOS)
            test_spec = BacktestSpec(
                strategy=spec.strategy,
                prices=spec.prices,
                start_date=test_start,
                end_date=test_end,
                # B1: 与 train_spec 同一 fold 实例（callback 已按 fold 重建）
                regime_sm=spec.regime_sm,
                initial_cash=spec.initial_cash,
                cost_model=spec.cost_model,
                sizer=spec.sizer,
                risk_policies=spec.risk_policies,
                rebalance_frequency=spec.rebalance_frequency,
                benchmark_asset_id=spec.benchmark_asset_id,
                universe_id=spec.universe_id,
                features=spec.features,
                tags=spec.tags,
                optimizer=spec.optimizer,
                extra=spec.extra,
            )

            # Run both backtests
            train_result = self._engine.run(train_spec)
            test_result = self._engine.run(test_spec)

            # Extract metrics
            train_metrics = self._extract_metrics(train_result)
            test_metrics = self._extract_metrics(test_result)

            return FoldResult(
                fold_id=fold_id,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_metrics=train_metrics,
                test_metrics=test_metrics,
                success=True,
            )

        except Exception as e:
            logger.warning("Fold %d failed: %s", fold_id, e)
            return FoldResult(
                fold_id=fold_id,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_metrics={},
                test_metrics={},
                success=False,
                error=str(e),
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
            "total_trades": float(m.total_trades),
            "trading_days": float(m.trading_days),
        }

    def _aggregate_metrics(self, folds: list[FoldResult]) -> dict[str, float]:
        """Aggregate metrics across all successful folds.

        Args:
            folds: List of fold results.

        Returns:
            Aggregated metrics dictionary.
        """
        successful = [f for f in folds if f.success and f.test_metrics]
        if not successful:
            return {}

        # Aggregate OOS metrics
        metric_keys = [
            "total_return", "annualized_return", "sharpe_ratio",
            "sortino_ratio", "max_drawdown", "calmar_ratio",
        ]

        aggregated = {}
        for key in metric_keys:
            values = [f.test_metrics.get(key, 0.0) for f in successful]
            if values:
                aggregated[f"oos_{key}_mean"] = float(np.mean(values))
                aggregated[f"oos_{key}_std"] = float(np.std(values))
                aggregated[f"oos_{key}_median"] = float(np.median(values))

        # Compute consistency metrics
        sharpe_values = [f.test_metrics.get("sharpe_ratio", 0.0) for f in successful]
        positive_sharpe = sum(1 for s in sharpe_values if s > 0)
        aggregated["oos_sharpe_consistency"] = positive_sharpe / len(successful) if successful else 0.0

        return aggregated

    def run(self) -> WalkForwardResult:
        """Run walk-forward re-fit analysis.

        Returns:
            WalkForwardResult with per-fold and aggregated metrics.
        """
        start_date = self._base_spec.start_date
        end_date = self._base_spec.end_date

        logger.info(
            "Starting walk-forward re-fit: %d folds, %s to %s",
            self._n_folds, start_date, end_date,
        )

        # Split dates into folds
        try:
            fold_boundaries = self._split_dates(start_date, end_date)
        except ValueError as e:
            logger.error("Failed to split dates: %s", e)
            return WalkForwardResult(
                folds=[],
                aggregated_metrics={},
                oos_sharpe_mean=0.0,
                oos_sharpe_std=0.0,
                oos_sharpe_consistency=0.0,
                total_folds=0,
                successful_folds=0,
                folds_df=pl.DataFrame(),
            )

        # Run each fold
        folds = []
        for i, (train_start, train_end, test_start, test_end) in enumerate(fold_boundaries):
            logger.info(
                "Running fold %d/%d: train=%s to %s, test=%s to %s",
                i + 1, len(fold_boundaries),
                train_start, train_end, test_start, test_end,
            )
            fold_result = self._run_fold(i + 1, train_start, train_end, test_start, test_end)
            folds.append(fold_result)

        # Aggregate results
        aggregated = self._aggregate_metrics(folds)
        successful = [f for f in folds if f.success]

        # Compute summary statistics
        oos_sharpe_values = [f.test_metrics.get("sharpe_ratio", 0.0) for f in successful]
        oos_sharpe_mean = float(np.mean(oos_sharpe_values)) if oos_sharpe_values else 0.0
        oos_sharpe_std = float(np.std(oos_sharpe_values)) if oos_sharpe_values else 0.0
        oos_sharpe_consistency = aggregated.get("oos_sharpe_consistency", 0.0)

        # Build DataFrame
        rows = []
        for f in folds:
            row = {
                "fold_id": f.fold_id,
                "train_start": f.train_start,
                "train_end": f.train_end,
                "test_start": f.test_start,
                "test_end": f.test_end,
                "success": f.success,
                "error": f.error,
            }
            # Add test metrics
            for key, value in f.test_metrics.items():
                row[f"test_{key}"] = value
            # Add train metrics
            for key, value in f.train_metrics.items():
                row[f"train_{key}"] = value
            rows.append(row)

        folds_df = pl.DataFrame(rows) if rows else pl.DataFrame()

        logger.info(
            "Walk-forward complete: %d/%d folds successful, OOS Sharpe=%.3f +/- %.3f",
            len(successful), len(folds), oos_sharpe_mean, oos_sharpe_std,
        )

        return WalkForwardResult(
            folds=folds,
            aggregated_metrics=aggregated,
            oos_sharpe_mean=oos_sharpe_mean,
            oos_sharpe_std=oos_sharpe_std,
            oos_sharpe_consistency=oos_sharpe_consistency,
            total_folds=len(folds),
            successful_folds=len(successful),
            folds_df=folds_df,
        )

    @staticmethod
    def from_backtest_result(
        result: BacktestResult,
        n_folds: int = 5,
        train_ratio: float = 0.7,
        refit_callback: Callable[[BacktestSpec, date, date], BacktestSpec] | None = None,
    ) -> WalkForwardResult:
        """Create walk-forward analysis from an existing backtest result.

        This is a convenience method that extracts the spec from the result
        and runs the walk-forward analysis.

        Args:
            result: Existing backtest result.
            n_folds: Number of walk-forward folds.
            train_ratio: Fraction of each fold used for training.
            refit_callback: Optional strategy re-fit callback.

        Returns:
            WalkForwardResult.
        """
        refit = WalkForwardRefit(
            base_spec=result.spec,
            n_folds=n_folds,
            train_ratio=train_ratio,
            refit_callback=refit_callback,
        )
        return refit.run()
