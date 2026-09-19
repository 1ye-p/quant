import { useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { DataTable } from '@/components/ui/DataTable'
import { TradeScatter, type TradePoint } from '@/components/charts/TradeScatter'
import { TradeKlineChart } from '@/components/backtests/TradeKlineChart'
import { queryKeys } from '@/lib/queryKeys'

export function BacktestFillsTab() {
  const { id: selectedId } = useParams<{ id: string }>()
  const [view, setView] = useState<'table' | 'scatter' | 'kline'>('table')
  const [fillsPage, setFillsPage] = useState(0)
  const fillsPageSize = 50
  const { t } = useTranslation()

  const { data: fillsData } = useQuery({
    queryKey: queryKeys.backtests.fills(selectedId!, fillsPage * fillsPageSize, fillsPageSize),
    queryFn: () => backtestsApi.getFills(selectedId!, fillsPage * fillsPageSize, fillsPageSize),
    enabled: !!selectedId,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  })

  // Transform fills data for TradeScatter chart
  const tradePoints = useMemo((): TradePoint[] => {
    if (!fillsData?.items || fillsData.items.length === 0) return []

    // Group trades by asset to compute P&L
    const tradesByAsset = new Map<string, typeof fillsData.items>()
    for (const fill of fillsData.items) {
      const assetId = fill.asset_id
      if (!tradesByAsset.has(assetId)) {
        tradesByAsset.set(assetId, [])
      }
      tradesByAsset.get(assetId)!.push(fill)
    }

    const points: TradePoint[] = []

    for (const [assetId, assetFills] of tradesByAsset) {
      // Sort by date
      const sorted = [...assetFills].sort((a, b) =>
        String(a.trade_date ?? '').localeCompare(String(b.trade_date ?? ''))
      )

      // Track positions to compute P&L
      let position = 0
      let avgCost = 0

      for (const fill of sorted) {
        const side = fill.side as 'buy' | 'sell'
        const price = Number(fill.price ?? 0)
        const qty = Number(fill.qty ?? 0)

        // Compute P&L for sells
        let pnl: number | undefined = undefined
        if (side === 'sell' && position > 0 && avgCost > 0) {
          pnl = (price - avgCost) / avgCost
        }

        points.push({
          date: String(fill.trade_date ?? '').slice(0, 10),
          price,
          side,
          pnl,
          symbol: assetId.split(':').pop() ?? assetId,
          quantity: qty,
        })

        // Update position tracking
        if (side === 'buy') {
          const totalCost = avgCost * position + price * qty
          position += qty
          avgCost = position > 0 ? totalCost / position : 0
        } else {
          position = Math.max(0, position - qty)
        }
      }
    }

    // Sort by date for display
    return points.sort((a, b) => a.date.localeCompare(b.date))
  }, [fillsData])

  if (!selectedId) return null

  return (
    <div className="space-y-3">
      {/* View Tabs */}
      <div className="flex gap-2">
        <button
          className={view === 'table' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
          onClick={() => setView('table')}
        >
          {t('component.fills.tab.table')}
        </button>
        <button
          className={view === 'scatter' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
          onClick={() => setView('scatter')}
        >
          {t('component.fills.tab.scatter')}
        </button>
        <button
          className={view === 'kline' ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
          onClick={() => setView('kline')}
        >
          {t('component.fills.tab.kline')}
        </button>
      </div>

      {/* Trade Scatter Chart */}
      {view === 'scatter' && tradePoints.length > 0 && (
        <TradeScatter data={tradePoints} title={t('component.fills.scatter_title')} />
      )}

      {/* K-line Chart */}
      {view === 'kline' && selectedId && (
        <TradeKlineChart backtestId={selectedId} />
      )}

      {/* Table View */}
      {view === 'table' && (
        <DataTable
          data={(fillsData?.items ?? []) as unknown as Record<string, unknown>[]}
          rowKey={(r) => `${r.trade_date}_${r.asset_id}_${r.order_idx ?? ''}`}
          pageSize={fillsPageSize}
          enableExport
          exportFilename={`fills_${selectedId?.slice(0, 8) ?? 'backtest'}`}
          emptyText={t('component.fills.empty')}
          rowClassName={(row: Record<string, unknown>) =>
            row.reason === 'delist_forced_liquidation' ? 'bg-orange-50' : ''
          }
          backendPagination={fillsData ? {
            total: fillsData.total,
            page: fillsPage,
            onPageChange: setFillsPage,
          } : undefined}
          columns={[
            { key: 'trade_date', label: t('component.fills.column.date'), sortable: true, width: '100px',
              render: (v) => <span className="text-xs">{String(v ?? '').slice(0, 10)}</span> },
            { key: 'asset_id', label: t('component.fills.column.asset'), sortable: true, searchable: true,
              render: (v) => <span className="font-mono text-xs">{String(v)}</span> },
            { key: 'side', label: t('component.fills.column.side'), sortable: true, filterable: true, filters: ['buy', 'sell'],
              render: (v) => (
                <span className={`badge ${v === 'buy' ? 'bg-red-100 text-red-800' : 'bg-green-100 text-green-800'}`}>
                  {v === 'buy' ? t('component.fills.side.buy') : t('component.fills.side.sell')}
                </span>
              ) },
            { key: 'qty', label: t('component.fills.column.qty'), sortable: true, width: '80px',
              render: (v) => <span className="text-right block">{Number(v).toLocaleString()}</span> },
            { key: 'price', label: t('component.fills.column.price'), sortable: true, width: '80px',
              render: (v) => <span className="text-right block">{Number(v).toFixed(2)}</span> },
            { key: 'notional', label: t('component.fills.column.amount'), sortable: true, width: '120px',
              render: (v) => <span className="text-right block">{Number(v).toLocaleString(undefined, { minimumFractionDigits: 2 })}</span> },
            { key: 'total_cost', label: t('component.fills.column.cost'), sortable: true, width: '80px',
              render: (v) => <span className="text-right block text-gray-500">{Number(v).toFixed(2)}</span> },
            { key: 'reason', label: t('component.fills.column.reason'), width: '120px',
              render: (v: unknown) => {
                if (v === 'delist_forced_liquidation') return <span className="text-orange-600 font-medium">{t('component.fills.reason.forced_liquidation')}</span>
                return <span className="text-gray-400">-</span>
              } },
          ]}
        />
      )}
    </div>
  )
}
