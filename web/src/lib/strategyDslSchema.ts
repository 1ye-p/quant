/**
 * StrategyDSL zod schema — L2 声明式策略（pydantic schema 的前端镜像）。
 *
 * 错误信息与 python/cquant/strategy_dsl/schema.py 中文文案保持一致。
 * 设计依据：docs/superpowers/specs/2026-09-16-research-loop-optimization-design.md §3.3 / §3.4.3
 */
import { z } from "zod";

export const SIZER_NAMES = [
  "equal_weight",
  "kelly",
  "mvo",
  "target_vol",
  "vol_parity",
  "black_litterman",
] as const;

export const POLICY_NAMES = [
  "fixed_stop_loss",
  "trailing_stop_loss",
  "atr_stop_loss",
  "drawdown_breaker",
  "position_limit",
  "sector_limit",
  "leverage_limit",
  "max_holding_days",
  "factor_exposure_limit",
] as const;

export const CUSTOM_PREFIX = "custom:";

export const scoreItemSchema = z
  .object({
    factor: z.string().min(1, "score.factor: 因子名不能为空"),
    weight: z.number(),
  })
  .strict()
  .superRefine((v, ctx) => {
    if (v.weight === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["weight"],
        message: "score.weight: 权重不能为 0（权重为 0 的因子请直接删除）",
      });
    }
  });

export const positionDefSchema = z
  .object({
    method: z.enum(SIZER_NAMES, {
      errorMap: () => ({
        message: `position.method: 未知的仓位方法。可用方法：${SIZER_NAMES.join(" / ")}`,
      }),
    }),
    params: z.record(z.string(), z.any()).default({}),
    constraints: z.record(z.string(), z.any()).default({}),
  })
  .strict();

export const riskItemSchema = z
  .object({
    type: z.enum(POLICY_NAMES, {
      errorMap: () => ({
        message: `risk.type: 未知的风控类型。可用类型：${POLICY_NAMES.join(" / ")}`,
      }),
    }),
    params: z.record(z.string(), z.any()).default({}),
  })
  .strict();

export const stateDefSchema = z
  .object({
    name: z.string().min(1, "regime.states[].name: 状态名不能为空"),
    enter_when: z.string().min(1, "regime.states[].enter_when: 表达式不能为空"),
    position_scale: z
      .number()
      .min(0, "regime.states[].position_scale: 必须 ≥ 0")
      .max(1, "regime.states[].position_scale: 必须 ≤ 1"),
  })
  .strict();

export const ruleDefSchema = z
  .object({
    when: z.string().nullable().default(null),
    position_scale: z
      .number()
      .min(0, "regime.rules[].position_scale: 必须 ≥ 0")
      .max(1, "regime.rules[].position_scale: 必须 ≤ 1"),
  })
  .strict();

export const regimeDefSchema = z
  .object({
    mode: z.enum(["threshold", "switch", "continuous"], {
      errorMap: () => ({
        message: "regime.mode: 必须是 threshold / switch / continuous 之一",
      }),
    }),
    indicators: z.record(z.string(), z.string()).default({}),
    initial: z.string().nullable().default(null),
    reevaluate: z.enum(["daily", "rebalance"]).nullable().default(null),
    states: z.array(stateDefSchema).nullable().default(null),
    rules: z.array(ruleDefSchema).nullable().default(null),
    scale_expr: z.string().nullable().default(null),
  })
  .strict()
  .superRefine((v, ctx) => {
    const add = (path: (string | number)[], message: string) =>
      ctx.addIssue({ code: z.ZodIssueCode.custom, path, message });

    if (v.mode === "threshold") {
      if (!v.rules || v.rules.length === 0) {
        add(["rules"], "regime(mode=threshold): 必须提供 rules（至少一条 when 规则 + 恰好一条 default 兜底规则）");
      } else {
        const defaults = v.rules.filter((r) => r.when === null).length;
        if (defaults !== 1) {
          add(["rules"], `regime(mode=threshold).rules: 必须恰好包含一条 default 规则（when 省略），当前默认规则数 = ${defaults}`);
        }
      }
      if (v.states || v.scale_expr) {
        add(["states"], "regime(mode=threshold): 不允许配置 states/scale_expr（threshold 为无状态模式，仅用 rules）");
      }
    } else if (v.mode === "switch") {
      if (!v.states || v.states.length === 0) {
        add(["states"], "regime(mode=switch): 必须提供 states（至少一个状态，含 enter_when + position_scale）");
      } else {
        const names = v.states.map((s) => s.name);
        if (new Set(names).size !== names.length) {
          add(["states"], `regime(mode=switch).states: 状态名重复 — ${names.join(", ")}`);
        }
        if (!v.initial) {
          add(["initial"], "regime(mode=switch): 必须提供 initial（初始状态名）");
        } else if (!names.includes(v.initial)) {
          add(["initial"], `regime(mode=switch).initial: 初始状态「${v.initial}」不在 states 中（${names.join(", ")}）`);
        }
      }
      if (v.rules || v.scale_expr) {
        add(["rules"], "regime(mode=switch): 不允许配置 rules/scale_expr（switch 用 states 的 enter_when 表达式）");
      }
    } else {
      if (!v.scale_expr) {
        add(["scale_expr"], "regime(mode=continuous): 必须提供 scale_expr（返回 0-1 的连续缩放表达式）");
      }
      if (v.states || v.rules) {
        add(["states"], "regime(mode=continuous): 不允许配置 states/rules（continuous 仅用 scale_expr）");
      }
    }
  });

export const strategyDslSchema = z
  .object({
    name: z.string().min(1, "name: 策略名不能为空"),
    universe: z.string().default("all"),
    frequency: z.enum(["daily", "weekly", "monthly"], {
      errorMap: () => ({
        message: "frequency: 必须是 daily / weekly / monthly 之一",
      }),
    }),
    score: z
      .array(scoreItemSchema)
      .min(1, "score: 至少需要一个打分因子")
      .superRefine((items, ctx) => {
        if (items.every((i) => i.weight === 0)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "score: 所有因子权重均为 0 — 合成得分恒为 0，请配置非零权重",
          });
        }
      }),
    position: positionDefSchema.default({ method: "equal_weight", params: {}, constraints: {} }),
    risk: z.array(riskItemSchema).default([]),
    benchmark: z.string().default(""),
    regime: regimeDefSchema.nullable().default(null),
  })
  .strict();

export type ScoreItem = z.infer<typeof scoreItemSchema>;
export type PositionDef = z.infer<typeof positionDefSchema>;
export type RiskItem = z.infer<typeof riskItemSchema>;
export type StateDef = z.infer<typeof stateDefSchema>;
export type RuleDef = z.infer<typeof ruleDefSchema>;
export type RegimeDef = z.infer<typeof regimeDefSchema>;
export type StrategyDsl = z.infer<typeof strategyDslSchema>;

/**
 * 因子存在性检查（与 pydantic `_check_factor_exists` 一致）：
 * `custom:` 前缀查自定义因子集合，否则查内置因子集合。集合为空时跳过。
 */
export function validateFactorExists(
  factor: string,
  knownFactors: Set<string>,
  customFactors: Set<string>,
): string | null {
  if (factor.startsWith(CUSTOM_PREFIX)) {
    const name = factor.slice(CUSTOM_PREFIX.length);
    if (!name) return `score.factor: 自定义因子名为空 — 「${factor}」缺少 custom: 后的因子名`;
    if (customFactors.size > 0 && !customFactors.has(name)) {
      return `score.factor: 自定义因子「${name}」不存在（custom: 前缀需先在自定义因子库中创建并物化）`;
    }
    return null;
  }
  if (knownFactors.size > 0 && !knownFactors.has(factor)) {
    return `score.factor: 因子「${factor}」不存在。内置因子请使用物化后的因子名，自定义因子请加「${CUSTOM_PREFIX}」前缀`;
  }
  return null;
}
