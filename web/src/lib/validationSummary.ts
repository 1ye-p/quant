/**
 * Validation suite rule engine (pure functions).
 *
 * Maps a ValidationSuite result (checklist + steps) to:
 * - badge state (pass / warn / not_run)
 * - credibility points (3-5 lines, i18n keys)
 * - next-step recommendation (i18n keys)
 *
 * Pure — no React, no i18n dependency; components translate returned keys.
 */

// ── Types (mirrors GET /backtests/{run_id}/validation-suite) ──────────────────

export interface ValidationCostAssumptions {
  run_config: Record<string, unknown>
  defaults: {
    commission_rate: string
    min_commission: string
    stamp_duty_rate: string
    stamp_duty_side: string
    slippage_rate: string
    market_impact_rate: string
  }
  defaults_used: boolean
  note?: string
}

export interface ValidationChecklist {
  psr_pass: boolean | null
  fold_stable: boolean | null
  sensitivity_flat: boolean | null
  regime_cycles_sufficient: boolean | null
  fold_source: string | null
  cost_assumptions: ValidationCostAssumptions
  thresholds: {
    psr_pass: number
    fold_stability_cv_max: number
    sensitivity_cv_max: number
    min_regime_cycles: number
  }
}

/** Backend `steps` is a list of step records keyed by `step`. */
export interface ValidationStep {
  step: 'psr_dsr' | 'walk_forward' | 'sensitivity' | 'regime_cycles'
  status: 'completed' | 'failed' | 'skipped'
  error?: string
  weights_refit?: boolean
  weights_refit_note?: string
  fold_source?: string | null
  fold_stable_note?: string
  [key: string]: unknown
}

export interface ValidationSuite {
  suite_id: string
  run_id: string
  psr: number | null
  dsr: number | null
  checklist: ValidationChecklist
  steps: ValidationStep[]
  created_at: string
  status?: string
}

// ── Badge state ───────────────────────────────────────────────────────────────

export type SuiteBadgeState = 'pass' | 'warn' | 'not_run'

/**
 * Suite badge: pass (core items — PSR / fold / sensitivity — all checked true,
 * regime true when applicable) / warn (any false or undetermined core item) /
 * not_run (no result).
 */
export function suiteBadgeState(suite: ValidationSuite | null | undefined): SuiteBadgeState {
  const c = suite?.checklist
  if (!c) return 'not_run'
  const core = [c.psr_pass, c.fold_stable, c.sensitivity_flat]
  if (core.every((v) => v === null) && c.regime_cycles_sufficient === null) return 'not_run'
  const corePass = core.every((v) => v === true)
  const regimePass = c.regime_cycles_sufficient === null || c.regime_cycles_sufficient === true
  return corePass && regimePass ? 'pass' : 'warn'
}

// ── Credibility points ───────────────────────────────────────────────────────

export interface CredibilityPoint {
  key: string
  kind: 'good' | 'bad' | 'neutral'
}

function findStep(
  suite: ValidationSuite | null | undefined,
  name: ValidationStep['step'],
): ValidationStep | undefined {
  return suite?.steps?.find((s) => s.step === name)
}

/**
 * Rule-generated credibility points (3-5 lines). Keys are translated by the
 * component via `t(key)`.
 */
export function buildCredibilityPoints(
  suite: ValidationSuite | null | undefined,
): CredibilityPoint[] {
  const c = suite?.checklist
  if (!c) return []

  const points: CredibilityPoint[] = []

  // PSR
  if (c.psr_pass === true) points.push({ key: 'validation.points.psr_pass', kind: 'good' })
  else if (c.psr_pass === false) points.push({ key: 'validation.points.psr_fail', kind: 'bad' })
  else points.push({ key: 'validation.points.psr_unknown', kind: 'neutral' })

  // Fold stability (returns_slice → full-period slice estimate, not OOS folds)
  if (c.fold_stable === true) points.push({ key: 'validation.points.fold_stable', kind: 'good' })
  else if (c.fold_stable === false) points.push({ key: 'validation.points.fold_unstable', kind: 'bad' })
  else if (c.fold_source === 'returns_slice')
    points.push({ key: 'validation.points.fold_slice', kind: 'neutral' })
  else points.push({ key: 'validation.points.fold_unknown', kind: 'neutral' })

  // Sensitivity
  if (c.sensitivity_flat === true)
    points.push({ key: 'validation.points.sensitivity_flat', kind: 'good' })
  else if (c.sensitivity_flat === false)
    points.push({ key: 'validation.points.sensitivity_sensitive', kind: 'bad' })
  else points.push({ key: 'validation.points.sensitivity_unknown', kind: 'neutral' })

  // Regime cycles (null = non-regime strategy → not applicable, omit)
  if (c.regime_cycles_sufficient === true)
    points.push({ key: 'validation.points.regime_sufficient', kind: 'good' })
  else if (c.regime_cycles_sufficient === false)
    points.push({ key: 'validation.points.regime_insufficient', kind: 'bad' })

  // D7: walk-forward weights not refit per fold
  const wf = findStep(suite, 'walk_forward')
  if (wf && wf.weights_refit === false)
    points.push({ key: 'validation.points.weights_not_refit', kind: 'neutral' })

  return points
}

// ── Next-step recommendation ─────────────────────────────────────────────────

/**
 * Rule-generated next-step recommendation keys, most important first.
 */
export function buildNextSteps(suite: ValidationSuite | null | undefined): string[] {
  const c = suite?.checklist
  if (!c) return ['validation.next.not_run']

  if (suiteBadgeState(suite) === 'pass') return ['validation.next.all_pass']

  const next: string[] = []
  if (c.psr_pass === false) next.push('validation.next.psr_fail')
  if (c.fold_stable === false) next.push('validation.next.fold_unstable')
  if (c.sensitivity_flat === false) next.push('validation.next.sensitivity')
  if (c.regime_cycles_sufficient === false) next.push('validation.next.regime_cycles')
  if (next.length === 0) next.push('validation.next.default')
  return next
}
