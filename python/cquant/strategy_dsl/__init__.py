"""cquant.strategy_dsl — L2 声明式策略 DSL（schema + 执行器）。

设计依据：docs/superpowers/specs/2026-09-16-research-loop-optimization-design.md §3.3 / §3.4.3。
"""

from cquant.strategy_dsl.schema import (
    RegimeDef,
    RiskItem,
    RuleDef,
    ScoreItem,
    StateDef,
    PositionDef,
    StrategyDSL,
)
from cquant.strategy_dsl.market_context import MarketSeriesContext
from cquant.strategy_dsl.regime import RegimeResult, RegimeStateMachine

__all__ = [
    "RegimeDef",
    "RiskItem",
    "RuleDef",
    "ScoreItem",
    "StateDef",
    "PositionDef",
    "StrategyDSL",
    "MarketSeriesContext",
    "RegimeResult",
    "RegimeStateMachine",
]
