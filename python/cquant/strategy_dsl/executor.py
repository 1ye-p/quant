"""DSLStrategy — L2 声明式策略执行器。

score 段复用 CrossSectionScorer 的截面标准化（winsorize/fill_null/zscore）
与加权求和；`custom:` 前缀因子经 factor_registry（name → ExpressionFactor）
解析，实际取值来自已物化的 ctx.features 宽表列（Phase 2 落盘闭环）。
position/risk 段映射既有 sizer / RiskPolicy 实例，由引擎 / BacktestRunner
挂载（generate_signals 本身只负责 score → SignalFrame）。
regime 段不在本层执行：BacktestRunner 装配 RegimeStateMachine（B1 接线），
引擎在每个调仓日按 regime position_scale 缩放目标权重。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import polars as pl

from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.core.types import SignalFrame
from cquant.factorlab.cross_section_scorer import (
    CrossSectionScorer,
    FactorWeight,
    ScoringConfig,
)
from cquant.strategy_dsl.schema import StrategyDSL

logger = logging.getLogger(__name__)


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


class DSLStrategy(Strategy):
    """L2 DSL 策略：spec.score 加权打分 → TopN 信号（接口形态对齐 MultiFactorStrategy）。"""

    def __init__(
        self,
        spec: StrategyDSL,
        catalog: Any = None,
        factor_registry: Mapping[str, Any] | None = None,
        top_n: int = 10,
    ) -> None:
        self._spec = spec
        self._top_n = top_n
        self._factor_registry = dict(factor_registry) if factor_registry else {}
        # 复用 CrossSectionScorer 的纯截面数学（_normalize_cross_section /
        # _weighted_sum 不触库；catalog 仅用于其 DB 相关方法，此处不调用）
        self._scorer = CrossSectionScorer(catalog)
        self._config = ScoringConfig(
            name=f"dsl:{spec.name}",
            factors=[
                FactorWeight(factor_name=item.resolved_name, weight=item.weight)
                for item in spec.score
            ],
        )
        # 校验 custom: 因子在注册表中（构造期失败优于回测中途静默降级）
        for item in spec.score:
            if item.factor.startswith("custom:"):
                if self._factor_registry and item.resolved_name not in self._factor_registry:
                    raise ValueError(
                        f"自定义因子「{item.resolved_name}」不在 factor_registry 中 — "
                        "请先在自定义因子库创建并物化"
                    )

    @property
    def strategy_id(self) -> str:
        return f"dsl_{self._spec.name}"

    @property
    def spec(self) -> StrategyDSL:
        return self._spec

    # ── Strategy ABC ──────────────────────────────────────────────────────

    def generate_signals(self, ctx: StrategyContext) -> SignalFrame:
        empty = _empty_frame()
        if ctx.features is None or ctx.features.is_empty():
            return empty

        day_features = ctx.features.filter(pl.col("trade_date") == ctx.as_of_date)
        if day_features.is_empty():
            return empty

        available = [
            fw for fw in self._config.factors if fw.factor_name in day_features.columns
        ]
        missing = [
            fw.factor_name for fw in self._config.factors
            if fw.factor_name not in day_features.columns
        ]
        if missing:
            logger.warning(
                "DSLStrategy[%s]: 因子 %s 在 %s 的特征宽表中缺失，已跳过",
                self._spec.name, missing, ctx.as_of_date,
            )
        if not available:
            return empty

        day_config = ScoringConfig(name=self._config.name, factors=available)
        scored = self._scorer._normalize_cross_section(day_features, day_config)
        scored = self._scorer._weighted_sum(scored, day_config.factors)
        scored = scored.sort("score", descending=True).head(self._top_n)
        if scored.is_empty():
            return empty

        return scored.select([
            pl.col("asset_id"),
            pl.lit(ctx.as_of_date).alias("signal_date"),
            pl.lit("long").alias("direction"),
            pl.col("score").alias("strength"),
            pl.lit(1.0).alias("confidence"),
        ])

    # ── position / risk 映射（由 runner / 引擎挂载） ──────────────────────

    def build_sizer(self):
        """PositionDef.method → 既有 PositionSizer 实例（params 透传构造器）。"""
        from cquant.riskguard.sizers.base import PositionSizer
        from cquant.riskguard.sizers.black_litterman import BlackLittermanSizer
        from cquant.riskguard.sizers.equal_weight import EqualWeightSizer
        from cquant.riskguard.sizers.kelly import KellySizer
        from cquant.riskguard.sizers.mvo import MVOSizer
        from cquant.riskguard.sizers.target_vol import TargetVolSizer
        from cquant.riskguard.sizers.vol_parity import VolParitySizer

        table = {
            "equal_weight": EqualWeightSizer,
            "kelly": KellySizer,
            "mvo": MVOSizer,
            "target_vol": TargetVolSizer,
            "vol_parity": VolParitySizer,
            "black_litterman": BlackLittermanSizer,
        }
        pos = self._spec.position
        cls = table.get(pos.method)
        if cls is None:
            logger.warning(
                "DSLStrategy[%s]: 未知 sizer「%s」，回退 equal_weight",
                self._spec.name, pos.method,
            )
            cls = EqualWeightSizer
        sizer = cls(**pos.params)
        assert isinstance(sizer, PositionSizer)
        return sizer

    def build_risk_policies(self) -> list:
        """RiskItem.type → 既有 RiskPolicy 实例（params 透传构造器）。"""
        from cquant.riskguard.policies.atr_stop_loss import ATRStopLossPolicy
        from cquant.riskguard.policies.drawdown_breaker import DrawdownBreakerPolicy
        from cquant.riskguard.policies.factor_exposure_limit import FactorExposureLimitPolicy
        from cquant.riskguard.policies.leverage_limit import LeverageLimitPolicy
        from cquant.riskguard.policies.max_holding_days import MaxHoldingDaysPolicy
        from cquant.riskguard.policies.position_limits import PositionLimitPolicy
        from cquant.riskguard.policies.sector_limit import SectorLimitPolicy
        from cquant.riskguard.policies.stop_loss import (
            FixedStopLossPolicy,
            TrailingStopLossPolicy,
        )

        table = {
            "fixed_stop_loss": FixedStopLossPolicy,
            "trailing_stop_loss": TrailingStopLossPolicy,
            "atr_stop_loss": ATRStopLossPolicy,
            "drawdown_breaker": DrawdownBreakerPolicy,
            "position_limit": PositionLimitPolicy,
            "sector_limit": SectorLimitPolicy,
            "leverage_limit": LeverageLimitPolicy,
            "max_holding_days": MaxHoldingDaysPolicy,
            "factor_exposure_limit": FactorExposureLimitPolicy,
        }
        policies = []
        for item in self._spec.risk:
            policies.append(table[item.type](**item.params))
        return policies
