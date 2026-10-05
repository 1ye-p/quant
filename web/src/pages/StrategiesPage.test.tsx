import { describe, it, expect } from 'vitest'
import { DEFAULT_CONFIG } from './StrategiesPage'
import { strategyDslSchema } from '@/lib/strategyDslSchema'

describe('StrategiesPage DEFAULT_CONFIG', () => {
  const config = JSON.parse(DEFAULT_CONFIG) as {
    strategy_type: string
    strategy_id: string
    dsl_spec: Record<string, unknown> & { universe: unknown }
  }

  it('wraps a DSL-shaped dsl_spec (strategy_type=DSL)', () => {
    expect(config.strategy_type).toBe('DSL')
    expect(config.strategy_id).toBe('my_strategy')
    expect(config.dsl_spec.name).toBe('my_strategy')
  })

  it('uses string universe "all" (DSL schema form, not legacy object)', () => {
    expect(typeof config.dsl_spec.universe).toBe('string')
    expect(config.dsl_spec.universe).toBe('all')
  })

  it('passes the frontend zod mirror of StrategyDSL with zero corrections', () => {
    // 零修正：默认值原样通过 zod 校验（与后端 StrategyDSL.from_dict 镜像）
    const parsed = strategyDslSchema.safeParse(config.dsl_spec)
    expect(parsed.success).toBe(true)
  })
})
