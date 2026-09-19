import { describe, expect, it } from 'vitest'
import {
  buildCredibilityPoints,
  buildNextSteps,
  suiteBadgeState,
  type ValidationSuite,
} from './validationSummary'

function makeSuite(overrides: Partial<ValidationSuite['checklist']>, steps: ValidationSuite['steps'] = []): ValidationSuite {
  return {
    suite_id: 's1',
    run_id: 'r1',
    psr: 0.97,
    dsr: 0.9,
    created_at: '2026-09-06T00:00:00Z',
    steps,
    checklist: {
      psr_pass: null,
      fold_stable: null,
      sensitivity_flat: null,
      regime_cycles_sufficient: null,
      fold_source: null,
      cost_assumptions: {
        run_config: {},
        defaults: {
          commission_rate: '0.0003',
          min_commission: '5',
          stamp_duty_rate: '0.001',
          stamp_duty_side: 'sell',
          slippage_rate: '0.001',
          market_impact_rate: '0.001',
        },
        defaults_used: true,
      },
      thresholds: {
        psr_pass: 0.95,
        fold_stability_cv_max: 0.5,
        sensitivity_cv_max: 0.5,
        min_regime_cycles: 6,
      },
      ...overrides,
    },
  }
}

describe('suiteBadgeState', () => {
  it('returns not_run when no suite', () => {
    expect(suiteBadgeState(null)).toBe('not_run')
    expect(suiteBadgeState(undefined)).toBe('not_run')
  })

  it('returns not_run when all items null', () => {
    expect(suiteBadgeState(makeSuite({}))).toBe('not_run')
  })

  it('returns pass when all applicable items true (regime null ok)', () => {
    const s = makeSuite({ psr_pass: true, fold_stable: true, sensitivity_flat: true })
    expect(suiteBadgeState(s)).toBe('pass')
  })

  it('returns warn when any applicable item false', () => {
    const s = makeSuite({ psr_pass: true, fold_stable: false, sensitivity_flat: true })
    expect(suiteBadgeState(s)).toBe('warn')
  })
})

describe('buildCredibilityPoints', () => {
  it('maps each checklist tri-state to a point', () => {
    const s = makeSuite({ psr_pass: true, fold_stable: false, sensitivity_flat: null })
    const keys = buildCredibilityPoints(s).map((p) => p.key)
    expect(keys).toContain('validation.points.psr_pass')
    expect(keys).toContain('validation.points.fold_unstable')
    expect(keys).toContain('validation.points.sensitivity_unknown')
  })

  it('uses slice-estimate wording when fold_source=returns_slice', () => {
    const s = makeSuite({ fold_source: 'returns_slice' })
    const keys = buildCredibilityPoints(s).map((p) => p.key)
    expect(keys).toContain('validation.points.fold_slice')
    expect(keys).not.toContain('validation.points.fold_unknown')
  })

  it('omits regime point for non-regime strategies and adds D7 note', () => {
    const s = makeSuite(
      { regime_cycles_sufficient: null },
      [{ step: 'walk_forward', status: 'completed', weights_refit: false }],
    )
    const keys = buildCredibilityPoints(s).map((p) => p.key)
    expect(keys).not.toContain('validation.points.regime_sufficient')
    expect(keys).toContain('validation.points.weights_not_refit')
  })
})

describe('buildNextSteps', () => {
  it('recommends live validation when everything passes', () => {
    const s = makeSuite({ psr_pass: true, fold_stable: true, sensitivity_flat: true, regime_cycles_sufficient: true })
    expect(buildNextSteps(s)).toEqual(['validation.next.all_pass'])
  })

  it('prioritizes failing items', () => {
    const s = makeSuite({ psr_pass: false, sensitivity_flat: false })
    expect(buildNextSteps(s)).toEqual(['validation.next.psr_fail', 'validation.next.sensitivity'])
  })

  it('falls back to default when only undetermined items exist', () => {
    expect(buildNextSteps(makeSuite({ psr_pass: true }))).toEqual(['validation.next.default'])
    expect(buildNextSteps(null)).toEqual(['validation.next.not_run'])
  })
})
