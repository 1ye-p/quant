import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import {
  ResponsiveContainer, ComposedChart, Area, XAxis, YAxis, Tooltip,
  CartesianGrid, ReferenceArea,
} from 'recharts'
import { backtestsApi, type RegimeTimeline, type RegimeTimelineInterval } from '@/lib/api/backtests'
import { queryKeys } from '@/lib/queryKeys'
import { DataTable } from '@/components/ui/DataTable'
import { DataState } from '@/components/ui/DataState'
import { MetricCard } from '@/components/ui/MetricCard'

const STATE_COLORS: Record<string, string> = {
  full: 'rgba(34, 197, 94, 0.10)',   // green tint
  reduced: 'rgba(245, 158, 11, 0.14)', // amber tint
  flat: 'rgba(148, 163, 184, 0.20)',   // slate tint
}

const STATE_STROKES: Record<string, string> = {
  full: '#16a34a',
  reduced: '#d97706',
  flat: '#64748b',
}

interface CycleRow extends Record<string, unknown> {
  idx: number
  state: string
  scale: number
  start: string
  end: string
  days: number
  interval_return: number | null
}

export function BacktestRegimeTimelineTab() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.backtests.regimeTimeline(id!),
    queryFn: () => backtestsApi.getRegimeTimeline(id!),
    enabled: !!id,
    staleTime: 60_000,
  })

  if (!id) return null

  return (
    <DataState isLoading={isLoading} error={error}>
      {!data ? null : !data.applicable ? (
        <div className="card p-8 text-center text-gray-400">
          {t('page.backtest.regime.not_applicable')}
          <div className="mt-2 text-sm">
            {t('page.backtest.regime.not_applicable_hint')}
          </div>
        </div>
      ) : (
        <RegimeTimelineBody intervals={data.intervals ?? []} navSeries={data.nav_series ?? []} cycles={data.cycles ?? 0} minCycles={data.min_cycles ?? 6} sufficient={data.sufficient ?? false} contribution={data.contribution} />
      )}
    </DataState>
  )
}

function RegimeTimelineBody({
  intervals, navSeries, cycles, minCycles, sufficient, contribution,
}: {
  intervals: RegimeTimelineInterval[]
  navSeries: { date: string; nav: number }[]
  cycles: number
  minCycles: number
  sufficient: boolean
  contribution: NonNullable<RegimeTimeline['contribution']> | undefined
}) {
  const { t } = useTranslation()

  const chartData = navSeries
  const rows: CycleRow[] = intervals.map((itv, i) => ({
    idx: i + 1,
    state: itv.state,
    scale: itv.scale,
    start: itv.start,
    end: itv.end,
    days: itv.days,
    interval_return: itv.interval_return,
  }))

  return (
    <div className="space-y-6">
      {/* Insufficient-cycle warning */}
      {!sufficient && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {t('page.backtest.regime.insufficient_warning', { cycles, threshold: minCycles })}
        </div>
      )}

      {/* NAV chart with state shading */}
      <div className="card p-4">
        <h3 className="text-sm font-medium mb-3">{t('page.backtest.regime.chart_title')}</h3>
        <ResponsiveContainer width="100%" height={280}>
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={60} />
            <YAxis yAxisId="nav" domain={['auto', 'auto']} tick={{ fontSize: 11 }} />
            <YAxis yAxisId="scale" orientation="right" domain={[0, 1]} tick={{ fontSize: 11 }} />
            <Tooltip
              formatter={(value: unknown, name: unknown) =>
                name === 'nav' ? [Number(value).toFixed(4), t('page.backtest.regime.nav')] :
                name === 'desired' ? [Number(value).toFixed(2), t('page.backtest.regime.desired_scale')] :
                [Number(value).toFixed(2), t('page.backtest.regime.actual_scale')]}
            />
            {intervals.map((itv, i) => (
              <ReferenceArea
                key={i}
                x1={itv.start}
                x2={itv.end}
                yAxisId="nav"
                fill={STATE_COLORS[itv.state] ?? 'transparent'}
                fillOpacity={1}
                stroke={STATE_STROKES[itv.state] ?? 'transparent'}
                strokeOpacity={0.4}
                ifOverflow="extendDomain"
              />
            ))}
            <Area yAxisId="nav" type="monotone" dataKey="nav" stroke="#2563eb" fill="rgba(37, 99, 235, 0.08)" dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
        {/* Legend */}
        <div className="flex gap-4 mt-2 text-xs text-gray-500">
          {(['full', 'reduced', 'flat'] as const).map(s => (
            <span key={s} className="inline-flex items-center gap-1">
              <span className="inline-block w-3 h-3 rounded" style={{ background: STATE_COLORS[s], border: `1px solid ${STATE_STROKES[s]}` }} />
              {t(`page.backtest.regime.state_${s}`)}
            </span>
          ))}
        </div>
      </div>

      {/* Contribution cards */}
      {contribution && (
        <div>
          <h3 className="text-sm font-medium mb-3">{t('page.backtest.regime.contribution_title')}</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <MetricCard
              label={t('page.backtest.regime.held_return')}
              value={`${(contribution.held_return * 100).toFixed(2)}%`}
              sub={t('page.backtest.regime.held_days', { days: contribution.held_days })}
            />
            <MetricCard
              label={t('page.backtest.regime.avoided')}
              value={`${(contribution.out_of_market.avoided * 100).toFixed(2)}%`}
              sub={t('page.backtest.regime.flat_days', { days: contribution.flat_days })}
            />
            <MetricCard
              label={t('page.backtest.regime.missed')}
              value={`${(contribution.out_of_market.missed * 100).toFixed(2)}%`}
              sub={contribution.out_of_market.benchmark_source === 'mean_return_approx'
                ? t('page.backtest.regime.benchmark_approx_note')
                : t('page.backtest.regime.flat_days', { days: contribution.flat_days })}
            />
          </div>
        </div>
      )}

      {/* Cycle stats table */}
      <div>
        <h3 className="text-sm font-medium mb-3">
          {t('page.backtest.regime.cycle_stats_title')}
          <span className="ml-2 text-xs text-gray-400">
            {t('page.backtest.regime.cycles_count', { cycles, threshold: minCycles })}
          </span>
        </h3>
        <DataTable
          data={rows}
          rowKey="idx"
          pageSize={15}
          enableExport
          exportFilename="regime_cycles"
          columns={[
            { key: 'idx', label: '#', sortable: true, width: '50px' },
            { key: 'state', label: t('page.backtest.regime.col_state'), sortable: true, filterable: true,
              filters: ['full', 'reduced', 'flat'],
              render: (v) => (
                <span className={`badge ${
                  v === 'full' ? 'bg-green-100 text-green-800'
                  : v === 'reduced' ? 'bg-amber-100 text-amber-800'
                  : 'bg-gray-100 text-gray-700'}`}>
                  {t(`page.backtest.regime.state_${v}`)}
                </span>
              ) },
            { key: 'scale', label: t('page.backtest.regime.col_scale'), sortable: true,
              render: (v) => Number(v).toFixed(2) },
            { key: 'start', label: t('page.backtest.regime.col_start'), sortable: true },
            { key: 'end', label: t('page.backtest.regime.col_end'), sortable: true },
            { key: 'days', label: t('page.backtest.regime.col_days'), sortable: true },
            { key: 'interval_return', label: t('page.backtest.regime.col_return'), sortable: true,
              render: (v) => v == null ? '—' : (
                <span className={Number(v) >= 0 ? 'text-green-600' : 'text-red-600'}>
                  {(Number(v) * 100).toFixed(2)}%
                </span>
              ) },
          ]}
        />
      </div>
    </div>
  )
}
