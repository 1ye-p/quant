/**
 * Decision summary card (Phase 4 T3).
 *
 * Top-of-page card: core metrics row (from existing run metrics, not
 * recomputed) + validation badge + rule-generated credibility points and
 * next-step recommendation + reproducibility anchors.
 */

import { useTranslation } from 'react-i18next'
import type { Backtest } from '@/lib/types'
import {
  buildCredibilityPoints,
  buildNextSteps,
  suiteBadgeState,
} from '@/lib/validationSummary'
import { ValidationBadge, useValidationSuite } from './ValidationPanel'

const KIND_STYLES: Record<string, string> = {
  good: 'text-green-700',
  bad: 'text-red-700',
  neutral: 'text-gray-600',
}

const KIND_ICONS: Record<string, string> = { good: '✓', bad: '✗', neutral: '·' }

export function DecisionSummaryCard({ detail }: { detail: Backtest | undefined }) {
  const { t } = useTranslation()
  const runId = detail?.run_id
  const { suite } = useValidationSuite(runId)
  const badge = suiteBadgeState(suite)
  const points = buildCredibilityPoints(suite)
  const nextSteps = buildNextSteps(suite)
  const m = detail?.metrics

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">
          {t('page.backtest.validation.decision.title')}
        </h3>
        <ValidationBadge runId={runId} />
      </div>

      {/* Core metrics row */}
      {m && (
        <div className="grid grid-cols-3 gap-4">
          <div>
            <div className="text-xs text-gray-500">{t('common.metric.total_return')}</div>
            <div className={`text-lg font-semibold ${m.total_return < 0 ? 'text-red-600' : ''}`}>
              {(m.total_return * 100).toFixed(1)}%
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">{t('common.metric.sharpe_ratio')}</div>
            <div className={`text-lg font-semibold ${m.sharpe_ratio < 0 ? 'text-red-600' : ''}`}>
              {Number(m.sharpe_ratio ?? 0).toFixed(3)}
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">{t('common.metric.max_drawdown')}</div>
            <div className="text-lg font-semibold text-red-600">
              {(m.max_drawdown * 100).toFixed(1)}%
            </div>
          </div>
        </div>
      )}

      {/* Credibility points + next step */}
      {badge !== 'not_run' && points.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1">
            <div className="text-xs font-medium text-gray-500">
              {t('page.backtest.validation.decision.points_title')}
            </div>
            {points.map((p) => (
              <div key={p.key} className={`text-sm ${KIND_STYLES[p.kind]}`}>
                {KIND_ICONS[p.kind]} {t('page.backtest.' + p.key)}
              </div>
            ))}
          </div>
          <div className="space-y-1">
            <div className="text-xs font-medium text-gray-500">
              {t('page.backtest.validation.decision.next_title')}
            </div>
            {nextSteps.map((key) => (
              <div key={key} className="text-sm text-gray-700">
                → {t('page.backtest.' + key)}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Reproducibility anchors */}
      <div className="text-xs text-gray-400 flex flex-wrap gap-x-4 gap-y-1 border-t border-gray-100 pt-2">
        <span>
          {t('page.backtest.validation.decision.anchors.dataset')}:{' '}
          <span className="font-mono">{detail?.dataset_version ?? '—'}</span>
        </span>
        <span>
          {t('page.backtest.validation.decision.anchors.strategy')}:{' '}
          <span className="font-mono">{detail?.strategy_id ?? '—'}</span>
        </span>
        <span>
          {t('page.backtest.validation.decision.anchors.run')}:{' '}
          <span className="font-mono">{runId ?? '—'}</span>
        </span>
        <span>
          {t('page.backtest.validation.decision.anchors.created')}:{' '}
          <span className="font-mono">
            {detail?.started_at ? String(detail.started_at).slice(0, 19).replace('T', ' ') : '—'}
          </span>
        </span>
        <span>
          {t('page.backtest.validation.decision.anchors.external_source')}:{' '}
          {t('page.backtest.validation.decision.anchors.external_na')}
        </span>
      </div>
    </div>
  )
}
