import { describe, it, expect } from 'vitest'
import {
  formToYaml,
  yamlToForm,
  validateDsl,
  dslToStrategyConfig,
  DEFAULT_DSL_FORM,
} from './strategyDslYaml'

const validForm = {
  ...DEFAULT_DSL_FORM,
  name: 'my_dsl',
  score: [
    { factor: 'ret_20d', weight: 1 },
    { factor: 'custom:alpha_1', weight: -0.5 },
  ],
  position: { method: 'kelly', params: { window: 20 }, constraints: { max_weight: 0.1 } },
  risk: [{ type: 'fixed_stop_loss', params: { threshold: 0.08 } }],
  regime: {
    mode: 'threshold' as const,
    indicators: { dd: 'drawdown_20d' },
    initial: null,
    reevaluate: 'daily' as const,
    states: null,
    rules: [
      { when: 'dd > 0.08', position_scale: 0.4 },
      { when: null, position_scale: 1 },
    ],
    scale_expr: null,
  },
}

describe('strategyDslYaml', () => {
  it('form → YAML → form roundtrip preserves content', () => {
    const yamlText = formToYaml(validForm)
    expect(yamlText).toContain('name: my_dsl')
    expect(yamlText).toContain('method: kelly')
    const back = yamlToForm(yamlText)
    expect(back.ok).toBe(true)
    expect(back.form!.name).toBe('my_dsl')
    expect(back.form!.score).toHaveLength(2)
    expect(back.form!.score[1].factor).toBe('custom:alpha_1')
    expect(back.form!.position.method).toBe('kelly')
    expect(back.form!.position.params).toEqual({ window: 20 })
    expect(back.form!.risk[0].type).toBe('fixed_stop_loss')
    expect(back.form!.regime?.mode).toBe('threshold')
    expect(back.form!.regime?.rules).toHaveLength(2)
    expect(back.form!.regime?.rules![1].when).toBeNull()
  })

  it('omits benchmark/regime when empty', () => {
    const yamlText = formToYaml({ ...DEFAULT_DSL_FORM, score: [{ factor: 'ret_20d', weight: 1 }] })
    expect(yamlText).not.toContain('benchmark')
    expect(yamlText).not.toContain('regime')
  })

  it('reports yaml syntax error in Chinese', () => {
    const result = yamlToForm('name: [unclosed')
    expect(result.ok).toBe(false)
    expect(result.syntaxError).toContain('YAML 解析失败')
  })

  it('maps zod errors to field paths with Chinese messages', () => {
    const result = validateDsl({
      ...DEFAULT_DSL_FORM,
      name: '',
      score: [{ factor: 'ret_20d', weight: 0 }],
    })
    expect(result.ok).toBe(false)
    expect(result.errors['name']).toBe('name: 策略名不能为空')
    expect(result.errors['score.0.weight']).toBe('score.weight: 权重不能为 0（权重为 0 的因子请直接删除）')
  })

  it('flags unknown factor via catalog', () => {
    const result = validateDsl(
      { ...DEFAULT_DSL_FORM, name: 'x', score: [{ factor: 'nope', weight: 1 }] },
      new Set(['ret_20d']),
      new Set(),
    )
    expect(result.ok).toBe(false)
    expect(result.errors['score.0.factor']).toContain('nope')
  })

  it('validates regime threshold default-rule constraint', () => {
    const bad = {
      ...validForm,
      regime: {
        ...validForm.regime!,
        rules: [{ when: 'dd > 0.08', position_scale: 0.4 }],
      },
    }
    const result = validateDsl(bad)
    expect(result.ok).toBe(false)
    expect(result.errors['regime.rules']).toContain('恰好包含一条 default 规则')
  })

  it('wraps valid dsl into strategy config (strategy_type=DSL)', () => {
    const result = validateDsl(validForm)
    expect(result.ok).toBe(true)
    const config = JSON.parse(dslToStrategyConfig(result.dsl!))
    expect(config.strategy_type).toBe('DSL')
    expect(config.dsl_spec.name).toBe('my_dsl')
    expect(config.dsl_spec.regime.mode).toBe('threshold')
  })
})
