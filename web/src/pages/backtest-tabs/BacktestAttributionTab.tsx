/**
 * B1 — Brinson attribution tab (four-metric cards + sector detail +
 * rebalance-period detail), with tri-state 404 copy as the primary path
 * (production runs mostly return `no_attribution` until the write-side
 * backlog fix lands).
 */

import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { backtestsApi, type AttributionSector } from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { queryKeys } from '@/lib/queryKeys'
import { DataTable } from '@/components/ui/DataTable'
import { DataState } from '@/components/ui/DataState'
import { MetricCard } from '@/components/ui/MetricCard'

type AttributionReason = 'run_not_found' | 'no_analysis_run' | 'no_attribution'

const REASONS: readonly AttributionReason[] = [
  'run_not_found',
  'no_analysis_run',
  'no_attribution',
]

/** Extract the tri-state 404 reason from an ApiError (null for other errors). */
function extractReason(error: unknown): AttributionReason | null {
  if (!(error instanceof ApiError) || error.status !== 404) return null
  const reason = (error.details as { reason?: unknown } | undefined)?.reason
  return REASONS.includes(reason as AttributionReason) ? (reason as AttributionReason) : null
}

/** Format a fractional return as a signed percentage ('—' when null). */
function fmtPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

function pctClass(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return 'text-gray-400'
  return Number(value) >= 0 ? 'text-green-600' : 'text-red-600'
}

interface SectorRow extends Record<string, unknown> {
  sector: string
  allocation: number
  selection: number
  interaction: number
  total: number
}

/**
 * Derive per-sector Brinson-Fachler effects client-side from stored
 * weights/returns (allocation = (wp−wb)·(rb−Rb), selection = wb·(rp−rb),
 * interaction = (wp−wb)·(rp−rb)), sorted by |active contribution| desc.
 */
function buildSectorRows(sectors: AttributionSector[], benchTotal: number | null): SectorRow[] {
  const rbTotal = Number(benchTotal ?? 0)
  return sectors
    .map(s => {
      const wp = Number(s.port_weight ?? 0)
      const wb = Number(s.bench_weight ?? 0)
      const rp = Number(s.port_return ?? 0)
      const rb = Number(s.bench_return ?? 0)
      const allocation = (wp - wb) * (rb - rbTotal)
      const selection = wb * (rp - rb)
      const interaction = (wp - wb) * (rp - rb)
      return { sector: s.sector, allocation, selection, interaction, total: allocation + selection + interaction }
    })
    .sort((a, b) => Math.abs(b.total) - Math.abs(a.total))
}

export function BacktestAttributionTab() {
  const { id } = useParams<{ id: string }>()
  const { t } = useTranslation()

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.backtests.attribution(id!),
    queryFn: () => backtestsApi.getAttribution(id!),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  })

  const reason = extractReason(error)
  const sectorRows = useMemo(
    () => buildSectorRows(data?.sectors ?? [], data?.summary.benchmark_return ?? null),
    [data],
  )

  if (!id) return null

  // Tri-state 404 copy — the primary path in production until the write-side fix
  if (reason) {
    return (
      <div className="card p-8 text-center text-gray-400" data-testid={`attribution-${reason}`}>
        {t(`page.backtest.attribution.reason.${reason}`)}
        <div className="mt-2 text-sm">
          {t(`page.backtest.attribution.reason.${reason}_hint`)}
        </div>
      </div>
    )
  }

  return (
    <DataState isLoading={isLoading} error={error}>
      {!data ? null : (
        <div className="space-y-6">
          {/* Metric cards: returns + three Brinson effects */}
          <div>
            <h3 className="text-sm font-medium mb-3">{t('page.backtest.attribution.summary_title')}</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              <MetricCard
                label={t('page.backtest.attribution.total_return')}
                value={fmtPct(data.summary.total_return)}
              />
              <MetricCard
                label={t('page.backtest.attribution.benchmark_return')}
                value={fmtPct(data.summary.benchmark_return)}
              />
              <MetricCard
                label={t('page.backtest.attribution.active_return')}
                value={fmtPct(data.summary.active_return)}
                warn={Number(data.summary.active_return ?? 0) < 0}
                good={Number(data.summary.active_return ?? 0) >= 0}
              />
              <MetricCard
                label={t('page.backtest.attribution.allocation')}
                value={fmtPct(data.summary.allocation)}
              />
              <MetricCard
                label={t('page.backtest.attribution.selection')}
                value={fmtPct(data.summary.selection)}
              />
              <MetricCard
                label={t('page.backtest.attribution.interaction')}
                value={fmtPct(data.summary.interaction)}
              />
            </div>
          </div>

          {/* Sector detail */}
          <div>
            <h3 className="text-sm font-medium mb-3">{t('page.backtest.attribution.sectors_title')}</h3>
            {sectorRows.length === 0 ? (
              <div className="card p-8 text-center text-sm text-gray-400" data-testid="attribution-sectors-empty">
                {t('page.backtest.attribution.sectors_empty')}
              </div>
            ) : (
              <DataTable
                data={sectorRows}
                rowKey="sector"
                pageSize={20}
                enableExport
                exportFilename={`attribution_sectors_${id.slice(0, 8)}`}
                columns={[
                  { key: 'sector', label: t('page.backtest.attribution.col_sector'), sortable: true, searchable: true },
                  { key: 'allocation', label: t('page.backtest.attribution.col_allocation'), sortable: true,
                    render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
                  { key: 'selection', label: t('page.backtest.attribution.col_selection'), sortable: true,
                    render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
                  { key: 'interaction', label: t('page.backtest.attribution.col_interaction'), sortable: true,
                    render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
                  { key: 'total', label: t('page.backtest.attribution.col_total'), sortable: true,
                    render: v => <span className={`font-medium ${pctClass(v as number | null)}`}>{fmtPct(v as number | null)}</span> },
                ]}
              />
            )}
          </div>

          {/* Rebalance-period detail (backend field `daily` kept as-is) */}
          <div>
            <h3 className="text-sm font-medium mb-3">
              {t('page.backtest.attribution.periods_title')}
              <span className="ml-2 text-xs text-gray-400">
                {t('page.backtest.attribution.periods_note')}
              </span>
            </h3>
            <DataTable
              data={(data.periods ?? []) as unknown as Record<string, unknown>[]}
              rowKey="date"
              pageSize={500}
              enableExport
              exportFilename={`attribution_periods_${id.slice(0, 8)}`}
              emptyText={t('page.backtest.attribution.periods_empty')}
              columns={[
                { key: 'date', label: t('page.backtest.attribution.col_date'), sortable: true, width: '120px' },
                { key: 'allocation', label: t('page.backtest.attribution.col_allocation'), sortable: true,
                  render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
                { key: 'selection', label: t('page.backtest.attribution.col_selection'), sortable: true,
                  render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
                { key: 'interaction', label: t('page.backtest.attribution.col_interaction'), sortable: true,
                  render: v => <span className={pctClass(v as number | null)}>{fmtPct(v as number | null)}</span> },
              ]}
            />
          </div>
        </div>
      )}
    </DataState>
  )
}
