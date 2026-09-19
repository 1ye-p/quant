/**
 * Validation suite UI (Phase 4 T2):
 * - `useValidationSuite` — shared query (GET suite, 404 → null) + job polling
 * - `ValidationBadge` — tri-state suite badge (pass / warn / not run)
 * - `ValidationPanel` — tri-state checklist card with thresholds, cost
 *   assumptions block, and one-click run (POST + job polling → refetch)
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import i18next from 'i18next'
import { toast } from 'sonner'
import { backtestsApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import {
  suiteBadgeState,
  type ValidationSuite,
} from '@/lib/validationSummary'

// ── Shared suite query + job polling ─────────────────────────────────────────

export function useValidationSuite(runId: string | undefined) {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const suiteQuery = useQuery({
    queryKey: queryKeys.backtests.validationSuite(runId ?? ''),
    queryFn: () => backtestsApi.getValidationSuite(runId!).catch(() => null),
    enabled: !!runId,
    staleTime: 60_000,
    retry: false,
  })

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
    setJobId(null)
  }, [])

  useEffect(() => {
    if (!jobId) return
    timerRef.current = setInterval(async () => {
      try {
        const job = await backtestsApi.pollJob(jobId)
        if (job.status === 'completed') {
          stopPolling()
          qc.invalidateQueries({ queryKey: queryKeys.backtests.validationSuite(runId!) })
        } else if (job.status === 'failed') {
          stopPolling()
          toast.error(job.error ?? i18next.t('page.backtest.validation.checklist.run_failed'))
        }
      } catch {
        stopPolling()
        toast.error(i18next.t('page.backtest.validation.checklist.run_failed'))
      }
    }, 2000)
    return stopPolling
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, qc, runId])

  const runMutation = useMutation({
    mutationFn: () => backtestsApi.runValidationSuite(runId!),
    onSuccess: (res) => setJobId(res.job_id),
    onError: (e: Error) => toast.error(e.message),
  })

  return {
    suite: (suiteQuery.data ?? null) as ValidationSuite | null,
    isLoading: suiteQuery.isLoading,
    isRunning: jobId !== null || runMutation.isPending,
    run: () => runMutation.mutate(),
  }
}

// ── Badge ─────────────────────────────────────────────────────────────────────

const BADGE_STYLES: Record<string, string> = {
  pass: 'bg-green-50 text-green-700 border-green-200',
  warn: 'bg-amber-50 text-amber-700 border-amber-200',
  not_run: 'bg-gray-100 text-gray-500 border-gray-200',
}

const BADGE_ICONS: Record<string, string> = { pass: '✓', warn: '⚠', not_run: '○' }

export function ValidationBadge({ runId }: { runId: string | undefined }) {
  const { t } = useTranslation()
  const { suite, isLoading } = useValidationSuite(runId)
  const state = suiteBadgeState(suite)
  return (
    <span
      title={t(`page.backtest.validation.badge.tooltip.${state}`)}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs font-medium whitespace-nowrap ${BADGE_STYLES[state]}`}
    >
      <span>{isLoading ? '…' : BADGE_ICONS[state]}</span>
      {t(`page.backtest.validation.badge.${state}`)}
    </span>
  )
}

// ── Checklist card ────────────────────────────────────────────────────────────

type TriState = boolean | null

function ChecklistRow({ label, value, tooltip }: { label: string; value: TriState; tooltip: string }) {
  const { t } = useTranslation()
  return (
    <div className="flex items-center justify-between py-1.5" title={tooltip}>
      <span className="text-sm text-gray-700">{label}</span>
      <span className="text-sm font-medium">
        {value === true && <span className="text-green-600">✅ {t('page.backtest.validation.checklist.pass')}</span>}
        {value === false && <span className="text-red-600">⚠️ {t('page.backtest.validation.checklist.fail')}</span>}
        {value === null && (
          <span className="text-gray-400">⬜ {t('page.backtest.validation.checklist.unknown')}</span>
        )}
      </span>
    </div>
  )
}

export function ValidationPanel({ runId }: { runId: string | undefined }) {
  const { t } = useTranslation()
  const { suite, isRunning, run } = useValidationSuite(runId)
  const c = suite?.checklist

  if (!runId) return null

  if (!suite) {
    // Not run yet — one-click run
    return (
      <div className="card p-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-2">
          {t('page.backtest.validation.checklist.title')}
        </h3>
        <p className="text-xs text-gray-500 mb-3">{t('page.backtest.validation.checklist.empty_hint')}</p>
        <button onClick={run} disabled={isRunning} className="btn-primary text-xs">
          {isRunning ? t('page.backtest.validation.checklist.running') : t('page.backtest.validation.checklist.run_button')}
        </button>
      </div>
    )
  }

  const th = c?.thresholds
  const cost = c?.cost_assumptions

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">{t('page.backtest.validation.checklist.title')}</h3>
        <div className="flex items-center gap-2">
          <ValidationBadge runId={runId} />
          <button onClick={run} disabled={isRunning} className="btn-secondary text-xs">
            {isRunning ? t('page.backtest.validation.checklist.running') : t('page.backtest.validation.checklist.rerun_button')}
          </button>
        </div>
      </div>

      <div className="divide-y divide-gray-100">
        <ChecklistRow
          label={t('page.backtest.validation.checklist.psr_pass')}
          value={c?.psr_pass ?? null}
          tooltip={t('page.backtest.validation.checklist.th.psr', { threshold: th?.psr_pass ?? 0.95 })}
        />
        <ChecklistRow
          label={t('page.backtest.validation.checklist.fold_stable')}
          value={c?.fold_stable ?? null}
          tooltip={
            (c?.fold_source === 'returns_slice'
              ? t('page.backtest.validation.checklist.fold_source_slice') + ' — '
              : '') +
            t('page.backtest.validation.checklist.th.fold_cv', { threshold: th?.fold_stability_cv_max ?? 0.5 })
          }
        />
        <ChecklistRow
          label={t('page.backtest.validation.checklist.sensitivity_flat')}
          value={c?.sensitivity_flat ?? null}
          tooltip={t('page.backtest.validation.checklist.th.sens_cv', { threshold: th?.sensitivity_cv_max ?? 0.5 })}
        />
        <ChecklistRow
          label={t('page.backtest.validation.checklist.regime_cycles')}
          value={
            c?.regime_cycles_sufficient === undefined ? null : c.regime_cycles_sufficient
          }
          tooltip={t('page.backtest.validation.checklist.th.regime', { threshold: th?.min_regime_cycles ?? 6 })}
        />
        {c?.regime_cycles_sufficient == null && (
          <div className="py-1.5 text-xs text-gray-400">
            {t('page.backtest.validation.checklist.regime_na')}
          </div>
        )}
      </div>

      {/* Cost assumptions block */}
      {cost && (
        <div
          className={`rounded-lg border p-3 text-xs space-y-1 ${
            cost.defaults_used
              ? 'bg-amber-50 border-amber-200 text-amber-800'
              : 'bg-gray-50 border-gray-200 text-gray-600'
          }`}
        >
          <div className="font-medium">
            {t('page.backtest.validation.cost.title')}
            {cost.defaults_used && (
              <span className="ml-2 font-normal">⚠️ {t('page.backtest.validation.cost.defaults_warning')}</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
            <span>
              {t('page.backtest.validation.cost.commission')}: {cost.defaults.commission_rate}
            </span>
            <span>
              {t('page.backtest.validation.cost.stamp_duty')}: {cost.defaults.stamp_duty_rate} ({cost.defaults.stamp_duty_side})
            </span>
            <span>
              {t('page.backtest.validation.cost.slippage')}: {cost.defaults.slippage_rate}
            </span>
            <span>
              {t('page.backtest.validation.cost.impact')}: {cost.defaults.market_impact_rate}
            </span>
          </div>
          {cost.note && <div className="text-gray-500">{cost.note}</div>}
        </div>
      )}
    </div>
  )
}
