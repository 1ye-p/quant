"""Multi-factor weighted strategy — z-scored composite ranking."""

from __future__ import annotations

import logging

import polars as pl

logger = logging.getLogger(__name__)

from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.core.types import SignalFrame


class MultiFactorStrategy(Strategy):
    """Combine z-scored factors with configurable weights into a composite score.

    Parameters
    ----------
    strategy_id : str
        Unique identifier for this strategy instance.
    factor_weights : dict[str, float]
        Mapping of factor name to weight.  Positive weight = higher factor
        value produces a higher composite score; negative weight inverts.
    top_n : int
        Number of top-ranked assets to emit signals for.
    missing_factor_strategy : str
        Strategy for handling missing factor values. Options:
        - "fill_0": Fill missing values with 0 (default)
        - "fill_median": Fill missing values with daily median
        - "exclude": Drop assets with missing factors
        - "risk_penalty": Don't fill; penalise composite score per missing factor
    penalty_per_missing : float
        Points subtracted from composite score for each missing factor
        (only used when missing_factor_strategy == "risk_penalty").

    Optional capability (B2, engine-detected via getattr — not on the ABC):
    ``last_score_detail`` / ``missing_factors`` are reset at the start of
    every ``generate_signals`` call and populated only on the success path.
    """

    #: Per-call score detail: columns [asset_id, score, rank, _w_{factor}...].
    #: The `_w_` prefix is kept as-is; consumers strip it if needed.
    last_score_detail: pl.DataFrame | None
    #: Unique factor names missing from the feature table for the latest call.
    missing_factors: list[str]

    def __init__(
        self,
        strategy_id: str,
        factor_weights: dict[str, float],
        top_n: int = 10,
        missing_factor_strategy: str = "fill_0",
        penalty_per_missing: float = 0.5,
    ) -> None:
        self._strategy_id = strategy_id
        self._factor_weights = factor_weights
        self._top_n = top_n
        self._missing_factor_strategy = missing_factor_strategy
        self._penalty_per_missing = penalty_per_missing
        self.last_score_detail = None
        self.missing_factors = []

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    # ------------------------------------------------------------------
    def _handle_missing_factors(self, day_features: pl.DataFrame, factor_weights: dict) -> pl.DataFrame:
        """Handle missing factor values based on configured strategy.

        Parameters
        ----------
        day_features : pl.DataFrame
            DataFrame containing factor values for a single day.
        factor_weights : dict
            Dictionary of factor names and their configured weights.

        Returns
        -------
        pl.DataFrame
            DataFrame with missing values handled according to strategy.
        """
        if self._missing_factor_strategy == "exclude":
            # Only drop on columns that exist; missing ones are filtered later
            cols = [c for c in factor_weights if c in day_features.columns]
            return day_features.drop_nulls(cols) if cols else day_features

        elif self._missing_factor_strategy == "risk_penalty":
            # Risk penalty: don't fill — return as-is, penalty applied later
            return day_features

        elif self._missing_factor_strategy == "fill_median":
            for col in factor_weights:
                if col in day_features.columns:
                    median_val = day_features[col].median()
                    fill_val = median_val if median_val is not None else 0.0
                    day_features = day_features.with_columns(
                        pl.when(pl.col(col).is_null())
                        .then(fill_val)
                        .otherwise(pl.col(col))
                        .alias(col)
                    )
            return day_features

        else:  # fill_0
            if self._missing_factor_strategy != "fill_0":
                logger.warning("Unrecognized missing_factor_strategy=%r, defaulting to fill_0", self._missing_factor_strategy)
            for col in factor_weights:
                if col not in day_features.columns:
                    logger.warning("Factor %r not in features, filling with 0.0", col)
                    self._note_missing_factor(col)
                    day_features = day_features.with_columns(pl.lit(0.0).alias(col))
                else:
                    day_features = day_features.with_columns(
                        pl.when(pl.col(col).is_null())
                        .then(0.0)
                        .otherwise(pl.col(col))
                        .alias(col)
                    )
            return day_features

    # ------------------------------------------------------------------
    def _note_missing_factor(self, name: str) -> None:
        """Accumulate a unique missing-factor name for the current call."""
        if name not in self.missing_factors:
            self.missing_factors.append(name)

    # ------------------------------------------------------------------
    def generate_signals(self, ctx: StrategyContext) -> SignalFrame:
        # B2 lifecycle: reset per-call state FIRST (stays None/empty on failure
        # or early return — no cross-day residue), populate on success only.
        self.last_score_detail = None
        self.missing_factors = []

        empty = _empty_frame()

        if ctx.features is None or ctx.features.is_empty():
            return empty

        day_features = ctx.features.filter(pl.col("trade_date") == ctx.as_of_date)
        if day_features.is_empty():
            return empty

        # Handle missing factors (fill_0 may add missing columns)
        day_features = self._handle_missing_factors(day_features, self._factor_weights)

        # Keep only factors present in the features
        available = {k: w for k, w in self._factor_weights.items() if k in day_features.columns}
        for k in self._factor_weights:
            if k not in day_features.columns:
                self._note_missing_factor(k)
        if not available:
            return empty

        # Z-score each factor column and accumulate weighted scores
        score_exprs: list[pl.Expr] = []
        for col, weight in available.items():
            z = ((pl.col(col) - pl.col(col).mean()) / pl.col(col).std()).fill_nan(0.0)
            score_exprs.append((z * weight).alias(f"_w_{col}"))
        scored = day_features.with_columns(score_exprs)

        if scored.is_empty():
            return empty

        # Sum weighted z-scores into composite
        composite = pl.sum_horizontal([f"_w_{c}" for c in available]).alias("_composite")
        scored = scored.with_columns(composite)

        # Risk penalty: subtract penalty_per_missing for each missing factor
        if self._missing_factor_strategy == "risk_penalty":
            # Count nulls across all configured factor columns present in the data
            null_count_exprs = [
                pl.col(c).is_null().cast(pl.Int32) for c in available
            ]
            scored = scored.with_columns(
                pl.sum_horizontal(null_count_exprs).alias("_null_count")
            )
            scored = scored.with_columns(
                (pl.col("_composite") - pl.col("_null_count") * self._penalty_per_missing)
                .alias("_composite")
            ).drop("_null_count")

        scored = scored.sort("_composite", descending=True)

        # B2: expose per-call score detail (full ranked cross-section).
        # Columns: asset_id / score / rank, then `_w_{factor}` weighted
        # z-score parts (prefix kept as-is; consumers strip if needed).
        if not scored.is_empty():
            ranked = scored.with_row_index("_row")
            self.last_score_detail = ranked.select(
                [
                    pl.col("asset_id"),
                    pl.col("_composite").alias("score"),
                    (pl.col("_row") + 1).alias("rank"),
                ]
                + [pl.col(f"_w_{c}") for c in available]
            )

        scored = scored.head(self._top_n)

        if scored.is_empty():
            return empty

        return scored.select([
            pl.col("asset_id"),
            pl.lit(ctx.as_of_date).alias("signal_date"),
            pl.lit("long").alias("direction"),
            pl.col("_composite").alias("strength"),
            pl.lit(1.0).alias("confidence"),
        ])


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "asset_id": pl.Utf8,
            "signal_date": pl.Date,
            "direction": pl.Utf8,
            "strength": pl.Float64,
            "confidence": pl.Float64,
        }
    )
