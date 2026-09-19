/**
 * StrategyDSL YAML 序列化/解析 + 策略配置封装。
 *
 * - 表单态（宽松对象）⇄ YAML 文本 双向转换（js-yaml）
 * - dslToStrategyConfig：把 StrategyDSL 包装成标准策略 config_text
 *   （strategy_type="DSL" + dsl_spec），经 POST /strategies 保存，
 *   回测时由 BacktestRunSpec.dsl_spec 走标准回测。
 */
import yaml from 'js-yaml'
import { strategyDslSchema, validateFactorExists, type StrategyDsl } from './strategyDslSchema'

/** 宽松表单态：与 zod input 兼容（regime 可为 null）。 */
export type StrategyDslFormState = {
  name: string
  universe: string
  frequency: 'daily' | 'weekly' | 'monthly'
  benchmark: string
  score: Array<{ factor: string; weight: number }>
  position: { method: string; params: Record<string, unknown>; constraints: Record<string, unknown> }
  risk: Array<{ type: string; params: Record<string, unknown> }>
  regime: {
    mode: 'threshold' | 'switch' | 'continuous'
    indicators: Record<string, string>
    initial: string | null
    reevaluate: 'daily' | 'rebalance' | null
    states: Array<{ name: string; enter_when: string; position_scale: number }> | null
    rules: Array<{ when: string | null; position_scale: number }> | null
    scale_expr: string | null
  } | null
}

export const DEFAULT_DSL_FORM: StrategyDslFormState = {
  name: '',
  universe: 'all',
  frequency: 'daily',
  benchmark: '',
  score: [{ factor: '', weight: 1 }],
  position: { method: 'equal_weight', params: {}, constraints: {} },
  risk: [],
  regime: null,
}

/** 表单态 → YAML（剔除 regime null；保持字段顺序，与 pydantic to_yaml 对齐）。 */
export function formToYaml(form: StrategyDslFormState): string {
  const data: Record<string, unknown> = {
    name: form.name,
    universe: form.universe,
    frequency: form.frequency,
    score: form.score,
    position: form.position,
    risk: form.risk,
  }
  if (form.benchmark) data.benchmark = form.benchmark
  if (form.regime) data.regime = form.regime
  return yaml.dump(data, { noRefs: true, sortKeys: false, lineWidth: 100 })
}

export interface YamlParseResult {
  ok: boolean
  /** YAML 语法错误信息（中文），仅语法层失败时存在 */
  syntaxError?: string
  form?: StrategyDslFormState
}

/** YAML → 宽松表单态。仅做 YAML 语法解析 + 基本结构猜测，不做 zod 校验。 */
export function yamlToForm(text: string): YamlParseResult {
  let data: unknown
  try {
    data = yaml.load(text)
  } catch (e) {
    return { ok: false, syntaxError: `YAML 解析失败 — ${(e as Error).message}` }
  }
  if (data == null) return { ok: false, syntaxError: 'YAML 根节点为空，必须是映射（key: value）' }
  if (typeof data !== 'object' || Array.isArray(data)) {
    return { ok: false, syntaxError: 'YAML 根节点必须是映射（key: value）' }
  }
  const d = data as Record<string, unknown>
  const regime = d.regime as StrategyDslFormState['regime'] | undefined
  return {
    ok: true,
    form: {
      name: String(d.name ?? ''),
      universe: String(d.universe ?? 'all'),
      frequency: (['daily', 'weekly', 'monthly'].includes(String(d.frequency))
        ? d.frequency
        : 'daily') as StrategyDslFormState['frequency'],
      benchmark: String(d.benchmark ?? ''),
      score: Array.isArray(d.score)
        ? d.score.map((s) => ({
            factor: String((s as Record<string, unknown>).factor ?? ''),
            weight: Number((s as Record<string, unknown>).weight ?? 0),
          }))
        : [],
      position: {
        method: String((d.position as Record<string, unknown> | undefined)?.method ?? 'equal_weight'),
        params: ((d.position as Record<string, unknown>)?.params as Record<string, unknown>) ?? {},
        constraints: ((d.position as Record<string, unknown>)?.constraints as Record<string, unknown>) ?? {},
      },
      risk: Array.isArray(d.risk)
        ? d.risk.map((r) => ({
            type: String((r as Record<string, unknown>).type ?? ''),
            params: ((r as Record<string, unknown>).params as Record<string, unknown>) ?? {},
          }))
        : [],
      regime: regime && typeof regime === 'object'
        ? {
            mode: (['threshold', 'switch', 'continuous'].includes(String(regime.mode))
              ? regime.mode
              : 'threshold') as NonNullable<StrategyDslFormState['regime']>['mode'],
            indicators: (regime.indicators as Record<string, string>) ?? {},
            initial: regime.initial ?? null,
            reevaluate: regime.reevaluate ?? null,
            states: Array.isArray(regime.states) ? regime.states : null,
            rules: Array.isArray(regime.rules) ? regime.rules : null,
            scale_expr: regime.scale_expr ?? null,
          }
        : null,
    },
  }
}

export interface DslValidationResult {
  ok: boolean
  /** 路径（如 score.0.weight）→ 中文错误信息 */
  errors: Record<string, string>
  dsl?: StrategyDsl
}

/**
 * zod safeParse + 因子存在性检查 → 中文错误映射。
 * errors key 为字段路径，供表单红框定位；空路径 "" 表示对象级错误。
 */
export function validateDsl(
  form: StrategyDslFormState,
  knownFactors: Set<string> = new Set(),
  customFactors: Set<string> = new Set(),
): DslValidationResult {
  const parsed = strategyDslSchema.safeParse(form)
  const errors: Record<string, string> = {}
  if (!parsed.success) {
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.') || ''
      if (!errors[key]) errors[key] = issue.message
    }
    return { ok: false, errors }
  }
  // 结构校验通过后做因子存在性检查（注册表为空时 validateFactorExists 内部跳过）
  parsed.data.score.forEach((item, i) => {
    const err = validateFactorExists(item.factor, knownFactors, customFactors)
    if (err) errors[`score.${i}.factor`] = err
  })
  if (Object.keys(errors).length > 0) return { ok: false, errors, dsl: parsed.data }
  return { ok: true, errors, dsl: parsed.data }
}

/** 把校验通过的 StrategyDSL 包装成标准策略 config_text（strategy_type=DSL）。 */
export function dslToStrategyConfig(dsl: StrategyDsl): string {
  return JSON.stringify(
    {
      strategy_type: 'DSL',
      strategy_id: dsl.name,
      dsl_spec: dsl,
    },
    null,
    2,
  )
}
