"""cquant.execution.strategy_loader — Load strategy instances from backtest config.

Queries gold_backtest_runs for strategy_id and config_json, then instantiates
the corresponding Strategy class from the registry or built-in map.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from cquant.backtest_vector.strategy import Strategy
from cquant.datahub.catalog import Catalog

if TYPE_CHECKING:
    from cquant.registry.registry import Registry

logger = logging.getLogger(__name__)

# Built-in strategy class map (avoids full registry discovery for common cases)
_BUILTIN_STRATEGIES: dict[str, type[Strategy]] = {}


def _get_builtin_strategies() -> dict[str, type[Strategy]]:
    """Lazy-load built-in strategy classes."""
    if not _BUILTIN_STRATEGIES:
        from cquant.backtest_vector.run import StaticTopNStrategy
        from cquant.backtest_vector.strategies.ml_strategy import MLModelStrategy
        from cquant.backtest_vector.strategies.combo import CompositeStrategy
        from cquant.backtest_vector.strategies.market_neutral import MarketNeutralStrategy
        from cquant.backtest_vector.strategies.sector_rotation import SectorRotationStrategy
        from cquant.backtest_vector.strategies.custom_weight_strategy import CustomWeightStrategy
        from cquant.backtest_vector.strategies.multi_factor import MultiFactorStrategy

        _BUILTIN_STRATEGIES.update({
            "StaticTopN": StaticTopNStrategy,
            "MLModel": MLModelStrategy,
            "Composite": CompositeStrategy,
            "MarketNeutral": MarketNeutralStrategy,
            "SectorRotation": SectorRotationStrategy,
            "CustomWeight": CustomWeightStrategy,
            "MultiFactor": MultiFactorStrategy,
        })
    return _BUILTIN_STRATEGIES


def get_strategy_class(
    strategy_type: str,
    registry: "Registry | None" = None,
) -> type[Strategy]:
    """Resolve strategy class (or plugin factory) by type name.

    Checks built-in map first, then falls back to the plugin registry
    (capability ``"strategy"`` — the plugin's entrypoint may be a Strategy
    subclass *or* a factory callable returning one; both are invoked the
    same way by :meth:`StrategyLoader._build_strategy`).

    Parameters
    ----------
    strategy_type:
        Strategy type name (e.g. "StaticTopN", "MLModel", or a registered
        plugin name declaring the ``strategy`` capability).
    registry:
        Optional pre-populated :class:`~cquant.registry.registry.Registry`.
        When omitted, a fresh Registry is created and plugin paths from the
        ``CQUANT_PLUGIN_PATHS`` env var (colon/comma-separated) are
        discovered, if set.

    Returns
    -------
    Strategy class or plugin factory callable.

    Raises
    ------
    ValueError
        If strategy_type is not recognized.
    """
    builtin = _get_builtin_strategies()
    if strategy_type in builtin:
        return builtin[strategy_type]

    # Fallback: try registry (built-in capability since P3S-2 / L3)
    try:
        if registry is None:
            registry = _default_registry()
        factory = registry.resolve("strategy", strategy_type)
        return factory
    except Exception as exc:
        raise ValueError(
            f"Unknown strategy type: '{strategy_type}'. "
            f"Available built-in: {sorted(builtin.keys())}"
        ) from exc


def _default_registry() -> "Registry":
    """Build a Registry from ``CQUANT_PLUGIN_PATHS`` (if configured)."""
    import os

    from cquant.registry.registry import Registry

    reg = Registry()
    paths = os.environ.get("CQUANT_PLUGIN_PATHS", "")
    if paths:
        discovered = reg.discover([
            p for sep in (":", ",") for p in paths.replace(sep, "\x00").split("\x00")
            if p
        ])
        if not discovered:
            logger.debug("CQUANT_PLUGIN_PATHS set but no plugins discovered")
    return reg


class StrategyLoader:
    """Loads strategy instances from backtest configuration.

    Queries ``gold_backtest_runs`` for the strategy's ``config_json``,
    then uses ``get_strategy_class`` to instantiate.

    Usage::

        loader = StrategyLoader(catalog)
        strategy = loader.load("my_strategy_id")
    """

    def __init__(self, catalog: Catalog, registry: "Registry | None" = None) -> None:
        self._catalog = catalog
        # Optional plugin registry for the "strategy" capability (L3) —
        # injected by callers that manage plugin discovery; when None,
        # get_strategy_class falls back to a CQUANT_PLUGIN_PATHS-based one.
        self._registry = registry

    def load(self, strategy_id: str) -> Strategy:
        """Load and instantiate a strategy by its ID.

        Parameters
        ----------
        strategy_id:
            The strategy identifier stored in ``gold_backtest_runs``.

        Returns
        -------
        Instantiated Strategy object.

        Raises
        ------
        ValueError
            If the strategy is not found or cannot be instantiated.
        """
        df = self._catalog.query(
            "SELECT strategy_id, config_json FROM gold_backtest_runs "
            "WHERE strategy_id = ? AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1",
            [strategy_id],
        )
        if df.is_empty():
            raise ValueError(f"No completed backtest run found for strategy '{strategy_id}'")

        row = df.to_dicts()[0]
        config_json = row.get("config_json")

        if config_json:
            config = json.loads(config_json) if isinstance(config_json, str) else config_json
        else:
            config = {}

        strategy_type = config.get("strategy_type", "StaticTopN")
        strategy_cls = get_strategy_class(strategy_type, registry=self._registry)

        try:
            strategy = self._build_strategy(strategy_cls, strategy_id, config)
            logger.info("Loaded strategy '%s' (type=%s)", strategy_id, strategy_type)
            return strategy
        except Exception as exc:
            raise ValueError(
                f"Failed to instantiate strategy '{strategy_id}' (type={strategy_type}): {exc}"
            ) from exc

    def load_active_strategies(self) -> list[dict[str, Any]]:
        """Load all active deployed strategies from meta_live_strategies.

        Returns
        -------
        List of dicts with keys: live_id, strategy_id, backtest_run_id,
        initial_cash, risk_mode, config.
        """
        df = self._catalog.query(
            "SELECT live_id, strategy_id, backtest_run_id, initial_cash, risk_mode "
            "FROM meta_live_strategies WHERE status = 'active'"
        )
        if df.is_empty():
            return []

        results = []
        for row in df.to_dicts():
            # Fetch config from backtest run
            run_df = self._catalog.query(
                "SELECT config_json FROM gold_backtest_runs WHERE run_id = ?",
                [row["backtest_run_id"]],
            )
            config = {}
            if not run_df.is_empty() and run_df["config_json"][0]:
                raw = run_df["config_json"][0]
                config = json.loads(raw) if isinstance(raw, str) else raw

            results.append({**row, "config": config})

        return results

    @staticmethod
    def _build_strategy(
        strategy_cls: type[Strategy],
        strategy_id: str,
        config: dict[str, Any],
    ) -> Strategy:
        """Build a strategy instance from config dict.

        Tries common constructor patterns:
        1. strategy_cls(strategy_id=strategy_id, **config)
        2. strategy_cls(**config)  (if strategy_id is in config)
        """
        # Remove non-constructor keys
        build_config = {k: v for k, v in config.items() if k != "strategy_type"}

        try:
            return strategy_cls(strategy_id=strategy_id, **build_config)
        except TypeError:
            # Maybe strategy_id is already in config
            if "strategy_id" not in build_config:
                build_config["strategy_id"] = strategy_id
            return strategy_cls(**build_config)
