import { MetricCard } from '../../components/ui/MetricCard'
import { ROLLING_WINDOWS } from '@/lib/constants'
import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useParams, useNavigate } from 'react-router-dom'
import { backtestsApi, backtestExtApi, liveApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { toast } from 'sonner'
import { BenchmarkCompare } from '@/components/charts/BenchmarkCompare'
import { RollingMetricsChart } from '@/components/charts/RollingMetricsChart'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, CartesianGrid } from 'recharts'

import { downloadJson } from '@/lib/download'
import { SensitivityPanel } from '@/components/backtests/SensitivityPanel'
import { SensitivityChart } from '@/components/backtests/SensitivityChart'
import { ValidationPanel } from '@/components/backtests/ValidationPanel'

export function BacktestOverviewTab() {
  const { t } = useTranslation()

  function marketLabel(m?: string) {
    return { CN: t('page.market.cn'), US: t('page.market.us'), HK: t('page.market.hk') }[m ?? 'CN'] ?? t('page.market.cn')
  }
  function adjLabel(a?: string) {
    return { forward: t('page.market.adj_forward'), backward: t('page.market.adj_backward'), none: t('page.market.adj_none') }[a ?? 'forward'] ?? t('page.market.adj_forward')
  }
  function rebalanceLabel(r?: string) {
    return { '1d': t('page.market.daily'), '1w': t('page.market.weekly'), '1mo': t('page.market.monthly'), '5d': t('page.market.weekly'), '20d': t('page.market.monthly') }[r ?? '1d'] ?? r ?? t('page.market.daily')
  }
  const { id: selectedId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showDeployWizard, setShowDeployWizard] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [showSensitivity, setShowSensitivity] = useState(false)
  const [sensitivityResult, setSensitivityResult] = useState<any>(null)
  const [deployStep, setDeployStep] = useState(1)
  const [deployCash, setDeployCash] = useState('1000000')
  const [deployRiskMode, setDeployRiskMode] = useState<'conservative' | 'moderate' | 'aggressive'>('conservative')
  const [deployChecklist, setDeployChecklist] = useState({
    confirmBacktest: false,
    understandPaper: false,
    reviewRisk: false,
  })

  const deployMutation = useMutation({
    mutationFn: (body: Parameters<typeof liveApi.deploy>[0]) => liveApi.deploy(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['live', 'deployed'] })
      setShowDeployWizard(false)
      setDeployStep(1)
      setDeployChecklist({ confirmBacktest: false, understandPaper: false, reviewRisk: false })
      toast.success(t('component.backtest_overview.deploy.success_toast'))
    },
    onError: (e: Error) => toast.error(t('component.backtest_overview.deploy.failed_toast', { message: e.message })),
  })

  const { data: detail } = useQuery({
    queryKey: queryKeys.backtests.detail(selectedId!),
    queryFn: () => backtestsApi.get(selectedId!),
    enabled: !!selectedId,
  })

  const { data: analysisData } = useQuery({
    queryKey: queryKeys.backtests.analysis(selectedId!),
    queryFn: () => backtestsApi.getAnalysis(selectedId!),
    enabled: !!selectedId,
    staleTime: 60_000,
    retry: false,
  })

  const { data: riskData } = useQuery({
    queryKey: queryKeys.backtests.risk(selectedId!),
    queryFn: () => backtestsApi.getRisk(selectedId!, 20),
    enabled: !!selectedId,
    staleTime: 60_000,
  })

  const { data: tearsheetData } = useQuery({
    queryKey: queryKeys.backtests.tearsheet(selectedId!),
    queryFn: () => backtestExtApi.tearsheet(selectedId!),
    enabled: !!selectedId,
    staleTime: 60_000,
    retry: false,
  })

  const { data: drawdownTimeseriesData } = useQuery({
    queryKey: queryKeys.backtests.drawdownTimeseries(selectedId!),
    queryFn: () => backtestsApi.getDrawdownTimeseries(selectedId!),
    enabled: !!selectedId,
    staleTime: 60_000,
  })

  const analysis = analysisData as Record<string, unknown> | null | undefined
  const overfitScore = Number(analysis?.overall_overfit_score ?? 0)
  const psr = Number(analysis?.psr ?? 0)
  const dsr = Number(analysis?.dsr ?? 0)

  // Extract NAV data from tearsheet for benchmark comparison
  const tearsheet = tearsheetData as Record<string, unknown> | null | undefined
  const strategyNav = ((tearsheet?.snapshots as Record<string, unknown>[] ?? [])).map(s => ({
    date: String(s.trade_date ?? '').slice(0, 10),
    nav: Number(s.nav ?? 1),
  })).filter(d => d.date)
  const benchmarkNav = (tearsheet?.benchmark_nav as { date: string; nav: number }[] ?? [])
  const benchmarkLabel = String(tearsheet?.benchmark_asset_id ?? 'Benchmark')
  const rebalanceDates = (tearsheet?.rebalance_dates as string[] ?? [])

  // Net-of-fee NAV series (only present when a fee model was configured)
  const netNavSeries = (tearsheet?.net_nav as { date: string; nav: number }[] ?? [])

  // Transform drawdown timeseries data
  const drawdownChart = Array.isArray(drawdownTimeseriesData)
    ? (drawdownTimeseriesData as { date: string; drawdown: number }[]).map(d => ({
        date: d.date,
        drawdown: Number(d.drawdown) * 100, // Convert to percentage
      }))
    : []

  // Rolling metrics state
  const [rollingWindow, setRollingWindow] = useState(60)

  // Net-fee display mode: 'both' overlays gross + net; 'net' shows net only
  const [feeView, setFeeView] = useState<'both' | 'net'>('both')

  // Merged gross/net NAV series for the net-fee chart
  const netFeeChartData = useMemo(() => {
    if (netNavSeries.length === 0) return []
    // Gross NAV keyed by date for O(1) lookup
    const grossMap = new Map(strategyNav.map(d => [d.date, d.nav]))
    // Use net dates as the index (net is a subset when fee model present)
    return netNavSeries.map(d => ({
      date: d.date,
      gross: grossMap.get(d.date) ?? null,
      net: d.nav,
    }))
  }, [strategyNav, netNavSeries])

  // Compute rolling Sharpe and Volatility from strategy NAV
  const rollingOverviewData = useMemo(() => {
    if (strategyNav.length < 2) return []

    // Compute daily returns
    const returns: { date: string; ret: number }[] = []
    for (let i = 1; i < strategyNav.length; i++) {
      const ret = strategyNav[i].nav / strategyNav[i - 1].nav - 1
      returns.push({ date: strategyNav[i].date, ret })
    }

    if (returns.length < rollingWindow) return []

    // Rolling calculation helper
    const result: { date: string; values: Record<string, number> }[] = []
    for (let i = rollingWindow - 1; i < returns.length; i++) {
      const slice = returns.slice(i - rollingWindow + 1, i + 1)
      const rets = slice.map(r => r.ret)
      const mean = rets.reduce((a, b) => a + b, 0) / rets.length
      const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1))

      const rollingSharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0
      const rollingVol = std * Math.sqrt(252)

      result.push({
        date: returns[i].date,
        values: { sharpe: rollingSharpe, volatility: rollingVol },
      })
    }
    return result
  }, [strategyNav, rollingWindow])

  if (!selectedId) return null

  return (
    <div className="space-y-4">
      {/* Validation suite checklist (tri-state + cost assumptions) */}
      {detail?.status === 'completed' && <ValidationPanel runId={selectedId} />}

      {/* Export / Deploy / Sensitivity buttons */}
      <div className="flex justify-end gap-2">
        {/* Sensitivity analysis button */}
        {detail?.status === 'completed' && (
          <button
            onClick={() => setShowSensitivity(!showSensitivity)}
            className="btn-secondary text-xs flex items-center gap-1"
          >
            {showSensitivity ? t('component.backtest_overview.btn.sensitivity_close') : t('component.backtest_overview.btn.sensitivity_open')}
          </button>
        )}
        {/* Export dropdown */}
        <div className="relative" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setExportOpen(false) }} tabIndex={-1}>
          <button
            onClick={() => setExportOpen(!exportOpen)}
            className="btn-secondary text-xs flex items-center gap-1"
          >
            {t('component.backtest_overview.export.report')}
            <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {exportOpen && (
            <div className="absolute right-0 mt-1 w-40 bg-white border border-gray-200 rounded shadow-lg z-10">
              <a
                href={`/api/v1/backtests/${selectedId}/export?format=html`}
                target="_blank"
                rel="noopener noreferrer"
                className="block px-3 py-2 text-xs hover:bg-gray-50"
                onClick={() => setExportOpen(false)}
              >
                {t('component.backtest_overview.export.html')}
              </a>
              <div
                className="block w-full text-left px-3 py-2 text-xs text-gray-400 cursor-not-allowed"
                title={t('component.backtest_overview.export.pdf_note')}
              >
                {t('component.backtest_overview.export.pdf')}（{t('component.backtest_overview.export.pdf_unavailable')}）
              </div>
            </div>
          )}
        </div>
        {detail?.status === 'completed' && (
          <button
            onClick={() => setShowDeployWizard(true)}
            className="btn-primary text-xs flex items-center gap-1"
          >
            {t('component.backtest_overview.btn.deploy_paper')}
          </button>
        )}
        {detail?.status === 'completed' && (
          <button
            onClick={() => navigate('/factors', {
              state: {
                strategyContext: {
                  run_id: selectedId,
                  strategy_id: detail.strategy_id,
                  metrics: detail.metrics,
                },
              },
            })}
            className="btn-secondary text-xs flex items-center gap-1"
          >
            {t('component.backtest_overview.btn.optimize_weights')}
          </button>
        )}
      </div>

      {/* Sensitivity analysis panel */}
      {showSensitivity && selectedId && (
        <SensitivityPanel
          runId={selectedId}
          onComplete={(result) => setSensitivityResult(result)}
        />
      )}

      {/* Sensitivity chart */}
      {sensitivityResult?.metrics && (
        <SensitivityChart
          data={sensitivityResult.metrics.param_results || []}
          paramKey="param_value"
          metricKeys={['sharpe_ratio', 'total_return', 'max_drawdown']}
        />
      )}

      {/* Parameter summary */}
      {(() => {
        const ext = detail as unknown as Record<string, unknown> | undefined
        const sc = ext?.strategy_config as Record<string, unknown> | undefined
        if (!sc) return null
        const mr = sc.market_rule as Record<string, unknown> | undefined
        return (
          <div className="text-xs text-gray-400 flex flex-wrap gap-x-3 gap-y-1">
            <span>{t('component.backtest_overview.param.market')}: {marketLabel(mr?.market as string)}</span>
            <span>{t('component.backtest_overview.param.adj')}: {adjLabel(mr?.adj_type as string)}</span>
            <span>{t('component.backtest_overview.param.rebalance')}: {rebalanceLabel(sc.rebalance_frequency as string)}</span>
            <span>{t('component.backtest_overview.param.sizer')}: {String(sc.sizer ?? 'equal_weight')}</span>
          </div>
        )
      })()}

      {/* Metrics cards */}
      {detail?.metrics && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
            <MetricCard
              label={t("common.metric.total_return")}
              value={`${(detail.metrics.total_return * 100).toFixed(1)}%`}
              warn={detail.metrics.total_return < 0}
            />
            <MetricCard
              label={t("common.metric.annualized_return")}
              value={`${(detail.metrics.annualized_return * 100).toFixed(1)}%`}
              warn={detail.metrics.annualized_return < 0}
            />
            <MetricCard
              label={t("common.metric.sharpe_ratio")}
              value={Number(detail.metrics.sharpe_ratio ?? 0).toFixed(3)}
              warn={detail.metrics.sharpe_ratio < 0}
            />
            <MetricCard
              label={t("common.metric.max_drawdown")}
              value={`${(detail.metrics.max_drawdown * 100).toFixed(1)}%`}
              warn={detail.metrics.max_drawdown < -0.2}
            />
            <MetricCard
              label={t("common.metric.win_rate")}
              value={`${(detail.metrics.win_rate * 100).toFixed(1)}%`}
            />
            <MetricCard
              label={t("component.backtest_overview.metric.total_trades")}
              value={String(detail.metrics.total_trades)}
            />
          </div>

          {/* Active management metrics */}
          {(
            detail.metrics.information_ratio != null ||
            detail.metrics.omega_ratio != null ||
            detail.metrics.tail_ratio != null
          ) && (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
              <MetricCard
                label={t("common.metric.information_ratio")}
                value={detail.metrics.information_ratio != null
                  ? detail.metrics.information_ratio.toFixed(3)
                  : '—'}
                sub={detail.metrics.information_ratio == null
                  ? t('page.backtest.no_benchmark_hint')
                  : t("component.backtest_overview.metric.ir_needs_benchmark")}
                warn={detail.metrics.information_ratio != null && detail.metrics.information_ratio < 0}
              />
              <MetricCard
                label={t("common.metric.tracking_error")}
                value={detail.metrics.tracking_error != null
                  ? `${(detail.metrics.tracking_error * 100).toFixed(2)}%`
                  : '—'}
                sub={detail.metrics.tracking_error == null
                  ? t('page.backtest.no_benchmark_hint')
                  : t("component.backtest_overview.metric.te_annualized")}
              />
              <MetricCard
                label={t("common.metric.alpha")}
                value={detail.metrics.alpha != null
                  ? `${(detail.metrics.alpha * 100).toFixed(2)}%`
                  : '—'}
                sub={detail.metrics.alpha == null
                  ? t('page.backtest.no_benchmark_hint')
                  : t('component.backtest_overview.metric.jensen_alpha')}
                warn={detail.metrics.alpha != null && detail.metrics.alpha < 0}
              />
              <MetricCard
                label={t("component.backtest_overview.metric.omega_ratio")}
                value={detail.metrics.omega_ratio != null
                  ? detail.metrics.omega_ratio.toFixed(3)
                  : '—'}
                warn={detail.metrics.omega_ratio != null && detail.metrics.omega_ratio < 1}
              />
              <MetricCard
                label={t("component.backtest_overview.metric.tail_ratio")}
                value={detail.metrics.tail_ratio != null
                  ? detail.metrics.tail_ratio.toFixed(3)
                  : '—'}
              />
              <MetricCard
                label={t("component.backtest_overview.metric.turnover")}
                value={detail.metrics.turnover_pct != null
                  ? `${(detail.metrics.turnover_pct * 100).toFixed(1)}%`
                  : '—'}
                sub={t("component.backtest_overview.metric.avg_single_side")}
              />
            </div>
          )}

          {/* Export metrics button */}
          <div className="flex justify-end">
            <button
              className="btn-secondary text-sm"
              onClick={() => downloadJson(
                detail.metrics,
                `metrics_${selectedId?.slice(0, 8) ?? 'backtest'}.json`,
              )}
            >
              {t('component.backtest_overview.btn.export_metrics_json')}
            </button>
          </div>
        </>
      )}

      {/* Risk snapshot */}
      {riskData?.items?.[0] && (
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-3">{t('component.backtest_overview.section.latest_risk_snapshot')}</h3>
          <div className="grid grid-cols-4 gap-4">
            <MetricCard label={t('component.backtest_overview.metric.drawdown')} value={`${((riskData.items[0].drawdown as number ?? 0) * 100).toFixed(2)}%`} warn />
            <MetricCard label={t('component.backtest_overview.metric.leverage')} value={(riskData.items[0].gross_leverage as number ?? 0).toFixed(2)} />
            <MetricCard label={t('component.backtest_overview.metric.var_95')} value={`${((riskData.items[0].var_95 as number ?? 0) * 100).toFixed(2)}%`} />
            <MetricCard label={t("common.metric.beta")} value={(riskData.items[0].beta as number ?? 0).toFixed(2)} />
          </div>
        </div>
      )}

      {/* Analysis summary */}
      {analysis && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <MetricCard label={t('component.backtest_overview.metric.psr')} value={psr.toFixed(3)} sub={t("component.backtest_overview.metric.probabilistic_sharpe")} warn={psr < 0.5} />
            <MetricCard label={t('component.backtest_overview.metric.dsr')} value={dsr.toFixed(3)} sub={t("component.backtest_overview.metric.deflated_sharpe")} warn={dsr < 0.5} />
            <MetricCard label={t('component.backtest_overview.metric.summary')} value={String(analysis.summary ?? '').slice(0, 60) || '—'} sub="" />
            <MetricCard label={t('component.backtest_overview.metric.overfit_risk')} value={`${Math.round(overfitScore * 100)}%`} warn={overfitScore > 0.5} />
          </div>
          <div className="card text-sm text-gray-700">
            <div className="font-medium mb-1">{t('component.backtest_overview.section.analysis_summary')}</div>
            <p className="text-gray-600">{String(analysis.summary ?? t('component.backtest_overview.empty.no_analysis'))}</p>
          </div>
        </div>
      )}

      {/* Drawdown timeseries chart */}
      {drawdownChart.length > 0 && (
        <div className="bg-white rounded-xl shadow-sm border p-4">
          <h3 className="text-sm font-semibold text-gray-700 mb-3">{t('component.backtest_overview.section.drawdown_curve')}</h3>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={drawdownChart} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10, fill: '#94a3b8' }}
                  tickFormatter={(v: string) => v.slice(5)}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fontSize: 10, fill: '#94a3b8' }}
                  tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                  domain={['dataMin', 0]}
                />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }}
                  formatter={(value: number) => [`${value.toFixed(2)}%`, t('component.backtest_overview.series.drawdown')]}
                  labelFormatter={(label: string) => `${t('component.backtest_overview.label.date')}: ${label}`}
                />
                <ReferenceLine y={0} stroke="#cbd5e1" strokeWidth={1} />
                <Area
                  type="monotone"
                  dataKey="drawdown"
                  stroke="#ef4444"
                  fill="#ef4444"
                  fillOpacity={0.3}
                  strokeWidth={1.5}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Net-of-fee comparison (gross vs net NAV) */}
      {netFeeChartData.length > 0 && (
        <div className="bg-white rounded-xl shadow-sm border p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-700">{t('component.backtest_overview.section.net_fee_curve')}</h3>
            <div className="flex gap-1 text-xs">
              <button
                onClick={() => setFeeView('both')}
                className={`px-2 py-1 rounded ${feeView === 'both' ? 'bg-brand-600 text-white' : 'bg-gray-100 text-gray-600'}`}
              >
                {t('component.backtest_overview.series.gross')}/{t('component.backtest_overview.series.net')}
              </button>
              <button
                onClick={() => setFeeView('net')}
                className={`px-2 py-1 rounded ${feeView === 'net' ? 'bg-brand-600 text-white' : 'bg-gray-100 text-gray-600'}`}
              >
                {t('component.backtest_overview.series.net')}
              </button>
            </div>
          </div>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={netFeeChartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10, fill: '#94a3b8' }}
                  tickFormatter={(v: string) => v.slice(5)}
                  interval="preserveStartEnd"
                />
                <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }}
                  formatter={(value: number, name: string) => [
                    Number(value).toFixed(4),
                    name === 'gross' ? t('component.backtest_overview.series.gross_nav') : t('component.backtest_overview.series.net_nav'),
                  ]}
                  labelFormatter={(label: string) => `${t('component.backtest_overview.label.date')}: ${label}`}
                />
                <ReferenceLine y={1} stroke="#cbd5e1" strokeWidth={1} />
                {feeView === 'both' && (
                  <Area
                    type="monotone"
                    dataKey="gross"
                    stroke="#3b82f6"
                    fill="#3b82f6"
                    fillOpacity={0.1}
                    strokeWidth={1.5}
                  />
                )}
                <Area
                  type="monotone"
                  dataKey="net"
                  stroke="#ef4444"
                  fill="#ef4444"
                  fillOpacity={0.1}
                  strokeWidth={1.5}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Rolling Sharpe / Volatility */}
      {rollingOverviewData.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-end">
            <label className="text-xs text-gray-500 mr-2">{t('component.backtest_overview.label.rolling_window')}</label>
            <select
              value={rollingWindow}
              onChange={e => setRollingWindow(Number(e.target.value))}
              className="text-xs border border-gray-200 rounded px-2 py-1 bg-white text-gray-700 focus:outline-none focus:ring-1 focus:ring-brand-400"
            >
              {ROLLING_WINDOWS.map(w => <option key={w} value={w}>{w}d</option>)}
            </select>
          </div>
          <RollingMetricsChart
            data={rollingOverviewData}
            metrics={[
              { key: 'sharpe', label: t('component.backtest_overview.series.rolling_sharpe'), color: '#3b82f6' },
              { key: 'volatility', label: t('component.backtest_overview.series.rolling_volatility'), color: '#f59e0b' },
            ]}
            window={rollingWindow}
            title={t('component.backtest_overview.section.rolling_metrics')}
            referenceLines={[
              { value: 0, label: t('component.backtest_overview.label.zero'), color: '#e5e7eb' },
            ]}
          />
        </div>
      )}

      {/* Benchmark comparison */}
      {strategyNav.length > 0 && benchmarkNav.length > 0 && (
        <BenchmarkCompare
          strategyNav={strategyNav}
          benchmarkNav={benchmarkNav}
          benchmarkLabel={benchmarkLabel}
          rebalanceDates={rebalanceDates}
        />
      )}

      {!detail?.metrics && !analysis && (
        <div className="card text-center text-gray-400 py-12">
          <div className="text-4xl mb-3">{t('component.backtest_overview.empty.chart_icon')}</div>
          <div>{t('component.backtest_overview.empty.select_completed')}</div>
        </div>
      )}

      {/* Deploy Wizard Modal */}
      {showDeployWizard && selectedId && detail && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md">
            <div className="flex items-center justify-between p-4 border-b">
              <h2 className="font-semibold text-gray-900">{t('component.backtest_overview.deploy.modal_title')}</h2>
              <div className="flex gap-1">
                {[1, 2, 3].map(s => (
                  <span key={s} className={`w-6 h-6 rounded-full text-xs flex items-center justify-center ${
                    s === deployStep ? 'bg-brand-600 text-white' :
                    s < deployStep ? 'bg-green-500 text-white' : 'bg-gray-200 text-gray-500'
                  }`}>{s < deployStep ? '✓' : s}</span>
                ))}
              </div>
            </div>

            <div className="p-4">
              {deployStep === 1 && (
                <div className="space-y-3">
                  <h3 className="font-medium text-gray-800">{t('component.backtest_overview.deploy.step1_title')}</h3>
                  <div className="p-3 bg-gray-50 rounded-lg text-sm space-y-1">
                    <div><span className="text-gray-500">{t('component.backtest_overview.deploy.strategy')} </span><strong>{detail.strategy_id}</strong></div>
                    <div><span className="text-gray-500">{t('component.backtest_overview.deploy.run_id')} </span><span className="font-mono text-xs">{selectedId.slice(0, 16)}...</span></div>
                    <div><span className="text-gray-500">{t('component.backtest_overview.deploy.dataset')} </span>{detail.dataset_version}</div>
                    {detail.metrics?.sharpe_ratio != null && (
                      <div><span className="text-gray-500">{t('component.backtest_overview.deploy.sharpe')} </span><strong className="text-brand-600">{Number(detail.metrics.sharpe_ratio).toFixed(3)}</strong></div>
                    )}
                  </div>
                </div>
              )}

              {deployStep === 2 && (
                <div className="space-y-3">
                  <h3 className="font-medium text-gray-800">{t('component.backtest_overview.deploy.step2_title')}</h3>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_overview.deploy.initial_capital')}</label>
                    <input type="number" value={deployCash} onChange={e => setDeployCash(e.target.value)}
                      className="input w-full" min={10000} step={10000} />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_overview.deploy.risk_mode')}</label>
                    <select value={deployRiskMode} onChange={e => setDeployRiskMode(e.target.value as typeof deployRiskMode)}
                      className="input w-full">
                      <option value="conservative">{t('component.backtest_overview.deploy.mode_conservative')}</option>
                      <option value="moderate">{t('component.backtest_overview.deploy.mode_moderate')}</option>
                      <option value="aggressive">{t('component.backtest_overview.deploy.mode_aggressive')}</option>
                    </select>
                  </div>
                  <div className="p-3 bg-gray-50 rounded-lg text-xs space-y-1 text-gray-600">
                    <div className="font-medium text-gray-700 mb-1">{t('component.backtest_overview.deploy.risk_preview')}</div>
                    {deployRiskMode === 'conservative' && (
                      <>
                        <div>- {t('component.backtest_overview.deploy.per_trade_stop_loss')}: 5%</div>
                        <div>- {t('component.backtest_overview.deploy.max_drawdown_label')}: 10% ({t('component.backtest_overview.deploy.pauses_strategy')})</div>
                        <div>- {t('component.backtest_overview.deploy.single_stock_limit')}: 10% NAV</div>
                      </>
                    )}
                    {deployRiskMode === 'moderate' && (
                      <>
                        <div>- {t('component.backtest_overview.deploy.per_trade_stop_loss')}: 8%</div>
                        <div>- {t('component.backtest_overview.deploy.max_drawdown_label')}: 15% ({t('component.backtest_overview.deploy.pauses_strategy')})</div>
                        <div>- {t('component.backtest_overview.deploy.single_stock_limit')}: 15% NAV</div>
                      </>
                    )}
                    {deployRiskMode === 'aggressive' && (
                      <>
                        <div>- {t('component.backtest_overview.deploy.per_trade_stop_loss')}: 15%</div>
                        <div>- {t('component.backtest_overview.deploy.max_drawdown_label')}: 25% ({t('component.backtest_overview.deploy.pauses_strategy')})</div>
                        <div>- {t('component.backtest_overview.deploy.single_stock_limit')}: 20% NAV</div>
                      </>
                    )}
                  </div>
                </div>
              )}

              {deployStep === 3 && (
                <div className="space-y-3">
                  <h3 className="font-medium text-gray-800">{t('component.backtest_overview.deploy.step3_title')}</h3>
                  <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg text-sm space-y-1">
                    <div>{t('component.backtest_overview.deploy.strategy')}: <strong>{detail.strategy_id}</strong></div>
                    <div>{t('component.backtest_overview.deploy.initial_capital')}: <strong>{Number(deployCash).toLocaleString()}</strong></div>
                    <div>{t('component.backtest_overview.deploy.risk_mode')}: <strong>{deployRiskMode}</strong></div>
                  </div>

                  <div className="space-y-2">
                    <label className="flex items-start gap-2 text-xs text-gray-600 cursor-pointer">
                      <input type="checkbox" checked={deployChecklist.confirmBacktest}
                        onChange={e => setDeployChecklist(c => ({ ...c, confirmBacktest: e.target.checked }))}
                        className="mt-0.5 w-4 h-4 rounded border-gray-300 accent-brand-600" />
                      <span>{t('component.backtest_overview.deploy.confirm_backtest', { sharpe: Number(detail.metrics?.sharpe_ratio ?? 0).toFixed(3), maxDD: ((detail.metrics?.max_drawdown ?? 0) * 100).toFixed(1) })}</span>
                    </label>
                    <label className="flex items-start gap-2 text-xs text-gray-600 cursor-pointer">
                      <input type="checkbox" checked={deployChecklist.understandPaper}
                        onChange={e => setDeployChecklist(c => ({ ...c, understandPaper: e.target.checked }))}
                        className="mt-0.5 w-4 h-4 rounded border-gray-300 accent-brand-600" />
                      <span>{t('component.backtest_overview.deploy.confirm_paper')}</span>
                    </label>
                    <label className="flex items-start gap-2 text-xs text-gray-600 cursor-pointer">
                      <input type="checkbox" checked={deployChecklist.reviewRisk}
                        onChange={e => setDeployChecklist(c => ({ ...c, reviewRisk: e.target.checked }))}
                        className="mt-0.5 w-4 h-4 rounded border-gray-300 accent-brand-600" />
                      <span>{t('component.backtest_overview.deploy.confirm_risk')}</span>
                    </label>
                  </div>

                  <p className="text-xs text-gray-500">
                    {t('component.backtest_overview.deploy.after_hint')}
                  </p>
                </div>
              )}
            </div>

            <div className="flex justify-between p-4 border-t">
              <button
                onClick={() => {
                  if (deployStep > 1) {
                    setDeployStep(s => s - 1)
                  } else {
                    setShowDeployWizard(false)
                    setDeployStep(1)
                    setDeployCash('1000000')
                    setDeployRiskMode('conservative')
                    setDeployChecklist({ confirmBacktest: false, understandPaper: false, reviewRisk: false })
                  }
                }}
                className="btn-secondary text-sm"
              >
                {deployStep === 1 ? t('common.cancel') : t('component.backtest_overview.deploy.back')}
              </button>
              {deployStep < 3 ? (
                <button onClick={() => setDeployStep(s => s + 1)} className="btn-primary text-sm">
                  {t('component.backtest_overview.deploy.next')}
                </button>
              ) : (
                <button
                  onClick={() => deployMutation.mutate({
                    backtest_run_id: selectedId!,
                    initial_cash: Number(deployCash),
                    risk_mode: deployRiskMode,
                  })}
                  disabled={deployMutation.isPending || !deployChecklist.confirmBacktest || !deployChecklist.understandPaper || !deployChecklist.reviewRisk}
                  className="btn-primary text-sm disabled:opacity-40"
                >
                  {deployMutation.isPending ? t('component.backtest_overview.deploy.deploying') : t('component.backtest_overview.deploy.confirm_deployment')}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
