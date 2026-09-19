"""RegimeStateMachine — regime 三模式状态机（设计 §3.4.3，P3S-2）。

模式语义：
  - threshold（无状态）：按 rules 声明顺序取第一条 ``when`` 为真的规则，
    用其 position_scale；全不命中走 default 兜底规则。
  - switch（latch）：按 states 声明顺序（D6）检查各状态 ``enter_when``，
    首个命中者成为新状态；全不命中保持原状态（锁定）。
  - continuous：``scale_expr`` 求值并 clamp 到 [0, 1]。

数据缺失 / 求值失败语义：**保持上一状态 + warning，绝不静默切换**。
首次求值即失败时回退 ``(scale=1.0, state=initial 或 "default")`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from cquant.strategy_dsl.market_context import MarketSeriesContext
from cquant.strategy_dsl.schema import RegimeDef

logger = logging.getLogger(__name__)


@dataclass
class RegimeResult:
    """单日 regime 求值结果（引擎只消费 position_scale）。"""

    position_scale: float          # ∈ [0, 1]
    state: str | None              # threshold/continuous 为 None
    warnings: list[str] = field(default_factory=list)
    as_of_date: date | None = None


class RegimeStateMachine:
    """持有 RegimeDef + MarketSeriesContext，逐日求出 position_scale。"""

    def __init__(self, regime_def: RegimeDef, market_ctx: MarketSeriesContext) -> None:
        self._def = regime_def
        self._ctx = market_ctx
        # latch / hold-state 共用的上一结果；首日失败兜底为满仓
        self._last_result: RegimeResult | None = None
        if regime_def.mode == "switch":
            self._current_state: str | None = regime_def.initial
        else:
            self._current_state = None

    @property
    def current_state(self) -> str | None:
        return self._current_state

    # ── 求值入口 ─────────────────────────────────────────────────────────

    def evaluate(self, as_of_date: date) -> RegimeResult:
        try:
            if self._def.mode == "threshold":
                result = self._evaluate_threshold(as_of_date)
            elif self._def.mode == "switch":
                result = self._evaluate_switch(as_of_date)
            else:
                result = self._evaluate_continuous(as_of_date)
        except Exception as exc:  # noqa: BLE001 — 缺失/求值失败统一 hold-state
            result = self._hold_state(
                as_of_date, f"regime data missing/eval failed at {as_of_date}: {exc}"
            )
        self._last_result = result
        return result

    # ── 三模式 ───────────────────────────────────────────────────────────

    def _evaluate_threshold(self, as_of: date) -> RegimeResult:
        assert self._def.rules is not None
        for rule in self._def.rules:
            if rule.when is None:
                continue
            if self._truthy(rule.when, as_of):
                return RegimeResult(
                    position_scale=self._clamp(rule.position_scale),
                    state=None,
                    as_of_date=as_of,
                )
        # default 兜底（schema 已保证恰好一条）
        default = next(r for r in self._def.rules if r.when is None)
        return RegimeResult(
            position_scale=self._clamp(default.position_scale),
            state=None,
            as_of_date=as_of,
        )

    def _evaluate_switch(self, as_of: date) -> RegimeResult:
        assert self._def.states is not None
        warnings: list[str] = []
        for state in self._def.states:  # D6: 声明顺序优先
            try:
                hit = self._truthy(state.enter_when, as_of)
            except Exception as exc:  # noqa: BLE001
                # 单状态表达式失败 → 跳过该状态（不算作命中），记 warning
                warnings.append(
                    f"state '{state.name}' enter_when failed at {as_of}: {exc}"
                )
                continue
            if hit:
                if state.name != self._current_state:
                    logger.info(
                        "regime switch: %s -> %s at %s",
                        self._current_state, state.name, as_of,
                    )
                self._current_state = state.name
                return RegimeResult(
                    position_scale=self._clamp(state.position_scale),
                    state=state.name,
                    warnings=warnings,
                    as_of_date=as_of,
                )
        # 全不命中：latch 保持原状态（不是错误路径，无 warning）
        current = next(s for s in self._def.states if s.name == self._current_state)
        return RegimeResult(
            position_scale=self._clamp(current.position_scale),
            state=current.name,
            warnings=warnings,
            as_of_date=as_of,
        )

    def _evaluate_continuous(self, as_of: date) -> RegimeResult:
        assert self._def.scale_expr is not None
        raw = self._ctx.evaluate(self._def.scale_expr, as_of, self._def.indicators)
        return RegimeResult(
            position_scale=self._clamp(raw),
            state=None,
            as_of_date=as_of,
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _truthy(self, expr: str, as_of: date) -> bool:
        value = self._ctx.evaluate(expr, as_of, self._def.indicators)
        return value != 0.0

    def _hold_state(self, as_of: date, reason: str) -> RegimeResult:
        """数据缺失/求值失败：保持上一状态，仅记当日 warning。

        warnings 只含当日 reason（逐日前向累积会造成 O(N²) 日志膨胀）；
        继承的仅是 scale/state。
        """
        if self._last_result is not None:
            return RegimeResult(
                position_scale=self._last_result.position_scale,
                state=self._last_result.state,
                warnings=[reason],
                as_of_date=as_of,
            )
        # 首次求值即失败：switch 回 initial，其余满仓兜底
        scale = 1.0
        if self._def.mode == "switch":
            assert self._def.states is not None
            initial = next(
                (s for s in self._def.states if s.name == self._def.initial), None
            )
            if initial is not None:
                scale = self._clamp(initial.position_scale)
        return RegimeResult(
            position_scale=scale,
            state=self._current_state,
            warnings=[reason],
            as_of_date=as_of,
        )

    @staticmethod
    def _clamp(x: float) -> float:
        return max(0.0, min(1.0, float(x)))
