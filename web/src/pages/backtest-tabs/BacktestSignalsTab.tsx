/**
 * B2 — Signal details tab: rebalance-date picker + per-date detail table
 * (rank / asset / total score / dynamic factor-score columns / action badge /
 * old-new weights), plus the missing-factors warning banner (run tag
 * `signals_missing_factors`) and the persistence-failure banner
 * (`signals_persisted=false`, same pattern as fills).
 *
 * Tri-state 404 copy mirrors the attribution tab: `no_signal_details` /
 * `unsupported_strategy_type` / `run_not_found`.
 */

import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { backtestsApi, type BacktestSignalItem } from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { queryKeys } from '@/lib/queryKeys'
import { DataTable } from '@/components/ui/DataTable'
import { DataState } from '@/components/ui/DataState'

type SignalsReason = 'run_not_found' | 'no_signal_details' | 'unsupported_strategy_type'

const REASONS: readonly SignalsReason[] = [
  'run_not_found',
  'no_signal_details',
  'unsupported_strategy_type',
]

/** Extract the tri-state 404 reason from an ApiError (null for other errors). */
function extractReason(error: unknown): SignalsReason | null {
  if (!(error instanceof ApiError) || error.status !== 404) return null
  const reason = (error.details as { reason?: unknown } | undefined)?.reason
  return REASONS.includes(reason as SignalsReason) ? (reason as SignalsReason) : null
}

/** Parse run tags (JSON string or already-parsed object) into a record. */
function parseRunTags(raw: unknown): Record<string, unknown> {
  if (!raw) return {}
  if (typeof raw === 'object') return raw as Record<string, unknown>
  if (typeof raw !== 'string') return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

/** signals_missing_factors tag → sorted factor list (tolerant of raw list or JSON string). */
function parseMissingFactors(raw: unknown): string[] {
  let list = raw
  if (typeof raw === 'string') {
    try {
      list = JSON.parse(raw)
    } catch {
      return []
    }
  }
  return Array.isArray(list) ? list.map(String).sort() : []
}

const ACTION_BADGE: Record<string, string> = {
  enter: 'bg-green-100 text-green-800',
  hold: 'bg-gray-100 text-gray-700',
  exit: 'bg-red-100 text-red-800',
  candidate: 'bg-blue-100 text-blue-800',
}

function fmtWeight(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

function fmtScore(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return Number(value).toFixed(4)
}

export function BacktestSignalsTab() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const [selectedDate, setSelectedDate] = useState('')
  const [page, setPage] = useState(0)
  const pageSize = 50

  // Dates with persisted rows — drives the rebalance-date dropdown.
  const datesQuery = useQuery({
    queryKey: queryKeys.backtests.signalDates(id!),
    queryFn: () => backtestsApi.getSignalDates(id!),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  })

  // Default to the first (earliest) rebalance date once dates load.
  const dates = datesQuery.data?.dates ?? []
  useEffect(() => {
    if (dates.length > 0 && !dates.includes(selectedDate)) {
      setSelectedDate(dates[0])
      setPage(0)
    }
  }, [dates, selectedDate])

  const detailsQuery = useQuery({
    queryKey: queryKeys.backtests.signalDetails(id!, selectedDate, page, pageSize),
    queryFn: () => backtestsApi.getSignalDetails(id!, selectedDate, page, pageSize),
    enabled: !!id && !!selectedDate,
    staleTime: 60_000,
    retry: false,
    placeholderData: keepPreviousData,
  })

  // Run detail (tags) — missing-factor warning + persistence-failure banner.
  const { data: runDetail } = useQuery({
    queryKey: queryKeys.backtests.detail(id!),
    queryFn: () => backtestsApi.get(id!),
    enabled: !!id,
    staleTime: 60_000,
  })
  const runTags = useMemo(() => parseRunTags(runDetail?.tags), [runDetail?.tags])
  const missingFactors = useMemo(
    () => parseMissingFactors(runTags.signals_missing_factors),
    [runTags.signals_missing_factors],
  )
  const signalsPersistFailed = runTags.signals_persisted === false
  const signalsError = typeof runTags.signals_error === 'string' ? runTags.signals_error : ''

  // Dynamic factor-score columns: union of factor keys across the page's items
  // (factor set can vary by strategy), flattened as `fs__{factor}` columns.
  const items = detailsQuery.data?.items ?? []
  const factorKeys = useMemo(() => {
    const keys = new Set<string>()
    for (const it of items) {
      for (const k of Object.keys(it.factor_scores ?? {})) keys.add(k)
    }
    return [...keys].sort()
  }, [items])
  const rows = useMemo(
    () => items.map((it: BacktestSignalItem) => ({
      ...it,
      ...Object.fromEntries(
        factorKeys.map(k => [`fs__${k}`, it.factor_scores?.[k] ?? null]),
      ),
    })),
    [items, factorKeys],
  )

  const reason = extractReason(datesQuery.error) ?? extractReason(detailsQuery.error)

  if (!id) return null

  // Tri-state 404 copy
  if (reason) {
    return (
      <div className="card p-8 text-center text-gray-400" data-testid={`signals-${reason}`}>
        {t(`page.backtest.signals.reason.${reason}`)}
        <div className="mt-2 text-sm">
          {t(`page.backtest.signals.reason.${reason}_hint`)}
        </div>
      </div>
    )
  }

  return (
    <DataState isLoading={datesQuery.isLoading} error={datesQuery.error}>
      <div className="space-y-3">
          {/* Missing-factor warning banner (run tag signals_missing_factors) */}
          {missingFactors.length > 0 && (
            <div className="card p-4 border border-amber-200 bg-amber-50" data-testid="signals-missing-factors">
              <span className="text-amber-700 font-semibold text-sm">
                {t('page.backtest.signals.missing_factors_title')}
              </span>
              <p className="text-xs text-amber-700 mt-1">
                {t('page.backtest.signals.missing_factors_hint')}
              </p>
              <p className="text-xs text-amber-800 mt-1 font-mono" data-testid="signals-missing-factors-list">
                {missingFactors.join(', ')}
              </p>
            </div>
          )}

          {/* Persistence failure banner (run tag signals_persisted=false, fills precedent) */}
          {signalsPersistFailed && (
            <div className="card p-4 border border-red-200 bg-red-50" data-testid="signals-persist-failure">
              <span className="text-red-600 font-semibold text-sm">
                {t('page.backtest.signals.persist_failed_title')}
              </span>
              <p className="text-xs text-red-700 mt-1">
                {t('page.backtest.signals.persist_failed_hint')}
              </p>
              {signalsError && (
                <p className="text-xs text-red-600 mt-2 font-mono break-all" data-testid="signals-persist-error">
                  {signalsError}
                </p>
              )}
            </div>
          )}

          {/* Rebalance-date picker + detail table */}
          <div className="flex items-center gap-3">
            <label htmlFor="signals-date" className="text-sm text-gray-500">
              {t('page.backtest.signals.date_label')}
            </label>
            <select
              id="signals-date"
              className="input text-sm w-44"
              value={selectedDate}
              onChange={e => { setSelectedDate(e.target.value); setPage(0) }}
              data-testid="signals-date-select"
            >
              {dates.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
            {datesQuery.data && (
              <span className="text-xs text-gray-400">
                {t('page.backtest.signals.dates_count', { total: datesQuery.data.total })}
              </span>
            )}
          </div>

          {selectedDate && (
            <DataTable
              data={rows}
              rowKey="asset_id"
              pageSize={pageSize}
              enableExport
              exportFilename={`signals_${id.slice(0, 8)}_${selectedDate}`}
              emptyText={t('page.backtest.signals.empty')}
              backendPagination={detailsQuery.data ? {
                total: detailsQuery.data.total,
                page,
                onPageChange: setPage,
              } : undefined}
              columns={[
                { key: 'rank', label: t('page.backtest.signals.col_rank'), sortable: true, width: '70px',
                  render: v => v == null ? <span className="text-gray-400">—</span> : <span className="font-mono">{String(v)}</span> },
                { key: 'asset_id', label: t('page.backtest.signals.col_asset'), sortable: true, searchable: true,
                  render: v => <span className="font-mono text-xs">{String(v)}</span> },
                { key: 'score', label: t('page.backtest.signals.col_score'), sortable: true,
                  render: v => <span className="font-mono">{fmtScore(v as number | null)}</span> },
                ...factorKeys.map(k => ({
                  key: `fs__${k}`,
                  label: k,
                  sortable: true,
                  render: (v: unknown) => <span className="font-mono">{fmtScore(v as number | null)}</span>,
                })),
                { key: 'action', label: t('page.backtest.signals.col_action'), sortable: true, filterable: true,
                  filters: ['enter', 'hold', 'exit', 'candidate'],
                  render: v => (
                    <span className={`badge ${ACTION_BADGE[String(v)] ?? 'bg-gray-100 text-gray-700'}`}>
                      {t(`page.backtest.signals.action.${String(v)}`)}
                    </span>
                  ) },
                { key: 'prev_weight', label: t('page.backtest.signals.col_prev_weight'), sortable: true,
                  render: v => <span className="font-mono">{fmtWeight(v as number | null)}</span> },
                { key: 'new_weight', label: t('page.backtest.signals.col_new_weight'), sortable: true,
                  render: v => <span className="font-mono">{fmtWeight(v as number | null)}</span> },
              ]}
            />
          )}
        </div>
    </DataState>
  )
}
