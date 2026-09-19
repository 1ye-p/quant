import { useState, useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation } from '@tanstack/react-query'
import { keepPreviousData } from '@tanstack/react-query'
import { toast } from 'sonner'
import { scoringApi, factorAnalyticsApi } from '@/lib/api'
import { DataTable } from '@/components/ui/DataTable'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { ScoreHistory } from '@/components/scoring/ScoreHistory'

interface FactorWeightConfig {
  factor_name: string
  weight: number
  direction: 'long' | 'short'
}

const PAGE_SIZE = 50

type TabKey = 'current' | 'history'

export function ScoringPage() {
  const { t } = useTranslation()
  const location = useLocation()
  const [activeTab, setActiveTab] = useState<TabKey>('current')
  const [name, setName] = useState('momentum_value_v1')
  const [featureSetVersion, setFeatureSetVersion] = useState('')
  const [startDate, setStartDate] = useState('2024-01-01')
  const [endDate, setEndDate] = useState('2025-06-30')
  const [factors, setFactors] = useState<FactorWeightConfig[]>([
    { factor_name: 'ret_20d', weight: 1.0, direction: 'long' },
  ])

  // Pre-fill factors from navigation state (e.g. from FactorsPage)
  useEffect(() => {
    const state = location.state as { selectedFactors?: string[] } | null
    if (state?.selectedFactors && state.selectedFactors.length > 0) {
      const equalWeight = 1.0 / state.selectedFactors.length
      setFactors(state.selectedFactors.map(f => ({
        factor_name: f,
        weight: equalWeight,
        direction: 'long' as const,
      })))
    }
  }, [location.state])
  const [winsorize, setWinsorize] = useState<[number, number]>([0.01, 0.99])
  const [fillNull, setFillNull] = useState('median')
  const [marketCapNeutralize, setMarketCapNeutralize] = useState(false)
  const [industryNeutralize, setIndustryNeutralize] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const navigate = useNavigate()

  const [page, setPage] = useState(0)
  const [selectedDate, setSelectedDate] = useState('')

  const { data: factorDefs } = useQuery({
    queryKey: ['factors', 'definitions'],
    queryFn: () => factorAnalyticsApi.definitions(),
    staleTime: 300_000,
  })

  const runMutation = useMutation({
    mutationFn: (body: Parameters<typeof scoringApi.run>[0]) => scoringApi.run(body),
    onSuccess: (data) => setActiveRunId(data.run_id),
  })

  const { data: result } = useQuery({
    queryKey: ['scoring', 'result', activeRunId, page, selectedDate],
    queryFn: () => scoringApi.getResult(activeRunId!, page * PAGE_SIZE, PAGE_SIZE, selectedDate),
    enabled: !!activeRunId,
    refetchInterval: (query: any) => {
      const status = query.state.data?.run?.status
      return status === 'completed' || status === 'error' ? false : 3000
    },
    placeholderData: keepPreviousData,
  })

  const { data: snapshots } = useQuery({
    queryKey: ['scoring', 'snapshots'],
    queryFn: () => scoringApi.listSnapshots(),
    staleTime: 30_000,
  })

  const addFactor = () => {
    setFactors([...factors, { factor_name: '', weight: 1.0, direction: 'long' }])
  }

  const removeFactor = (idx: number) => {
    setFactors(factors.filter((_, i) => i !== idx))
  }

  const updateFactor = (idx: number, field: keyof FactorWeightConfig, value: unknown) => {
    const updated = [...factors]
    updated[idx] = { ...updated[idx], [field]: value }
    setFactors(updated)
  }

  const handleRun = () => {
    runMutation.mutate({
      name,
      factors,
      feature_set_version: featureSetVersion,
      start_date: startDate,
      end_date: endDate,
      winsorize,
      fill_null: fillNull,
      neutralize: [
        ...(marketCapNeutralize ? ['market_cap'] : []),
        ...(industryNeutralize ? ['industry'] : []),
      ],
    }, {
      onError: (error) => toast.error(t('page.scoring.run_failed', { message: (error as Error).message })),
    })
  }

  function downloadCSV() {
    if (!result?.results?.length) return
    const esc = (v: unknown) => { const s = String(v ?? ''); return s.includes(',') ? `"${s}"` : s }
    const headers = ['trade_date', 'asset_id', 'score', 'rank']
    const rows = result.results.map(r =>
      [esc(r.trade_date), esc(r.asset_id), esc(r.score?.toFixed(6) ?? ''), esc(r.rank)].join(',')
    )
    const csv = [headers.join(','), ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `scoring_${activeRunId}_p${page + 1}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800">{t('page.scoring.title')}</h1>
        <div className="flex bg-gray-100 rounded-lg p-1">
          <button
            onClick={() => setActiveTab('current')}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors ${
              activeTab === 'current'
                ? 'bg-white text-gray-800 shadow-sm font-medium'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            {t('page.scoring.tab.current')}
          </button>
          <button
            onClick={() => setActiveTab('history')}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors ${
              activeTab === 'history'
                ? 'bg-white text-gray-800 shadow-sm font-medium'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            {t('page.scoring.tab.history')}
          </button>
        </div>
      </div>

      {activeTab === 'history' ? (
        <ScoreHistory />
      ) : (
        <>
          {/* 配置区 */}
      <div className="card">
        <h2 className="font-semibold text-gray-800 mb-4">{t('page.scoring.section.config')}</h2>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('page.scoring.label.plan_name')}</label>
            <input value={name} onChange={e => setName(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('page.scoring.label.feature_set')}</label>
            <input value={featureSetVersion} onChange={e => setFeatureSetVersion(e.target.value)}
              placeholder="silver_v3" className="w-full border rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('common.start_date')}</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('common.end_date')}</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm" />
          </div>
        </div>

        {/* 因子权重表 */}
        <div className="mt-4">
          <h3 className="text-sm font-medium text-gray-700 mb-2">{t('page.scoring.section.factor_weights')}</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 border-b">
                <th className="py-2">{t('page.scoring.column.factor')}</th>
                <th className="py-2 w-24">{t('page.scoring.column.weight')}</th>
                <th className="py-2 w-28">{t('page.scoring.column.direction')}</th>
                <th className="py-2 w-16">{t('page.scoring.column.action')}</th>
              </tr>
            </thead>
            <tbody>
              {factors.map((f, idx) => (
                <tr key={idx} className="border-b">
                  <td className="py-2">
                    <select value={f.factor_name} onChange={e => updateFactor(idx, 'factor_name', e.target.value)}
                      className="w-full border rounded px-2 py-1 text-sm">
                      <option value="">{t('page.scoring.label.select_factor')}</option>
                      {factorDefs?.items?.map(fd => (
                        <option key={fd.name} value={fd.name}>{fd.name} — {fd.description}</option>
                      ))}
                    </select>
                  </td>
                  <td className="py-2">
                    <input type="number" value={f.weight} step={0.1}
                      onChange={e => updateFactor(idx, 'weight', Number(e.target.value))}
                      className="w-full border rounded px-2 py-1 text-sm" />
                  </td>
                  <td className="py-2">
                    <select value={f.direction} onChange={e => updateFactor(idx, 'direction', e.target.value)}
                      className="w-full border rounded px-2 py-1 text-sm">
                      <option value="long">{t('page.scoring.option.direction_long')}</option>
                      <option value="short">{t('page.scoring.option.direction_short')}</option>
                    </select>
                  </td>
                  <td className="py-2">
                    <button onClick={() => removeFactor(idx)} className="text-red-500 hover:text-red-700 text-sm">
                      {t('common.delete')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button onClick={addFactor} className="mt-2 text-sm text-brand-600 hover:text-brand-700">
            {t('page.scoring.btn.add_factor')}
          </button>
        </div>

        {/* 高级配置 */}
        <details className="mt-4">
          <summary className="text-sm text-gray-600 cursor-pointer">{t('page.scoring.section.advanced')}</summary>
          <div className="grid grid-cols-3 gap-4 mt-2">
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.scoring.label.winsorize_lower')}</label>
              <input type="number" value={winsorize[0]} step={0.01} min={0} max={0.5}
                onChange={e => setWinsorize([Number(e.target.value), winsorize[1]])}
                className="w-full border rounded px-2 py-1 text-sm" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.scoring.label.winsorize_upper')}</label>
              <input type="number" value={winsorize[1]} step={0.01} min={0.5} max={1}
                onChange={e => setWinsorize([winsorize[0], Number(e.target.value)])}
                className="w-full border rounded px-2 py-1 text-sm" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.scoring.label.fill_null')}</label>
              <select value={fillNull} onChange={e => setFillNull(e.target.value)}
                className="w-full border rounded px-2 py-1 text-sm">
                <option value="median">{t('page.scoring.option.fill_median')}</option>
                <option value="mean">{t('page.scoring.option.fill_mean')}</option>
                <option value="zero">{t('page.scoring.option.fill_zero')}</option>
              </select>
            </div>
          </div>
          <div className="mt-4">
            <h4 className="text-xs text-gray-500 mb-2">{t('page.scoring.section.neutralize')}</h4>
            <div className="flex gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={marketCapNeutralize}
                  onChange={e => setMarketCapNeutralize(e.target.checked)}
                  className="rounded border-gray-300"
                />
                <span className="text-sm text-gray-700">{t('page.scoring.label.market_cap_neutralize')}</span>
                <span className="text-xs text-gray-400" title={t('page.scoring.label.market_cap_neutralize_hint')}>ⓘ</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={industryNeutralize}
                  onChange={e => setIndustryNeutralize(e.target.checked)}
                  className="rounded border-gray-300"
                />
                <span className="text-sm text-gray-700">{t('page.scoring.label.industry_neutralize')}</span>
                <span className="text-xs text-gray-400" title={t('page.scoring.label.industry_neutralize_hint')}>ⓘ</span>
              </label>
            </div>
          </div>
        </details>

        <button onClick={handleRun} disabled={runMutation.isPending}
          className="mt-4 px-4 py-2 bg-brand-600 text-white rounded-lg hover:bg-brand-700 disabled:opacity-50 text-sm">
          {runMutation.isPending ? t('page.scoring.btn.running') : t('page.scoring.btn.run')}
        </button>
      </div>

      {/* 结果区 */}
      {result && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-gray-800">
              {t('page.scoring.section.result')}
              <span className="ml-2 text-sm font-normal text-gray-500">
                {t('page.scoring.label.status')}: <span className={result.run.status === 'completed' ? 'text-green-600' : result.run.status === 'error' ? 'text-red-500' : 'text-yellow-600'}>
                  {result.run.status}
                </span>
              </span>
            </h2>
            <div className="flex items-center gap-2">
              {result.available_dates && result.available_dates.length > 0 && (
                <select
                  value={selectedDate}
                  onChange={e => { setSelectedDate(e.target.value); setPage(0) }}
                  className="text-xs border rounded px-2 py-1"
                >
                  <option value="">{t('page.scoring.option.all_dates')}</option>
                  {result.available_dates.map(d => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
              )}
              <button
                onClick={downloadCSV}
                disabled={!result.results?.length}
                className="btn-secondary text-xs disabled:opacity-40"
              >
                {t('page.scoring.btn.export_csv')}
              </button>
              {result?.run?.status === 'completed' && (
                <button
                  onClick={() => {
                    navigate('/strategies', {
                      state: {
                        openBacktest: true,
                        scoringRunId: activeRunId,
                        scoringDateRange: {
                          start: result.run.start_date,
                          end: result.run.end_date,
                        },
                        prefill: {
                          strategy_id: `scoring_${activeRunId?.slice(0, 8)}`,
                          config: JSON.stringify({ strategy_type: 'StaticTopN', top_n: 20 }, null, 2),
                        },
                      },
                    })
                  }}
                  className="btn-primary text-xs"
                >
                  {t('page.scoring.btn.run_backtest')}
                </button>
              )}
            </div>
          </div>

          {/* 得分分布直方图 */}
          {result.score_distribution && result.score_distribution.length > 0 && (
            <div className="mb-4">
              <h3 className="text-sm text-gray-600 mb-2">{t('page.scoring.section.distribution')}</h3>
              <ResponsiveContainer width="100%" height={100}>
                <BarChart data={result.score_distribution} margin={{ top: 0, right: 8, left: -30, bottom: 0 }}>
                  <XAxis dataKey="breakpoint" tick={{ fontSize: 10 }}
                    tickFormatter={(v: number) => v?.toFixed(2) ?? ''} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip
                    formatter={(v: number) => [v, t('page.scoring.distribution.asset_count')]}
                    labelFormatter={(l: number) => t('page.scoring.distribution.score_label', { score: l?.toFixed(2) })}
                  />
                  <Bar dataKey="count" fill="#3b82f6" radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {result.run.status === 'running' && (
            <div className="flex items-center gap-2 text-sm text-gray-500 py-4">
              <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              {t('page.scoring.computing')}
            </div>
          )}

          {result.results && result.results.length > 0 && (
            <>
              <DataTable
                data={result.results as unknown as Record<string, unknown>[]}
                columns={[
                  { key: 'trade_date', label: t('page.scoring.column.date'), sortable: true },
                  { key: 'asset_id', label: t('page.scoring.column.symbol'), searchable: true },
                  { key: 'score', label: t('page.scoring.column.score'), sortable: true, render: (v) => typeof v === 'number' ? v.toFixed(4) : String(v ?? '—') },
                  { key: 'rank', label: t('page.scoring.column.rank'), sortable: true },
                ]}
                rowKey={(r) => `${r.asset_id}_${r.trade_date}`}
                enableExport
                exportFilename={`scoring_${result.results[0]?.trade_date ?? 'results'}`}
              />

              {result.total > PAGE_SIZE && (
                <div className="flex items-center justify-between mt-3 text-sm text-gray-600">
                  <span>{t('page.scoring.pagination', { total: result.total, page: page + 1, totalPages: Math.ceil(result.total / PAGE_SIZE) })}</span>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setPage(p => Math.max(0, p - 1))}
                      disabled={page === 0}
                      className="btn-secondary text-xs disabled:opacity-40"
                    >
                      {t('page.scoring.btn.prev_page')}
                    </button>
                    <button
                      onClick={() => setPage(p => p + 1)}
                      disabled={(page + 1) * PAGE_SIZE >= result.total}
                      className="btn-secondary text-xs disabled:opacity-40"
                    >
                      {t('page.scoring.btn.next_page')}
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* 历史快照 */}
      {snapshots && snapshots.items.length > 0 && (
        <div className="card">
          <h2 className="font-semibold text-gray-800 mb-4">{t('page.scoring.section.snapshots')}</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 border-b">
                <th className="py-2">{t('page.scoring.label.name')}</th>
                <th className="py-2">{t('page.scoring.label.feature_set')}</th>
                <th className="py-2">{t('page.scoring.label.time_range')}</th>
                <th className="py-2">{t('page.scoring.label.status')}</th>
                <th className="py-2">{t('page.scoring.label.created_at')}</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.items.map(s => (
                <tr
                  key={s.run_id}
                  className={`border-b cursor-pointer transition-colors ${
                    activeRunId === s.run_id
                      ? 'bg-brand-50 border-l-4 border-l-brand-500'
                      : 'hover:bg-gray-50'
                  }`}
                  onClick={() => {
                    setActiveRunId(s.run_id)
                    setPage(0)
                    setSelectedDate('')
                  }}
                >
                  <td className="py-2">{s.config_name}</td>
                  <td className="py-2 font-mono text-xs">{s.feature_set_version}</td>
                  <td className="py-2 text-xs">{s.start_date} ~ {s.end_date}</td>
                  <td className="py-2">
                    <span className={`text-xs px-2 py-0.5 rounded ${
                      s.status === 'completed' ? 'bg-green-100 text-green-700' :
                      s.status === 'error' ? 'bg-red-100 text-red-700' :
                      'bg-yellow-100 text-yellow-700'
                    }`}>{s.status}</span>
                  </td>
                  <td className="py-2 text-xs text-gray-500">{s.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
        </>
      )}
    </div>
  )
}
