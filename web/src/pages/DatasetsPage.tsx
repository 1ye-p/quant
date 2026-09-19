import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation } from '@tanstack/react-query'
import { datasetsApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { DataState } from '@/components/ui/DataState'
import { DataPreview } from '@/components/datasets/DataPreview'
import { FieldStats } from '@/components/datasets/FieldStats'
import { QualityReport } from '@/components/datasets/QualityReport'
import { AnomalyMarkers } from '@/components/datasets/AnomalyMarkers'
import { PriceChart } from '@/components/datasets/PriceChart'

type DetailTab = 'preview' | 'stats' | 'quality' | 'anomalies' | 'compare' | 'prices'

const TABS: { key: DetailTab; labelKey: string }[] = [
  { key: 'preview', labelKey: 'page.datasets.tab.preview' },
  { key: 'stats', labelKey: 'page.datasets.tab.stats' },
  { key: 'quality', labelKey: 'page.datasets.tab.quality' },
  { key: 'anomalies', labelKey: 'page.datasets.tab.anomalies' },
  { key: 'compare', labelKey: 'page.datasets.tab.compare' },
  { key: 'prices', labelKey: 'page.datasets.tab.prices' },
]

function VersionComparison({ versions }: { versions: { version_id: string; dataset_name: string }[] }) {
  const { t } = useTranslation()
  const [versionA, setVersionA] = useState('')
  const [versionB, setVersionB] = useState('')
  const [triggered, setTriggered] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.datasets.compare(versionA, versionB),
    queryFn: () => datasetsApi.compareVersions(versionA, versionB),
    enabled: triggered && !!versionA && !!versionB && versionA !== versionB,
  })

  const handleCompare = () => {
    if (versionA && versionB && versionA !== versionB) {
      setTriggered(true)
    }
  }

  return (
    <div className="space-y-4">
      {/* Selectors */}
      <div className="flex items-end gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.compare.version_a')}</label>
          <select
            value={versionA}
            onChange={e => { setVersionA(e.target.value); setTriggered(false) }}
            className="input-field text-sm w-48"
          >
            <option value="">{t('page.datasets.compare.select_version')}</option>
            {versions.map(v => (
              <option key={v.version_id} value={v.version_id}>{v.dataset_name}</option>
            ))}
          </select>
        </div>
        <div className="text-gray-400 text-sm pb-1">{t('page.datasets.compare.vs')}</div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.compare.version_b')}</label>
          <select
            value={versionB}
            onChange={e => { setVersionB(e.target.value); setTriggered(false) }}
            className="input-field text-sm w-48"
          >
            <option value="">{t('page.datasets.compare.select_version')}</option>
            {versions.map(v => (
              <option key={v.version_id} value={v.version_id}>{v.dataset_name}</option>
            ))}
          </select>
        </div>
        <button
          onClick={handleCompare}
          disabled={!versionA || !versionB || versionA === versionB}
          className="btn-primary text-sm disabled:opacity-40"
        >
          {t('page.datasets.compare.btn_compare')}
        </button>
      </div>

      {/* Empty / error states */}
      {!triggered && (
        <div className="card flex items-center justify-center h-32 text-gray-400 text-sm">
          {t('page.datasets.compare.hint_select_two')}
        </div>
      )}
      {triggered && isLoading && (
        <div className="card flex items-center justify-center h-32 text-gray-400 text-sm">
          {t('common.loading')}
        </div>
      )}
      {triggered && error && (
        <div className="card flex items-center justify-center h-32 text-red-500 text-sm">
          {t('page.datasets.compare.failed', { message: (error as Error).message })}
        </div>
      )}

      {/* Results */}
      {data && (
        <div className="space-y-4">
          {/* Summary cards */}
          <div className="grid grid-cols-4 gap-3">
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500">{t('page.datasets.compare.summary.version_a_count')}</div>
              <div className="text-lg font-semibold mt-1">{data.row_changes.version_a_count.toLocaleString()}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500">{t('page.datasets.compare.summary.version_b_count')}</div>
              <div className="text-lg font-semibold mt-1">{data.row_changes.version_b_count.toLocaleString()}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500">{t('page.datasets.compare.summary.added')}</div>
              <div className="text-lg font-semibold mt-1 text-green-600">+{data.row_changes.added.toLocaleString()}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500">{t('page.datasets.compare.summary.removed')}</div>
              <div className="text-lg font-semibold mt-1 text-red-600">-{data.row_changes.removed.toLocaleString()}</div>
            </div>
          </div>

          {/* Field changes */}
          {(data.field_changes.added_fields.length > 0 || data.field_changes.removed_fields.length > 0) && (
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <h3 className="text-sm font-medium text-gray-700 mb-2">{t('page.datasets.compare.field_changes')}</h3>
              <div className="flex flex-wrap gap-2">
                {data.field_changes.added_fields.map(f => (
                  <span key={f} className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">+ {f}</span>
                ))}
                {data.field_changes.removed_fields.map(f => (
                  <span key={f} className="text-xs bg-red-100 text-red-700 px-2 py-0.5 rounded-full">- {f}</span>
                ))}
                {data.field_changes.common_fields.length > 0 && (
                  <span className="text-xs text-gray-400">{t('page.datasets.compare.common_fields', { count: data.field_changes.common_fields.length })}</span>
                )}
              </div>
            </div>
          )}

          {/* Field stats table */}
          {data.field_stats.length > 0 && (
            <div className="bg-white rounded-xl shadow-sm border p-4 overflow-x-auto">
              <h3 className="text-sm font-medium text-gray-700 mb-3">{t('page.datasets.compare.stats_table')}</h3>
              <table className="w-full text-xs">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="table-th text-left">{t('page.datasets.compare.column.field')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.a_mean')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.b_mean')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.diff')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.change_rate')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.a_null_rate')}</th>
                    <th className="table-th text-right">{t('page.datasets.compare.column.b_null_rate')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.field_stats.map(row => {
                    const isSignificant = Math.abs(row.change.mean_pct_change) > 10
                    return (
                      <tr key={row.field} className={isSignificant ? 'bg-amber-50' : ''}>
                        <td className="table-td font-medium">{row.field}</td>
                        <td className="table-td text-right">{row.version_a.mean.toFixed(4)}</td>
                        <td className="table-td text-right">{row.version_b.mean.toFixed(4)}</td>
                        <td className={`table-td text-right ${row.change.mean_diff > 0 ? 'text-green-600' : row.change.mean_diff < 0 ? 'text-red-600' : ''}`}>
                          {row.change.mean_diff > 0 ? '+' : ''}{row.change.mean_diff.toFixed(4)}
                        </td>
                        <td className={`table-td text-right font-medium ${isSignificant ? 'text-amber-700' : ''}`}>
                          {row.change.mean_pct_change > 0 ? '+' : ''}{row.change.mean_pct_change.toFixed(2)}%
                        </td>
                        <td className="table-td text-right">{(row.version_a.null_rate * 100).toFixed(1)}%</td>
                        <td className="table-td text-right">{(row.version_b.null_rate * 100).toFixed(1)}%</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function DatasetsPage() {
  const { t } = useTranslation()
  const [selectedVersion, setSelectedVersion] = useState('')
  const [activeTab, setActiveTab] = useState<DetailTab>('preview')

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.datasets.list(100),
    queryFn: () => datasetsApi.list(100),
  })

  const { data: scheduleStatus, refetch: refetchSchedule } = useQuery({
    queryKey: ['datasets', 'schedule'],
    queryFn: datasetsApi.scheduleStatus,
    refetchInterval: 30_000,
  })

  const triggerMutation = useMutation({
    mutationFn: () => datasetsApi.triggerIngest(),
    onSuccess: () => {
      refetchSchedule()
    },
  })

  const handleSelectVersion = (id: string) => {
    setSelectedVersion(prev => {
      if (prev === id) return ''
      setActiveTab('preview')
      return id
    })
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">{t('page.datasets.title')}</h1>
        <p className="page-subtitle">{t('page.datasets.subtitle', { count: data?.total ?? 0 })}</p>
      </div>
      <div className="flex justify-end">
        <Link to="/datasets/external-indicators" className="btn-secondary text-xs">
          {t('page.datasets.ext_ind.link_open')}
        </Link>
      </div>

      {/* Schedule status bar */}
      {scheduleStatus && (
        <div className={`card flex items-center justify-between ${
          scheduleStatus.last_status === 'error' ? 'border-red-200 bg-red-50' : ''
        }`}>
          <div className="flex items-center gap-3">
            <div>
              <span className="text-sm font-medium text-gray-700">{t('page.datasets.schedule.label')}</span>
              <span className={`ml-2 text-xs px-2 py-0.5 rounded-full ${
                scheduleStatus.last_status === 'running'
                  ? 'bg-blue-100 text-blue-700 animate-pulse'
                  : scheduleStatus.last_status === 'success'
                  ? 'bg-green-100 text-green-700'
                  : scheduleStatus.last_status === 'error'
                  ? 'bg-red-100 text-red-700'
                  : 'bg-gray-100 text-gray-600'
              }`}>
                {scheduleStatus.last_status === 'running' ? t('page.datasets.schedule.status.running')
                  : scheduleStatus.last_status === 'success' ? t('page.datasets.schedule.status.success')
                  : scheduleStatus.last_status === 'error' ? t('page.datasets.schedule.status.error')
                  : t('page.datasets.schedule.status.pending')}
              </span>
            </div>
            <div className="text-xs text-gray-500 space-x-3">
              {scheduleStatus.last_data_date && (
                <span>{t('page.datasets.schedule.last_data_date')}<strong className="text-gray-700">{scheduleStatus.last_data_date}</strong></span>
              )}
              {scheduleStatus.last_run && (
                <span>{t('page.datasets.schedule.last_run')}{scheduleStatus.last_run.slice(0, 16).replace('T', ' ')}</span>
              )}
              {scheduleStatus.next_run && (
                <span>{t('page.datasets.schedule.next_run')}{scheduleStatus.next_run.slice(0, 16).replace('T', ' ')}</span>
              )}
            </div>
          </div>
          <button
            onClick={() => triggerMutation.mutate()}
            disabled={scheduleStatus.last_status === 'running' || triggerMutation.isPending}
            className="btn-secondary text-xs disabled:opacity-40"
          >
            {scheduleStatus.last_status === 'running' ? t('page.datasets.schedule.status.running') : t('page.datasets.schedule.btn_update_now')}
          </button>
        </div>
      )}
      {scheduleStatus?.last_error && (
        <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
          {t('page.datasets.schedule.error_prefix')}{scheduleStatus.last_error}
        </div>
      )}

      {/* Main layout: list + detail */}
      <div className="grid grid-cols-12 gap-4">
        {/* Left: dataset list (4 cols) */}
        <div className="col-span-4">
          <DataState isLoading={isLoading} error={error} isEmpty={!data?.items.length} emptyText={t('page.datasets.list.empty')}>
            <div className="card p-0 overflow-hidden max-h-[calc(100vh-280px)] overflow-y-auto">
              <table className="w-full">
                <thead className="bg-gray-50 sticky top-0 z-10">
                  <tr>
                    <th className="table-th text-xs">{t('page.datasets.list.column.name')}</th>
                    <th className="table-th text-xs">{t('page.datasets.list.column.frequency')}</th>
                    <th className="table-th text-xs">{t('page.datasets.list.column.asset_count')}</th>
                    <th className="table-th text-xs">{t('page.datasets.list.column.row_count')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map(d => (
                    <tr
                      key={d.version_id}
                      className={`table-row cursor-pointer transition-colors ${
                        selectedVersion === d.version_id
                          ? 'bg-brand-50 border-l-2 border-brand-500'
                          : 'hover:bg-gray-50'
                      }`}
                      onClick={() => handleSelectVersion(d.version_id)}
                    >
                      <td className="table-td font-medium text-xs">
                        <div className="truncate max-w-[120px]" title={d.dataset_name}>{d.dataset_name}</div>
                        <div className="text-[10px] text-gray-400">{d.frequency}</div>
                      </td>
                      <td className="table-td text-xs text-gray-500">{d.start_date?.slice(5)}</td>
                      <td className="table-td text-xs">{d.asset_count ?? '—'}</td>
                      <td className="table-td text-xs">{d.row_count?.toLocaleString() ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataState>
        </div>

        {/* Right: detail tabs (8 cols) */}
        <div className="col-span-8">
          {!selectedVersion ? (
            <div className="card flex items-center justify-center h-64 text-gray-400 text-sm">
              {t('page.datasets.detail.select_prompt')}
            </div>
          ) : (
            <div className="space-y-4">
              {/* Tab navigation */}
              <div className="flex gap-1 border-b border-gray-200">
                {TABS.map(tab => (
                  <button
                    key={tab.key}
                    className={`px-4 py-2 text-sm font-medium transition-colors -mb-px ${
                      activeTab === tab.key
                        ? 'border-b-2 border-brand-500 text-brand-600'
                        : 'text-gray-500 hover:text-gray-700'
                    }`}
                    onClick={() => setActiveTab(tab.key)}
                  >
                    {t(tab.labelKey)}
                  </button>
                ))}
              </div>

              {/* Tab content */}
              {activeTab === 'compare' ? (
                <VersionComparison versions={data?.items ?? []} />
              ) : activeTab === 'prices' ? (
                <PriceChart />
              ) : (
                <div className="card">
                  {activeTab === 'preview' && <DataPreview datasetId={selectedVersion} />}
                  {activeTab === 'stats' && <FieldStats datasetId={selectedVersion} />}
                  {activeTab === 'quality' && <QualityReport datasetId={selectedVersion} />}
                  {activeTab === 'anomalies' && <AnomalyMarkers datasetId={selectedVersion} />}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
