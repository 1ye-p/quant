import { useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { backtestsApi, jobsApi } from '@/lib/api'
import type { BacktestListParams } from '@/lib/api/backtests'
import { queryKeys } from '@/lib/queryKeys'
import { formatApiDateTime } from '@/lib/utils'
import { toast } from 'sonner'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { useBacktestCompareStore } from '@/stores/backtestCompareStore'
import {
  LineChart, Line, Legend,
  XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts'

const SORT_COLUMNS = ['started_at', 'strategy_id', 'status', 'engine', 'sharpe_ratio', 'total_return', 'max_drawdown'] as const
type SortCol = (typeof SORT_COLUMNS)[number]

function isSortCol(v: string | null): v is SortCol {
  return SORT_COLUMNS.includes(v as SortCol)
}

export function BacktestsListPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { selectedIds, toggleSelection, clearSelection } = useBacktestCompareStore()
  const [searchParams, setSearchParams] = useSearchParams()

  const [confirmAction, setConfirmAction] = useState<{ type: 'cancel' | 'delete'; runId: string } | null>(null)
  const [page, setPage] = useState(0)
  const pageSize = 20
  const [btSearch, setBtSearch] = useState('')
  const [showCompare, setShowCompare] = useState(false)

  // ── Filter / sort state from URL ──────────────────────────────────────
  const status = searchParams.get('status') ?? ''
  const engine = searchParams.get('engine') ?? ''
  const strategyId = searchParams.get('strategy_id') ?? ''
  const startDate = searchParams.get('start_date') ?? ''
  const endDate = searchParams.get('end_date') ?? ''
  const sortBy = searchParams.get('sort_by') ?? 'started_at'
  const sortOrder = (searchParams.get('sort_order') ?? 'desc') as 'asc' | 'desc'

  function updateParam(key: string, value: string) {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (!value) next.delete(key)
      else next.set(key, value)
      return next
    }, { replace: true })
    setPage(0)
  }

  // Build API filter params
  const filters: BacktestListParams = useMemo(() => ({
    offset: page * pageSize,
    limit: pageSize,
    status: status || undefined,
    engine: engine || undefined,
    strategy_id: strategyId || undefined,
    start_date: startDate || undefined,
    end_date: endDate || undefined,
    sort_by: isSortCol(sortBy) ? sortBy : undefined,
    sort_order: sortOrder,
  }), [page, pageSize, status, engine, strategyId, startDate, endDate, sortBy, sortOrder])

  const cancelMutation = useMutation({
    mutationFn: (runId: string) => jobsApi.cancel(runId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['backtests'] }),
    onError: (e: Error) => toast.error(`Cancel failed: ${e.message}`),
  })

  const deleteMutation = useMutation({
    mutationFn: (runId: string) => jobsApi.delete(runId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['backtests'] }),
    onError: (e: Error) => toast.error(`Delete failed: ${e.message}`),
  })

  const { data, isLoading, isFetching } = useQuery({
    queryKey: queryKeys.backtests.list(filters as Record<string, unknown>),
    queryFn: () => backtestsApi.list(filters),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => {
      const d = query.state.data
      return d && !d.items.some(r => r.status === 'running' || r.status === 'pending') ? false : 5000
    },
  })

  const { data: compareData, isLoading: compareLoading } = useQuery({
    queryKey: ['backtests', 'compare', selectedIds.join(',')],
    queryFn: () => backtestsApi.compare(selectedIds.join(',')),
    enabled: showCompare && selectedIds.length >= 2,
    staleTime: 60_000,
  })

  // Client-side text search fallback (still useful for quick fuzzy search)
  const filteredBacktests = useMemo(() => {
    if (!data?.items) return []
    if (!btSearch.trim()) return data.items
    const q = btSearch.toLowerCase()
    return data.items.filter(
      r => r.strategy_id.toLowerCase().includes(q)
        || r.run_id.toLowerCase().includes(q)
        || (r.engine ?? '').toLowerCase().includes(q)
    )
  }, [data, btSearch])

  // Derive unique engine / strategy values from current data for dropdown options
  const engineOptions = useMemo(() => {
    if (!data?.items) return []
    return [...new Set(data.items.map(r => r.engine).filter(Boolean))].sort()
  }, [data])

  const strategyOptions = useMemo(() => {
    if (!data?.items) return []
    return [...new Set(data.items.map(r => r.strategy_id))].sort()
  }, [data])

  // ── Sort toggle handler ───────────────────────────────────────────────
  function handleSort(col: SortCol) {
    if (sortBy === col) {
      updateParam('sort_order', sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSearchParams(prev => {
        const next = new URLSearchParams(prev)
        next.set('sort_by', col)
        next.set('sort_order', 'asc')
        return next
      }, { replace: true })
      setPage(0)
    }
  }

  function sortIndicator(col: SortCol) {
    if (sortBy !== col) return <span className="ml-1 text-gray-300">▲</span>
    return sortOrder === 'asc'
      ? <span className="ml-1 text-brand-600">▲</span>
      : <span className="ml-1 text-brand-600">▼</span>
  }

  // Sortable column header helper
  function SortableTh({ col, label, className = '' }: { col: SortCol; label: string; className?: string }) {
    return (
      <th
        className={`table-th cursor-pointer select-none hover:text-gray-700 ${className}`}
        onClick={() => handleSort(col)}
      >
        {label}
        {sortIndicator(col)}
      </th>
    )
  }

  // NAV series merge for compare chart
  function mergeNavSeries(
    runs: Array<{ run_id: string; nav_series: { date: string; nav: number }[] }>
  ): Array<Record<string, unknown>> {
    const dateMap = new Map<string, Record<string, unknown>>()
    runs.forEach(r => {
      r.nav_series.forEach(({ date, nav }) => {
        if (!dateMap.has(date)) dateMap.set(date, { date })
        dateMap.get(date)![r.run_id] = nav
      })
    })
    return Array.from(dateMap.values()).sort((a, b) =>
      String(a['date']).localeCompare(String(b['date']))
    )
  }

  const hasActiveFilters = status || engine || strategyId || startDate || endDate

  return (
    <div>
      <h1 className="page-title">{t('page.backtests_list.title')}</h1>

      {/* Filter bar */}
      <div className="card p-3 mb-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-400 uppercase tracking-wide">{t('page.backtests_list.filter.status')}</label>
            <select
              value={status}
              onChange={e => updateParam('status', e.target.value)}
              className="input py-1 text-xs w-32"
            >
              <option value="">{t('page.backtests_list.filter.all')}</option>
              <option value="running">{t('page.backtests_list.status.running')}</option>
              <option value="completed">{t('page.backtests_list.status.completed')}</option>
              <option value="failed">{t('page.backtests_list.status.failed')}</option>
              <option value="cancelled">{t('page.backtests_list.status.cancelled')}</option>
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-400 uppercase tracking-wide">{t('page.backtests_list.filter.engine')}</label>
            <select
              value={engine}
              onChange={e => updateParam('engine', e.target.value)}
              className="input py-1 text-xs w-36"
            >
              <option value="">{t('page.backtests_list.filter.all')}</option>
              {engineOptions.map(e => (
                <option key={e} value={e}>{e}</option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-400 uppercase tracking-wide">{t('page.backtests_list.filter.strategy')}</label>
            <select
              value={strategyId}
              onChange={e => updateParam('strategy_id', e.target.value)}
              className="input py-1 text-xs w-40"
            >
              <option value="">{t('page.backtests_list.filter.all')}</option>
              {strategyOptions.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-400 uppercase tracking-wide">{t('page.backtests_list.filter.start_from')}</label>
            <input
              type="date"
              value={startDate}
              onChange={e => updateParam('start_date', e.target.value)}
              className="input py-1 text-xs w-36"
            />
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-400 uppercase tracking-wide">{t('page.backtests_list.filter.end_before')}</label>
            <input
              type="date"
              value={endDate}
              onChange={e => updateParam('end_date', e.target.value)}
              className="input py-1 text-xs w-36"
            />
          </div>

          {hasActiveFilters && (
            <button
              onClick={() => setSearchParams({}, { replace: true })}
              className="text-xs text-gray-400 hover:text-gray-600 ml-1 pb-1"
            >
              {t('page.backtests_list.btn.clear_filters')}
            </button>
          )}
        </div>
      </div>

      {/* Search + subtitle row */}
      <div className="flex items-center justify-between mb-1">
        <p className="page-subtitle">
          {btSearch
            ? t('page.backtests_list.subtitle.found', { found: filteredBacktests.length, pageTotal: data?.items?.length ?? 0, total: data?.total ?? 0 })
            : t('page.backtests_list.subtitle.total', { count: data?.total ?? 0 })}
        </p>
        <div className="relative">
          <input
            type="text"
            placeholder={t('page.backtests_list.search.placeholder')}
            value={btSearch}
            onChange={e => setBtSearch(e.target.value)}
            className="pl-6 pr-6 py-1 text-xs border rounded focus:outline-none focus:ring-1 focus:ring-brand-500 w-44"
          />
          <span className="absolute left-1.5 top-1/2 -translate-y-1/2 text-gray-400 text-xs">{t('common.search')}</span>
          {btSearch && (
            <button onClick={() => setBtSearch('')}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-xs">X</button>
          )}
        </div>
      </div>

      {/* Run list */}
      <div className="card p-0 overflow-hidden mb-6">
        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-brand-600" />
            <span className="ml-2 text-sm text-gray-500">{t('common.loading')}</span>
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="table-th w-10"><span className="sr-only">{t('page.backtests_list.column.select')}</span></th>
                <th className="table-th">{t('page.backtests_list.column.run_id')}</th>
                <SortableTh col="strategy_id" label={t('page.backtests_list.column.strategy')} />
                <SortableTh col="engine" label={t('page.backtests_list.column.engine')} />
                <SortableTh col="status" label={t('page.backtests_list.column.status')} />
                <SortableTh col="started_at" label={t('page.backtests_list.column.started')} />
                <th className="table-th">{t('page.backtests_list.column.ended')}</th>
                <SortableTh col="sharpe_ratio" label={t('page.backtests_list.column.sharpe')} />
                <SortableTh col="total_return" label={t('page.backtests_list.column.return')} />
                <SortableTh col="max_drawdown" label={t('page.backtests_list.column.maxdd')} />
                <th className="table-th">{t('page.backtests_list.column.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {!filteredBacktests.length && (
                <tr><td colSpan={11} className="table-td text-center text-gray-400 py-8">
                  {isFetching ? t('common.loading') : btSearch ? t('page.backtests_list.empty.no_match') : t('page.backtests_list.empty.no_records')}
                </td></tr>
              )}
              {filteredBacktests.map(r => (
                <tr
                  key={r.run_id}
                  className={`table-row ${r.is_running_job ? 'opacity-60' : 'cursor-pointer'}`}
                  onClick={r.is_running_job ? undefined : () => navigate(`/backtests/${r.run_id}`)}
                >
                  <td className="table-td" onClick={e => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      disabled={!!r.is_running_job}
                      checked={selectedIds.includes(r.run_id)}
                      onChange={() => toggleSelection(r.run_id)}
                      className="w-4 h-4 rounded border-gray-300 accent-brand-600 disabled:opacity-40"
                    />
                  </td>
                  <td className="table-td font-mono text-xs">{r.is_running_job ? '-' : `${r.run_id.slice(0, 8)}...`}</td>
                  <td className="table-td font-medium">{r.is_running_job ? <span className="text-blue-600">{t('page.backtests_list.row.submitting')}</span> : r.strategy_id}</td>
                  <td className="table-td text-gray-500">
                    {r.is_running_job
                      ? '-'
                      : r.engine === 'walk_forward'
                        ? <span className="px-1.5 py-0.5 text-xs rounded bg-purple-100 text-purple-700 font-medium">{t('page.backtests_list.badge.wf_summary')}</span>
                        : <span className="text-gray-500">{r.engine}</span>}
                  </td>
                  <td className="table-td"><StatusBadge status={r.status} /></td>
                  <td className="table-td text-gray-400">{formatApiDateTime(r.started_at)}</td>
                  <td className="table-td text-gray-400">{formatApiDateTime(r.completed_at)}</td>
                  <td className="table-td font-mono text-xs">
                    {r.metrics?.sharpe_ratio != null ? r.metrics.sharpe_ratio.toFixed(2) : '-'}
                  </td>
                  <td className="table-td font-mono text-xs">
                    {r.metrics?.total_return != null
                      ? <span className={r.metrics.total_return >= 0 ? 'text-green-600' : 'text-red-600'}>
                          {(r.metrics.total_return * 100).toFixed(2)}%
                        </span>
                      : '-'}
                  </td>
                  <td className="table-td font-mono text-xs">
                    {r.metrics?.max_drawdown != null
                      ? <span className="text-red-600">
                          {(r.metrics.max_drawdown * 100).toFixed(2)}%
                        </span>
                      : '-'}
                  </td>
                  <td className="table-td" onClick={e => e.stopPropagation()}>
                    {(r.status === 'running' || r.status === 'pending') && (
                      <button
                        onClick={() => setConfirmAction({ type: 'cancel', runId: r.run_id })}
                        className="text-red-500 hover:text-red-700 mr-2 text-xs"
                        title={t('page.backtests_list.btn.stop')}
                      >{t('page.backtests_list.btn.stop')}</button>
                    )}
                    <button
                      onClick={() => setConfirmAction({ type: 'delete', runId: r.run_id })}
                      className="text-gray-400 hover:text-gray-600 text-xs"
                      title={t('common.delete')}
                    >{t('common.delete')}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {/* Pagination */}
        {data && data.total > pageSize && (
          <div className="flex items-center justify-between px-4 py-3 border-t bg-gray-50 text-sm">
            <span className="text-gray-500">{t('page.backtests_list.pagination.total', { count: data.total })}</span>
            <div className="flex gap-2">
              <button
                className="px-3 py-1 rounded border bg-white hover:bg-gray-100 disabled:opacity-40"
                disabled={page === 0}
                onClick={() => setPage(p => p - 1)}
              >{t('page.backtests_list.btn.previous')}</button>
              <span className="px-3 py-1 text-gray-600">
                {t('page.backtests_list.pagination.page', { current: page + 1, total: Math.ceil(data.total / pageSize) })}
              </span>
              <button
                className="px-3 py-1 rounded border bg-white hover:bg-gray-100 disabled:opacity-40"
                disabled={(page + 1) * pageSize >= data.total}
                onClick={() => setPage(p => p + 1)}
              >{t('page.backtests_list.btn.next')}</button>
            </div>
          </div>
        )}
      </div>

      {/* Floating compare bar */}
      {selectedIds.length >= 2 && !showCompare && (
        <div className="fixed bottom-6 right-6 z-20 flex items-center gap-2">
          <button
            className="btn-primary shadow-lg flex items-center gap-2 px-5 py-2.5"
            onClick={() => setShowCompare(true)}
          >
            {t('page.backtests_list.btn.compare_count', { count: selectedIds.length })}
          </button>
          <button className="btn-secondary text-xs" onClick={clearSelection}>
            {t('page.backtests_list.btn.clear')}
          </button>
        </div>
      )}

      {/* Compare modal */}
      {showCompare && (
        <div className="fixed inset-0 bg-black/40 z-30 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-5xl max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between p-5 border-b flex-shrink-0">
              <h2 className="text-lg font-semibold text-gray-800">
                {t('page.backtests_list.compare.modal_title', { count: selectedIds.length })}
              </h2>
              <button onClick={() => setShowCompare(false)} className="text-gray-400 hover:text-gray-700 text-2xl leading-none">X</button>
            </div>

            <div className="overflow-y-auto flex-1 p-5 space-y-6">
              {compareLoading ? (
                <div className="py-12 text-center text-gray-400">{t('common.loading')}</div>
              ) : compareData ? (
                <>
                  {/* Metrics comparison table */}
                  <div>
                    <h3 className="font-semibold text-gray-700 mb-3">{t('page.backtests_list.compare.key_metrics_title')}</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-left text-gray-500 border-b bg-gray-50">
                            <th className="py-2 px-3">{t('page.backtests_list.compare.metric_header')}</th>
                            {compareData.runs.map(r => (
                              <th key={r.run_id} className="py-2 px-3 text-center min-w-[120px]">
                                <div className="font-semibold text-gray-800 truncate max-w-[120px]" title={r.strategy_id}>
                                  {r.strategy_id}
                                </div>
                                <div className="text-xs text-gray-400 font-mono font-normal">{r.run_id.slice(0, 10)}...</div>
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {([
                            { key: 'sharpe_ratio', labelKey: 'common.metric.sharpe_ratio', pct: false, invert: false },
                            { key: 'max_drawdown', labelKey: 'common.metric.max_drawdown', pct: true, invert: true },
                            { key: 'total_return', labelKey: 'common.metric.total_return', pct: true, invert: false },
                            { key: 'calmar_ratio', labelKey: 'common.metric.calmar_ratio', pct: false, invert: false },
                            { key: 'sortino_ratio', labelKey: 'common.metric.sortino_ratio', pct: false, invert: false },
                            { key: 'annualized_volatility', labelKey: 'common.metric.annual_volatility', pct: true, invert: true },
                            { key: 'annualized_return', labelKey: 'common.metric.annualized_return', pct: true, invert: false },
                            { key: 'win_rate', labelKey: 'common.metric.win_rate', pct: true, invert: false },
                          ] as const).map(({ key, labelKey, pct, invert }) => {
                            const vals = compareData.runs.map(r => {
                              const v = r.metrics?.[key]
                              return v !== undefined && v !== null ? Number(v) : NaN
                            })
                            const validVals = vals.filter(v => !isNaN(v))
                            const best = validVals.length
                              ? invert ? Math.min(...validVals) : Math.max(...validVals)
                              : NaN
                            return (
                              <tr key={key} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-3 text-gray-600">{t(labelKey)}</td>
                                {vals.map((v, i) => {
                                  const isBest = !isNaN(v) && v === best
                                  const display = isNaN(v)
                                    ? '-'
                                    : pct ? `${(v * 100).toFixed(2)}%` : v.toFixed(3)
                                  return (
                                    <td
                                      key={compareData.runs[i].run_id}
                                      className={`py-2 px-3 text-center font-mono ${isBest ? 'text-green-600 font-bold' : 'text-gray-700'}`}
                                    >
                                      {display}
                                    </td>
                                  )
                                })}
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* NAV overlay chart */}
                  {compareData.runs.some(r => r.nav_series.length > 0) && (
                    <div>
                      <h3 className="font-semibold text-gray-700 mb-3">{t('page.backtests_list.compare.nav_overlay')}</h3>
                      <ResponsiveContainer width="100%" height={280}>
                        <LineChart
                          data={mergeNavSeries(compareData.runs)}
                          margin={{ top: 4, right: 16, left: -20, bottom: 0 }}
                        >
                          <XAxis dataKey="date" tick={{ fontSize: 10 }} interval="preserveStartEnd"
                            tickFormatter={v => String(v).slice(5)} />
                          <YAxis tick={{ fontSize: 10 }} />
                          <Tooltip formatter={(v: number) => v.toFixed(4)} />
                          <Legend />
                          {compareData.runs.map((r, i) => {
                            const COLORS = ['#3b82f6','#ef4444','#10b981','#f59e0b','#8b5cf6','#06b6d4']
                            return (
                              <Line
                                key={r.run_id}
                                dataKey={r.run_id}
                                name={r.strategy_id}
                                stroke={COLORS[i % COLORS.length]}
                                dot={false}
                                strokeWidth={2}
                                connectNulls
                              />
                            )
                          })}
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                </>
              ) : null}
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={confirmAction !== null}
        title={confirmAction?.type === 'cancel' ? t('page.backtests_list.confirm.stop_title') : t('page.backtests_list.confirm.delete_title')}
        message={confirmAction?.type === 'cancel' ? t('page.backtests_list.confirm.stop_body') : t('page.backtests_list.confirm.delete_body')}
        confirmLabel={confirmAction?.type === 'cancel' ? t('page.backtests_list.btn.stop') : t('common.delete')}
        variant="danger"
        onConfirm={() => {
          if (confirmAction) {
            if (confirmAction.type === 'cancel') cancelMutation.mutate(confirmAction.runId)
            else deleteMutation.mutate(confirmAction.runId)
          }
          setConfirmAction(null)
        }}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  )
}
