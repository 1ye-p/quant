/**
 * B4b-2 — Positions tab: point-in-time holdings visualization on top of the
 * positions-series endpoint (B4b-1).
 *
 * Two stacked-area views (recharts, project chart stack):
 * - weight   → per-asset stacked weights, Top-N by raw weight + `__other__` rollup
 * - industry → sector-aggregated weights (see granularity note below)
 *
 * Plus a simple time-window control (full / second half / last quarter) and a
 * latest-date holdings detail table (DataTable reuse).
 *
 * Copy note (carried from B4b-1 spot check): `silver_assets.industry` holds the
 * listing board (Main/ChiNext/STAR/BSE), NOT a SW industry classification —
 * the industry view labels state this explicitly. `'未知'` (backend-normalized
 * empty industry) and `'__other__'` (Top-N rollup) get i18n display keys.
 *
 * Tri-state 404 mirrors the signals tab: `run_not_found` / `no_position_data`.
 */

import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import {
  backtestsApi,
  weightPoints,
  industryPoints,
} from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { queryKeys } from '@/lib/queryKeys'
import { DataTable } from '@/components/ui/DataTable'
import { DataState } from '@/components/ui/DataState'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, Legend,
} from 'recharts'

type PositionsReason = 'run_not_found' | 'no_position_data'
type Metric = 'weight' | 'industry'
type WindowKey = 'all' | 'half' | 'quarter'

const REASONS: readonly PositionsReason[] = ['run_not_found', 'no_position_data']
const TOP_N_OPTIONS = [5, 10, 20] as const
/** Fixed cap for the latest-holdings table (endpoint max top_n = 50). */
const TABLE_TOP_N = 50

const STACK_COLORS = [
  '#2563eb', '#16a34a', '#d97706', '#dc2626', '#7c3aed',
  '#0891b2', '#db2777', '#65a30d', '#ea580c', '#4f46e5',
  '#0d9488', '#9333ea', '#b45309', '#1d4ed8', '#15803d',
  '#be123c', '#a16207', '#475569', '#7e22ce', '#065f46',
] as const

/** Extract the tri-state 404 reason from an ApiError (null for other errors). */
function extractReason(error: unknown): PositionsReason | null {
  if (!(error instanceof ApiError) || error.status !== 404) return null
  const reason = (error.details as { reason?: unknown } | undefined)?.reason
  return REASONS.includes(reason as PositionsReason) ? (reason as PositionsReason) : null
}

function fmtWeight(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

/** Slice a date-ascending series to the selected window (client-side). */
function applyWindow<T>(series: T[], win: WindowKey): T[] {
  if (win === 'all' || series.length === 0) return series
  const frac = win === 'half' ? 0.5 : 0.25
  return series.slice(Math.floor(series.length * (1 - frac)))
}

export function BacktestPositionsTab() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const [metric, setMetric] = useState<Metric>('weight')
  const [topN, setTopN] = useState<number>(10)
  const [win, setWin] = useState<WindowKey>('all')

  const chartQuery = useQuery({
    queryKey: queryKeys.backtests.positionsSeries(id!, metric, topN),
    queryFn: () => backtestsApi.getPositionsSeries(id!, metric, topN),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  })

  // Latest-date holdings table — always the weight shape at the max cap so the
  // table is stable regardless of the chart view / Top-N selection.
  const tableQuery = useQuery({
    queryKey: queryKeys.backtests.positionsSeries(id!, 'weight', TABLE_TOP_N),
    queryFn: () => backtestsApi.getPositionsSeries(id!, 'weight', TABLE_TOP_N),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  })

  const reason = extractReason(chartQuery.error) ?? extractReason(tableQuery.error)

  // ── Chart series (view-shaped) ────────────────────────────────────────────
  const { chartRows, stackKeys } = useMemo(() => {
    const data = chartQuery.data
    if (!data) return { chartRows: [] as Array<Record<string, number | string>>, stackKeys: [] as string[] }
    if (metric === 'weight') {
      const points = weightPoints(data)
      const keys = new Set<string>()
      const rows = points.map(p => {
        const row: Record<string, number | string> = { date: p.trade_date }
        for (const pos of p.positions) {
          if (pos.weight == null) continue
          keys.add(pos.asset_id)
          row[pos.asset_id] = pos.weight
        }
        return row
      })
      // Stable key order: __other__ last, others by first-appearance
      const ordered = [...keys].filter(k => k !== '__other__')
      if (keys.has('__other__')) ordered.push('__other__')
      // Same churn issue as the industry view: absent key = weight 0.
      const filled = rows.map(row => ({
        date: row.date,
        ...Object.fromEntries(ordered.map(k => [k, Number(row[k] ?? 0)])),
      }))
      return { chartRows: filled, stackKeys: ordered }
    }
    const points = industryPoints(data)
    const keys = new Set<string>()
    const raw = points.map(p => {
      const row: Record<string, number | string> = { date: p.trade_date }
      for (const [industry, w] of Object.entries(p.weights)) {
        keys.add(industry)
        row[industry] = w
      }
      return row
    })
    // Top-N membership churns day to day — a key absent from a given day must
    // read as weight 0, not undefined, or the Recharts stack breaks/gaps at
    // those indices instead of summing to ~100%.
    const stackKeys = [...keys].sort()
    const rows = raw.map(row => ({
      date: row.date,
      ...Object.fromEntries(stackKeys.map(k => [k, Number(row[k] ?? 0)])),
    }))
    return { chartRows: rows, stackKeys }
  }, [chartQuery.data, metric])

  const windowedRows = useMemo(() => applyWindow(chartRows, win), [chartRows, win])

  // ── Latest-date table rows ────────────────────────────────────────────────
  const tableRows = useMemo(() => {
    const points = weightPoints(tableQuery.data)
    const rows = points.length > 0
      ? points[points.length - 1].positions
      : []
    return rows.map(r => ({ ...r }) as Record<string, unknown>)
  }, [tableQuery.data])
  const latestDate = useMemo(() => {
    const points = weightPoints(tableQuery.data)
    return points.length > 0 ? points[points.length - 1].trade_date : ''
  }, [tableQuery.data])

  if (!id) return null

  // Tri-state 404 copy (signals-tab banner style)
  if (reason) {
    return (
      <div className="card p-8 text-center text-gray-400" data-testid={`positions-${reason}`}>
        {t(`page.backtest.positions.reason.${reason}`)}
        <div className="mt-2 text-sm">
          {t(`page.backtest.positions.reason.${reason}_hint`)}
        </div>
      </div>
    )
  }

  /** Display label for a stack/table key: `__other__` / `'未知'` get i18n keys. */
  const displayKey = (key: string): string =>
    key === '__other__'
      ? t('page.backtest.positions.other')
      : key === '未知'
        ? t('page.backtest.positions.unknown')
        : key

  return (
    <DataState isLoading={chartQuery.isLoading} error={chartQuery.error}>
      <div className="space-y-3">
        {/* View / Top-N / window controls */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex rounded-lg border border-gray-200 overflow-hidden" data-testid="positions-view-toggle">
            {(['weight', 'industry'] as const).map(m => (
              <button
                key={m}
                onClick={() => setMetric(m)}
                className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                  metric === m
                    ? 'bg-brand-600 text-white'
                    : 'bg-white text-gray-600 hover:text-gray-800'
                }`}
                data-testid={`positions-view-${m}`}
              >
                {t(`page.backtest.positions.view_${m}`)}
              </button>
            ))}
          </div>

          {metric === 'weight' && (
            <label htmlFor="positions-topn" className="flex items-center gap-2 text-sm text-gray-500">
              <span>{t('page.backtest.positions.top_n_label')}</span>
              <select
                id="positions-topn"
                className="input text-sm w-20"
                value={topN}
                onChange={e => setTopN(Number(e.target.value))}
                data-testid="positions-topn-select"
              >
                {TOP_N_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          )}

          <label htmlFor="positions-window" className="flex items-center gap-2 text-sm text-gray-500">
            <span>{t('page.backtest.positions.window_label')}</span>
            <select
              id="positions-window"
              className="input text-sm w-28"
              value={win}
              onChange={e => setWin(e.target.value as WindowKey)}
              data-testid="positions-window-select"
            >
              <option value="all">{t('page.backtest.positions.window_all')}</option>
              <option value="half">{t('page.backtest.positions.window_half')}</option>
              <option value="quarter">{t('page.backtest.positions.window_quarter')}</option>
            </select>
          </label>
        </div>

        {/* Top-N semantics note (weight view) / board-granularity note (industry view) */}
        <p className="text-xs text-gray-400" data-testid="positions-note">
          {metric === 'weight'
            ? t('page.backtest.positions.top_n_hint')
            : t('page.backtest.positions.industry_granularity_note')}
        </p>

        {/* Stacked area chart */}
        <div className="card p-4" data-testid="positions-chart">
          <ResponsiveContainer width="100%" height={360}>
            <AreaChart data={windowedRows} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis
                tick={{ fontSize: 11 }}
                tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
              />
              <Tooltip
                formatter={(v: unknown, name: unknown) =>
                  [fmtWeight(v as number | null), displayKey(String(name))]
                }
              />
              <Legend formatter={(v: unknown) => displayKey(String(v))} />
              {stackKeys.map((key, idx) => (
                <Area
                  key={key}
                  type="monotone"
                  dataKey={key}
                  name={key}
                  stackId="1"
                  stroke={STACK_COLORS[idx % STACK_COLORS.length]}
                  fill={STACK_COLORS[idx % STACK_COLORS.length]}
                  fillOpacity={0.85}
                  isAnimationActive={false}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Latest holdings detail table */}
        <div>
          <h3 className="font-semibold text-gray-800 mb-2 text-sm">
            {t('page.backtest.positions.latest_title')}
            {latestDate && (
              <span className="ml-2 text-xs font-normal text-gray-400">
                {t('page.backtest.positions.latest_date', { date: latestDate })}
              </span>
            )}
          </h3>
          <DataTable
            data={tableRows}
            rowKey="asset_id"
            pageSize={20}
            enableExport
            exportFilename={`positions_${id.slice(0, 8)}${latestDate ? `_${latestDate}` : ''}`}
            emptyText={t('page.backtest.positions.empty')}
            columns={[
              { key: 'asset_id', label: t('page.backtest.positions.col_asset'), sortable: true, searchable: true,
                render: v => (
                  <span className={`font-mono text-xs ${String(v) === '__other__' ? 'text-gray-400 italic' : ''}`}>
                    {displayKey(String(v))}
                  </span>
                ) },
              { key: 'weight', label: t('page.backtest.positions.col_weight'), sortable: true,
                render: v => <span className="font-mono">{fmtWeight(v as number | null)}</span> },
              { key: 'industry', label: t('page.backtest.positions.col_industry'), sortable: true,
                render: v => v == null
                  ? <span className="text-gray-400">—</span>
                  : <span>{displayKey(String(v))}</span> },
            ]}
          />
        </div>
      </div>
    </DataState>
  )
}
