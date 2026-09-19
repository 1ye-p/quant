"""cquant.bt_analyzer.run — Backtest analysis runner with DuckDB persistence.

Loads backtest results, runs AnalysisEngine, persists results to gold tables.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import polars as pl

from cquant.backtest_vector.engine import BacktestResult
from cquant.bt_analyzer.engine import AnalysisEngine
from cquant.bt_analyzer.models import AnalysisReport, AnalysisSpec
from cquant.datahub.catalog import Catalog

logger = logging.getLogger(__name__)


@dataclass
class AnalysisRunSpec:
    """Specification for a backtest analysis run."""

    backtest_run_id: str
    n_oos_windows: int = 5
    oos_fraction: float = 0.2
    n_splits: int = 6
    n_test_splits: int = 2
    embargo_days: int = 0
    benchmark_sharpe: float = 0.0
    n_trials: int = 1
    alpha: float = 0.05


@dataclass
class _ReconstructedSpec:
    """Minimal stand-in for BacktestSpec when rebuilding a result from gold tables.

    AnalysisEngine only touches ``spec.prices`` inside the Brinson attribution
    block (guarded by try/except — skipped when prices are empty), so an empty
    prices frame is safe and keeps the analysis path dependency-free.
    """

    prices: pl.DataFrame = field(default_factory=pl.DataFrame)
    initial_cash: Decimal = Decimal("1_000_000")


def _parse_ts(raw: str | None) -> datetime:
    """Parse an ISO timestamp from gold_backtest_runs; fallback to now(UTC)."""
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _load_or_compute_metrics(
    run_info: dict, ret_df: pl.DataFrame, n_fills: int
) -> "BacktestMetrics":
    """Load metrics from the run's JSON artifact, or recompute from returns."""
    from cquant.backtest_vector.metrics import BacktestMetrics, compute_metrics

    uri = run_info.get("metrics_uri") or ""
    if uri:
        try:
            data = json.loads(pathlib.Path(uri).read_text())
            if isinstance(data, dict):
                valid = {f: data[f] for f in BacktestMetrics._fields if f in data}
                if "sharpe_ratio" in valid:
                    return BacktestMetrics(**valid)
        except Exception as exc:
            logger.debug("Could not read metrics artifact %s: %s", uri, exc)

    returns = ret_df.get_column("portfolio_return").fill_null(0.0)
    return compute_metrics(returns, total_fills=n_fills or None)


def load_result(run_id: str, catalog: Catalog) -> BacktestResult:
    """Reconstruct a minimal ``BacktestResult`` from persisted gold tables.

    Sources:
    - ``gold_backtest_runs``       → run metadata + metrics_uri artifact
    - ``gold_portfolio_snapshots`` → portfolio_returns [trade_date, portfolio_return, nav]
    - ``gold_fills``               → fills for TCA
    - ``gold_signals``             → positions for Brinson (best effort)

    Raises ValueError with a human-readable reason when the artifacts needed
    for analysis are missing (e.g. no portfolio snapshots persisted).
    """
    from cquant.core.enums import EngineType

    run_df = catalog.query(
        "SELECT run_id, engine, strategy_id, metrics_uri, started_at, completed_at "
        "FROM gold_backtest_runs WHERE run_id = ?",
        [run_id],
    )
    if run_df.is_empty():
        raise ValueError(f"Backtest run '{run_id}' not found in gold_backtest_runs")
    run_info = run_df.to_dicts()[0]

    ret_df = catalog.query(
        "SELECT trade_date, portfolio_return, nav FROM gold_portfolio_snapshots "
        "WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )
    if ret_df.is_empty():
        raise ValueError(
            f"No portfolio snapshots persisted for run '{run_id}' — cannot analyze"
        )

    fills_df = catalog.query(
        "SELECT trade_date, asset_id, side, qty, price, notional, commission, "
        "stamp_duty, slippage, total_cost FROM gold_fills "
        "WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    )

    positions_df = pl.DataFrame()
    try:
        positions_df = catalog.query(
            "SELECT trade_date, asset_id, target_weight FROM gold_signals "
            "WHERE signal_set_version = ?",
            [run_id],
        )
    except Exception as exc:
        logger.debug("gold_signals unavailable for run %s: %s", run_id, exc)

    try:
        engine = EngineType(str(run_info.get("engine") or "vector"))
    except ValueError:
        engine = EngineType.VECTOR

    return BacktestResult(
        run_id=run_id,
        engine=engine,
        strategy_id=str(run_info.get("strategy_id") or ""),
        spec=_ReconstructedSpec(),  # type: ignore[arg-type]
        metrics=_load_or_compute_metrics(run_info, ret_df, fills_df.height),
        portfolio_returns=ret_df,
        net_returns=pl.DataFrame(),
        positions=positions_df,
        fills=fills_df,
        started_at=_parse_ts(run_info.get("started_at")),
        completed_at=_parse_ts(run_info.get("completed_at")),
    )


class AnalysisRunner:
    """Run backtest robustness analysis and persist results.

    Usage::

        runner = AnalysisRunner(catalog)
        report = runner.run(
            load_result(run_id, catalog),
            AnalysisRunSpec(backtest_run_id=run_id),
        )
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def run(self, result: BacktestResult, spec: AnalysisRunSpec | None = None) -> AnalysisReport:
        """Execute analysis and persist results. Returns AnalysisReport."""
        self._catalog.initialize()

        analysis_spec = AnalysisSpec(
            n_oos_windows=spec.n_oos_windows if spec else 5,
            oos_fraction=spec.oos_fraction if spec else 0.2,
            n_splits=spec.n_splits if spec else 6,
            n_test_splits=spec.n_test_splits if spec else 2,
            embargo_days=spec.embargo_days if spec else 0,
            benchmark_sharpe=spec.benchmark_sharpe if spec else 0.0,
            n_trials=spec.n_trials if spec else 1,
            alpha=spec.alpha if spec else 0.05,
        )

        engine = AnalysisEngine(analysis_spec)
        report = engine.run(result)

        self._persist_analysis_run(report)
        self._persist_validation_windows(report)
        self._persist_multiple_testing(report)
        self._persist_tca(report)
        self._persist_attribution(report)

        logger.info(
            "Analysis complete: run_id=%s, overfit_score=%.2f, psr=%.2f, dsr=%.2f",
            report.analysis_run_id,
            report.overall_overfit_score.score,
            report.psr,
            report.dsr,
        )
        return report

    def _persist_analysis_run(self, report: AnalysisReport) -> None:
        """Write analysis run metadata to gold_bt_analysis_runs."""
        self._catalog.upsert(
            "gold_bt_analysis_runs",
            ["analysis_run_id", "backtest_run_id", "overall_overfit_score",
             "dsr", "psr", "summary", "created_at"],
            [(
                report.analysis_run_id,
                report.backtest_run_id,
                report.overall_overfit_score.score,
                report.dsr,
                report.psr,
                report.summary,
                report.created_at.isoformat(),
            )],
            ["analysis_run_id"],
        )

    def _persist_validation_windows(self, report: AnalysisReport) -> None:
        """Write walk-forward and CPCV windows to gold_bt_validation_windows."""
        windows = []

        for w in report.walk_forward_windows:
            windows.append({
                "analysis_run_id": report.analysis_run_id,
                "window_id": w.window_id,
                "method": "walk_forward",
                "train_start": w.train_start.isoformat(),
                "train_end": w.train_end.isoformat(),
                "test_start": w.test_start.isoformat(),
                "test_end": w.test_end.isoformat(),
                "metrics_json": json.dumps(w.metrics),
            })

        if report.cpcv_windows:
            for w in report.cpcv_windows:
                windows.append({
                    "analysis_run_id": report.analysis_run_id,
                    "window_id": w.window_id,
                    "method": "cpcv",
                    "train_start": w.train_start.isoformat(),
                    "train_end": w.train_end.isoformat(),
                    "test_start": w.test_start.isoformat(),
                    "test_end": w.test_end.isoformat(),
                    "metrics_json": json.dumps(w.metrics),
                })

        if not windows:
            return

        import polars as pl

        df = pl.DataFrame(windows)
        rows = df.rows()
        try:
            self._catalog.upsert(
                "gold_bt_validation_windows",
                ["analysis_run_id", "window_id", "method", "train_start", "train_end",
                 "test_start", "test_end", "metrics_json"],
                rows,
                ["analysis_run_id", "method", "window_id"],
            )
        except Exception as exc:
            logger.warning("Failed to persist validation windows: %s", exc)

    def _persist_multiple_testing(self, report: AnalysisReport) -> None:
        """Write multiple testing results to gold_bt_multiple_testing."""
        mt = report.multiple_testing_result
        self._catalog.upsert(
            "gold_bt_multiple_testing",
            ["analysis_run_id", "method", "n_trials", "alpha", "results_json", "accepted"],
            [(
                report.analysis_run_id,
                mt.get("method", "holm"),
                mt.get("n_trials", 1),
                mt.get("alpha", 0.05),
                json.dumps(mt),
                mt.get("accepted", False),
            )],
            ["analysis_run_id", "method"],
        )

    def _persist_tca(self, report: AnalysisReport) -> None:
        """Write TCA summary to gold_bt_tca."""
        if not report.tca_summary:
            return
        self._catalog.upsert(
            "gold_bt_tca",
            ["analysis_run_id", "total_turnover", "total_commission", "total_stamp_duty",
             "total_slippage", "total_cost", "cost_per_trade", "cost_pct_turnover",
             "num_trades", "avg_trade_size"],
            [(
                report.analysis_run_id,
                report.tca_summary.total_turnover,
                report.tca_summary.total_commission,
                report.tca_summary.total_stamp_duty,
                report.tca_summary.total_slippage,
                report.tca_summary.total_cost,
                report.tca_summary.cost_per_trade,
                report.tca_summary.cost_as_pct_turnover,
                report.tca_summary.num_trades,
                report.tca_summary.avg_trade_size,
            )],
            ["analysis_run_id"],
        )

    def _persist_attribution(self, report: AnalysisReport) -> None:
        """Write Brinson attribution to gold_bt_attribution."""
        if not report.brinson_attribution:
            return
        br = report.brinson_attribution
        self._catalog.upsert(
            "gold_bt_attribution",
            ["analysis_run_id", "total_return", "benchmark_return", "active_return",
             "allocation_effect", "selection_effect", "interaction_effect",
             "daily_json", "sector_details_json"],
            [(
                report.analysis_run_id,
                br.total_return,
                report.benchmark_return if report.benchmark_return is not None else None,
                report.active_return if report.active_return is not None else None,
                br.allocation_effect,
                br.selection_effect,
                br.interaction_effect,
                json.dumps(report.brinson_daily or []),
                json.dumps(br.sector_details),
            )],
            ["analysis_run_id"],
        )
