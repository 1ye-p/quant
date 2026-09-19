"""StrategyDSL schema — L2 声明式策略配置（pydantic v2）。

设计依据：docs/superpowers/specs/2026-09-16-research-loop-optimization-design.md
  - §3.3   L2 DSL 草案（score / position / risk / benchmark / regime）
  - §3.4.3 regime 三模式（threshold / switch / continuous）

校验原则：
  - 纯内存校验：因子/自定义因子/sizer/policy 注册表快照由调用方通过
    ``model_validate(..., context=ValidationContext(...))`` 注入；未注入时
    跳过存在性检查（synthetic/预览场景），但结构性校验（权重非零、模式
    互斥字段、表达式语法）始终执行。
  - 错误信息中文 + 字段路径（与 web/src/lib/strategyDslSchema.ts zod 镜像一致）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

logger = logging.getLogger(__name__)

# ── 注册表快照（与 api_server/routes/risk.py 实名一致） ─────────────────────

DEFAULT_SIZER_NAMES: frozenset[str] = frozenset({
    "equal_weight", "kelly", "mvo", "target_vol", "vol_parity", "black_litterman",
})

DEFAULT_POLICY_NAMES: frozenset[str] = frozenset({
    "fixed_stop_loss", "trailing_stop_loss", "atr_stop_loss", "drawdown_breaker",
    "position_limit", "sector_limit", "leverage_limit", "max_holding_days",
    "factor_exposure_limit",
})

CUSTOM_PREFIX = "custom:"


@dataclass
class ValidationContext:
    """注册表快照（schema 层不查库，由调用方注入）。"""

    known_factors: set[str] = field(default_factory=set)
    custom_factors: set[str] = field(default_factory=set)
    sizer_names: frozenset[str] = DEFAULT_SIZER_NAMES
    policy_names: frozenset[str] = DEFAULT_POLICY_NAMES


def _ctx(info: ValidationInfo) -> ValidationContext | None:
    ctx = info.context
    if isinstance(ctx, ValidationContext):
        return ctx
    if isinstance(ctx, dict):
        return ValidationContext(
            known_factors=set(ctx.get("known_factors", set())),
            custom_factors=set(ctx.get("custom_factors", set())),
            sizer_names=frozenset(ctx.get("sizer_names", DEFAULT_SIZER_NAMES)),
            policy_names=frozenset(ctx.get("policy_names", DEFAULT_POLICY_NAMES)),
        )
    return None


def _check_expression(expr: str, path: str) -> None:
    """表达式试编译（compile_expression），失败转中文提示。

    schema 层不依赖 DB：语法错误直接抛出；未知列名/函数在编译期无法
    判定（Polars 惰性表达式），留待求值上下文检查。
    """
    from cquant.factorlab.dsl_evaluator import compile_expression

    try:
        compile_expression(expr)
    except Exception as exc:  # noqa: BLE001 — DSL 语法/函数错误统一转中文
        msg = str(exc)
        if "Unknown column" in msg or "Unknown function" in msg:
            # 纯语法校验通过：regime 表达式的标识符是指标键（breadth/
            # active_cap/ext.*），函数集在 regime 求值上下文（P3S-2）扩展
            # （pct_change/zscore 等）— 存在性留待求值期检查
            return
        raise ValueError(
            f"{path}: 表达式语法错误「{expr}」— {exc}。"
            "请检查表达式语法（括号配对、比较运算符、函数参数个数）"
        ) from exc


def _check_factor_exists(factor: str, path: str, info: ValidationInfo) -> None:
    """因子存在性：`custom:` 前缀查自定义集合，否则查内置集合。

    未注入注册表快照（无 context）时跳过——纯 schema 结构校验场景。
    """
    vctx = _ctx(info)
    if vctx is None:
        return
    if factor.startswith(CUSTOM_PREFIX):
        name = factor[len(CUSTOM_PREFIX):]
        if not name:
            raise ValueError(f"{path}: 自定义因子名为空 — 「{factor}」缺少 custom: 后的因子名")
        if vctx.custom_factors and name not in vctx.custom_factors:
            raise ValueError(
                f"{path}: 自定义因子「{name}」不存在（custom: 前缀需先在"
                "自定义因子库中创建并物化）"
            )
    else:
        if vctx.known_factors and factor not in vctx.known_factors:
            raise ValueError(
                f"{path}: 因子「{factor}」不存在。内置因子请使用物化后的因子名，"
                f"自定义因子请加「{CUSTOM_PREFIX}」前缀"
            )


# ── 模型 ─────────────────────────────────────────────────────────────────────


class ScoreItem(BaseModel):
    """打分因子项（设计 §3.3 score 段）。"""

    model_config = ConfigDict(extra="forbid")

    factor: str
    weight: float

    @field_validator("factor")
    @classmethod
    def _factor_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("score.factor: 因子名不能为空")
        return v

    @field_validator("weight")
    @classmethod
    def _weight_nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("score.weight: 权重不能为 0（权重为 0 的因子请直接删除）")
        return v

    @field_validator("factor")
    @classmethod
    def _factor_exists(cls, v: str, info: ValidationInfo) -> str:
        _check_factor_exists(v, "score.factor", info)
        return v

    @property
    def resolved_name(self) -> str:
        """去掉 `custom:` 前缀后的物化因子名（gold_factor_values 中的列名）。"""
        return self.factor[len(CUSTOM_PREFIX):] if self.factor.startswith(CUSTOM_PREFIX) else self.factor


class PositionDef(BaseModel):
    """仓位段（设计 §3.3：method 引用 sizer + params）。

    YAML 草案中的 `constraints`（max_weight/sector_limit 等）保留为透传
    字典，由 sizer/optimizer 侧解释。
    """

    model_config = ConfigDict(extra="forbid")

    method: str = "equal_weight"
    params: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def _method_in_registry(cls, v: str, info: ValidationInfo) -> str:
        vctx = _ctx(info)
        names = vctx.sizer_names if vctx else DEFAULT_SIZER_NAMES
        if v not in names:
            raise ValueError(
                f"position.method: 未知的仓位方法「{v}」。可用方法：{sorted(names)}"
            )
        return v


class RiskItem(BaseModel):
    """风控段（type 与 _POLICY_REGISTRY 实名一一对应）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "fixed_stop_loss", "trailing_stop_loss", "atr_stop_loss", "drawdown_breaker",
        "position_limit", "sector_limit", "leverage_limit", "max_holding_days",
        "factor_exposure_limit",
    ]
    params: dict[str, Any] = Field(default_factory=dict)


class StateDef(BaseModel):
    """regime switch 模式的状态定义（设计 §3.4.3 模式二）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    enter_when: str
    position_scale: float = Field(ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("regime.states[].name: 状态名不能为空")
        return v

    @field_validator("enter_when")
    @classmethod
    def _enter_when_compiles(cls, v: str) -> str:
        _check_expression(v, "regime.states[].enter_when")
        return v


class RuleDef(BaseModel):
    """regime threshold 模式的规则（设计 §3.4.3 模式一）。

    `when=None` 表示 default 规则（`default: {position_scale: 1.0}`），
    threshold 模式要求恰好一条 default 规则兜底。
    """

    model_config = ConfigDict(extra="forbid")

    when: str | None = None
    position_scale: float = Field(ge=0.0, le=1.0)

    @field_validator("when")
    @classmethod
    def _when_compiles(cls, v: str | None) -> str | None:
        if v is not None:
            _check_expression(v, "regime.rules[].when")
        return v


class RegimeDef(BaseModel):
    """regime 段（设计 §3.4.3，三模式互斥字段）。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["threshold", "switch", "continuous"]
    indicators: dict[str, str] = Field(default_factory=dict)
    # switch 模式
    initial: str | None = None
    reevaluate: Literal["daily", "rebalance"] | None = None
    states: list[StateDef] | None = None
    # threshold 模式
    rules: list[RuleDef] | None = None
    # continuous 模式
    scale_expr: str | None = None

    @field_validator("scale_expr")
    @classmethod
    def _scale_expr_compiles(cls, v: str | None) -> str | None:
        if v is not None:
            _check_expression(v, "regime.scale_expr")
        return v

    @model_validator(mode="after")
    def _mode_mutex(self) -> "RegimeDef":
        if self.mode == "threshold":
            if not self.rules:
                raise ValueError(
                    "regime(mode=threshold): 必须提供 rules（至少一条 when 规则 + "
                    "恰好一条 default 兜底规则）"
                )
            defaults = [r for r in self.rules if r.when is None]
            if len(defaults) != 1:
                raise ValueError(
                    "regime(mode=threshold).rules: 必须恰好包含一条 default 规则"
                    "（when 省略），当前默认规则数 = " + str(len(defaults))
                )
            if self.states or self.scale_expr:
                raise ValueError(
                    "regime(mode=threshold): 不允许配置 states/scale_expr"
                    "（threshold 为无状态模式，仅用 rules）"
                )
        elif self.mode == "switch":
            if not self.states:
                raise ValueError(
                    "regime(mode=switch): 必须提供 states（至少一个状态，含 enter_when + position_scale）"
                )
            names = [s.name for s in self.states]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"regime(mode=switch).states: 状态名重复 — {names}"
                )
            if not self.initial:
                raise ValueError(
                    "regime(mode=switch): 必须提供 initial（初始状态名）"
                )
            if self.initial not in names:
                raise ValueError(
                    f"regime(mode=switch).initial: 初始状态「{self.initial}」不在 states 中（{names}）"
                )
            if self.rules or self.scale_expr:
                raise ValueError(
                    "regime(mode=switch): 不允许配置 rules/scale_expr"
                    "（switch 用 states 的 enter_when 表达式）"
                )
        else:  # continuous
            if not self.scale_expr:
                raise ValueError(
                    "regime(mode=continuous): 必须提供 scale_expr（返回 0-1 的连续缩放表达式）"
                )
            if self.states or self.rules:
                raise ValueError(
                    "regime(mode=continuous): 不允许配置 states/rules（continuous 仅用 scale_expr）"
                )
        return self


class StrategyDSL(BaseModel):
    """L2 声明式策略（设计 §3.3 草案的完整形态）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    universe: str = "all"
    frequency: Literal["daily", "weekly", "monthly"] = "daily"
    score: list[ScoreItem] = Field(min_length=1)
    position: PositionDef = Field(default_factory=PositionDef)
    risk: list[RiskItem] = Field(default_factory=list)
    benchmark: str = ""
    regime: RegimeDef | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name: 策略名不能为空")
        return v

    @model_validator(mode="after")
    def _at_least_one_effective(self) -> "StrategyDSL":
        if all(item.weight == 0 for item in self.score):
            raise ValueError("score: 所有因子权重均为 0 — 合成得分恒为 0，请配置非零权重")
        return self

    # ── YAML / dict 序列化 ────────────────────────────────────────────────

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], context: ValidationContext | dict | None = None
    ) -> "StrategyDSL":
        return cls.model_validate(data, context=context)

    @classmethod
    def from_yaml(
        cls, text: str, context: ValidationContext | dict | None = None
    ) -> "StrategyDSL":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"name: YAML 解析失败 — {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("StrategyDSL: YAML 根节点必须是映射（key: value）")
        return cls.model_validate(data, context=context)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
