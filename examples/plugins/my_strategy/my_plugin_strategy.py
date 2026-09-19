"""Minimal L3 strategy plugin (example).

The manifest's ``strategy`` entrypoint points at ``create_strategy`` — a
factory returning a :class:`~cquant.backtest_vector.strategy.Strategy`
subclass. Both a class and a factory work with
``execution.strategy_loader.StrategyLoader``.

To keep the entrypoint module importable, this directory doubles as a
``PYTHONPATH`` entry (see README.md); the module is therefore named
``my_plugin_strategy`` rather than colliding with any stdlib/cquant name.
"""

from __future__ import annotations

import polars as pl

from cquant.backtest_vector.strategy import Strategy, StrategyContext


class MyStrategy(Strategy):
    """Long the top-N assets by signal strength, with a score floor."""

    def __init__(self, strategy_id: str = "my_strategy", top_n: int = 5, min_strength: float = 0.0) -> None:
        self._id = strategy_id
        self._top_n = top_n
        self._min_strength = min_strength

    @property
    def strategy_id(self) -> str:
        return self._id

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        if ctx.features is None or ctx.features.is_empty():
            return pl.DataFrame(schema={
                "asset_id": pl.Utf8, "signal_date": pl.Date,
                "direction": pl.Utf8, "strength": pl.Float64,
                "confidence": pl.Float64,
            })
        day = ctx.features.filter(pl.col("trade_date") == ctx.as_of_date)
        if "score" in day.columns:
            day = day.filter(pl.col("score") >= self._min_strength).sort(
                "score", descending=True
            ).head(self._top_n)
            strength = pl.col("score")
        else:
            strength = pl.lit(1.0)
        return day.select([
            "asset_id",
            pl.lit(ctx.as_of_date).alias("signal_date"),
            pl.lit("long").alias("direction"),
            strength.alias("strength"),
            pl.lit(1.0).alias("confidence"),
        ])


def create_strategy(**kwargs) -> MyStrategy:
    """Entrypoint referenced by plugin.json (``strategy`` capability)."""
    return MyStrategy(**kwargs)
