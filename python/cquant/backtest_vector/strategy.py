"""cquant.backtest_vector.strategy — Strategy ABC and context."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import polars as pl

from cquant.core.types import SignalFrame

logger = logging.getLogger(__name__)


@dataclass
class StrategyContext:
    """Runtime context provided to a strategy during signal generation."""

    as_of_date: date
    universe_id: str
    feature_set_version: str = ""
    features: pl.DataFrame | None = None    # Gold factor values for the universe
    prices: pl.DataFrame | None = None      # Silver OHLCV
    tradability: pl.DataFrame | None = None  # [trade_date, asset_id, is_suspended, is_limit_up, is_limit_down]
    extra: dict = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all cQuant strategies.

    A strategy is a pure function: given context, return signals.
    Strategies do NOT manage positions, risk, or execution.

    Subclasses may override ``fit()`` to support walk-forward re-fitting.
    The default implementation is a no-op, so strategies that do not require
    training (e.g. StaticTopN, MultiFactor with fixed weights) work without
    modification.

    Class/instance attribute ``required_history_days`` (trading days, default
    0) declares how much pre-window history the strategy needs to see before
    it can signal. Walk-forward fold backtests use it to prepend warmup data
    to the fold window (the fold's ~50-day window alone can never satisfy a
    180-day lookback).
    """

    required_history_days: int = 0

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Unique stable identifier for this strategy."""

    @abstractmethod
    def generate_signals(self, ctx: StrategyContext) -> SignalFrame:
        """Generate trading signals for the given context.

        Returns a Polars DataFrame with columns:
            [asset_id (str), signal_date (date), direction (str),
             strength (f64), confidence (f64)]
        """

    def fit(self, train_data: dict[str, Any]) -> None:
        """Re-fit the strategy on training data.

        Called by WalkForwardRefit before each out-of-sample evaluation
        fold.  The default implementation is a no-op.

        Parameters
        ----------
        train_data:
            Dictionary with training-period data.  Common keys:

            - ``"prices"`` (pl.DataFrame): OHLCV for the training window.
            - ``"features"`` (pl.DataFrame): Factor values for the window.
            - ``"train_start"`` / ``"train_end"`` (date): Window boundaries.

            Strategy-specific keys may also be present (e.g. ``"model_id"``
            for MLModelStrategy).
        """
        logger.debug(
            "Strategy.fit() called on '%s' — no-op (override to support re-fit)",
            self.strategy_id,
        )
