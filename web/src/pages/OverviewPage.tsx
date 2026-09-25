import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { datasetsApi, backtestsApi, knowledgeApi, liveApi, dashboardApi, realtimeApi, alertsApi } from '@/lib/api'
import { queryKeys, extendedQueryKeys } from '@/lib/queryKeys'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { formatApiDateTime } from '@/lib/utils'
import { SparkLine } from '@/components/ui/SparkLine'

function ErrorCard({ titleKey, error }: { titleKey: string; error: Error | null }) {
  const { t } = useTranslation()
  return (
    <div className="card border-l-4 border-red-400">
      <h3 className="text-sm font-semibold text-gray-700 mb-1">{t(titleKey)}</h3>
      <p className="text-xs text-red-500">{error?.message ?? t('page.overview.unknown_error')}</p>
    </div>
  )
}

interface QuickLinkProps { to: string; icon: string; label: string; desc: string }
function QuickLink({ to, icon, label, desc }: QuickLinkProps) {
  return (
    <Link to={to} className="card hover:shadow-md transition-shadow group flex items-start gap-3 no-underline">
      <span className="text-2xl">{icon}</span>
      <div>
        <div className="font-semibold text-gray-900 group-hover:text-brand-600 transition-colors">{label}</div>
        <div className="text-xs text-gray-400 mt-0.5">{desc}</div>
      </div>
    </Link>
  )
}

interface StatCardProps { label: string; value: string | number; icon: string; delta?: string; warn?: boolean; sparkData?: number[] }
function StatCard({ label, value, icon, delta, warn, sparkData }: StatCardProps) {
  return (
    <div className={`card ${warn ? 'border-l-4 border-amber-400' : ''}`}>
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="text-3xl font-bold text-brand-600">{value}</div>
          <div className="text-sm text-gray-500 mt-1">{label}</div>
          {delta && <div className="text-xs text-green-600 mt-0.5">{delta}</div>}
        </div>
        <span className="text-2xl opacity-60">{icon}</span>
      </div>
      {sparkData && sparkData.length > 0 && (
        <div className="mt-2">
          <SparkLine data={sparkData} height={32} />
        </div>
      )}
    </div>
  )
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: 'bg-red-100 text-red-800',
  high: 'bg-orange-100 text-orange-800',
  medium: 'bg-yellow-100 text-yellow-800',
  low: 'bg-blue-100 text-blue-800',
  info: 'bg-gray-100 text-gray-800',
}

export function OverviewPage() {
  const { t } = useTranslation()
  const { data: datasets, error: datasetsError, status: datasetsStatus } = useQuery({
    queryKey: queryKeys.datasets.list(5),
    queryFn: () => datasetsApi.list(5),
  })
  const { data: backtests, error: backtestsError, status: backtestsStatus } = useQuery({
    queryKey: queryKeys.backtests.list({ limit: 5 }),
    queryFn: () => backtestsApi.list({ limit: 5 }),
  })
  const { data: knowledgeDocs, error: knowledgeError, status: knowledgeStatus } = useQuery({
    queryKey: queryKeys.knowledge.list(),
    queryFn: () => knowledgeApi.list(),
  })
  const { data: liveStrategies, error: liveError, status: liveStatus } = useQuery({
    queryKey: extendedQueryKeys.live.strategies(),
    queryFn: liveApi.strategies,
  })

  const { data: freshness, error: freshnessError, status: freshnessStatus } = useQuery({
    queryKey: ['dashboard', 'freshness'],
    queryFn: datasetsApi.freshness,
    staleTime: 300_000,
  })

  const { data: bestBt, error: bestBtError, status: bestBtStatus } = useQuery({
    queryKey: ['dashboard', 'best-recent'],
    queryFn: () => dashboardApi.bestRecent(7),
    staleTime: 60_000,
  })

  const { data: icBoard, error: icBoardError, status: icBoardStatus } = useQuery({
    queryKey: ['dashboard', 'ic-leaderboard'],
    queryFn: () => dashboardApi.icLeaderboard(5),
    staleTime: 120_000,
  })

  const { data: backtestTrend, error: backtestTrendError, status: backtestTrendStatus } = useQuery({
    queryKey: extendedQueryKeys.dashboard.backtestTrend(30),
    queryFn: () => dashboardApi.backtestTrend(30),
    staleTime: 300_000,
  })

  const { data: icTrend, error: icTrendError, status: icTrendStatus } = useQuery({
    queryKey: extendedQueryKeys.dashboard.icTrend(30),
    queryFn: () => dashboardApi.icTrend(30),
    staleTime: 300_000,
  })

  const { data: recentAlerts, error: alertsError, status: alertsStatus } = useQuery({
    queryKey: ['alerts', 'recent'],
    queryFn: () => alertsApi.history(false, 5),
    staleTime: 60_000,
  })

  const { data: marketQuotes, error: marketError, status: marketStatus } = useQuery({
    queryKey: extendedQueryKeys.realtime.quotes(['sh000001', 'sz399001', 'sz399006']),
    queryFn: () => realtimeApi.quotes(['sh000001', 'sz399001', 'sz399006']),
    staleTime: 30_000,
    retry: false,
  })

  const completedRuns = backtests?.items.filter(r => r.status === 'completed').length ?? 0
  const runningStrategies = liveStrategies?.items.length ?? 0

  const btSparkData = backtestTrend?.items.map(i => i.count) ?? []
  const icSparkData = icTrend?.items.map(i => i.avg_ic) ?? []

  const marketItems = marketQuotes?.items ?? {}

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">{t('page.overview.title')}</h1>
        <p className="text-sm text-gray-500 mt-1">{t('page.overview.subtitle')}</p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {datasetsStatus === 'error' ? (
          <ErrorCard titleKey="page.overview.metric.datasets" error={datasetsError} />
        ) : (
          <StatCard label={t('page.overview.metric.datasets')} value={datasets?.total ?? '—'} icon="🗄️" />
        )}
        {backtestsStatus === 'error' ? (
          <ErrorCard titleKey="page.overview.metric.backtests" error={backtestsError} />
        ) : (
          <StatCard label={t('page.overview.metric.backtests')} value={backtests?.total ?? '—'} icon="📊"
            delta={completedRuns > 0 ? t('page.overview.metric.completed_delta', { count: completedRuns }) : undefined}
            sparkData={btSparkData} />
        )}
        {knowledgeStatus === 'error' ? (
          <ErrorCard titleKey="page.overview.metric.knowledge_docs" error={knowledgeError} />
        ) : (
          <StatCard label={t('page.overview.metric.knowledge_docs')} value={knowledgeDocs?.total ?? '—'} icon="📚" />
        )}
        {liveStatus === 'error' ? (
          <ErrorCard titleKey="page.overview.metric.active_strategies" error={liveError} />
        ) : (
          <StatCard label={t('page.overview.metric.active_strategies')} value={runningStrategies} icon="⚡"
            warn={runningStrategies === 0} />
        )}
      </div>

      {/* 增强行：数据新鲜度 + 最优回测 + IC 排行 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

        {/* 数据新鲜度 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.data_freshness')}</h3>
          {freshnessStatus === 'error' ? (
            <p className="text-xs text-red-500">{(freshnessError as Error).message}</p>
          ) : freshness ? (
            <>
              <p className="text-2xl font-bold text-gray-800">
                {freshness.last_updated ?? '—'}
              </p>
              {freshness.days_stale >= 0 && (
                <p className={`text-xs mt-1 ${freshness.days_stale > 3 ? 'text-amber-600' : 'text-green-600'}`}>
                  {freshness.days_stale === 0 ? t('page.overview.freshness.today_updated') : t('page.overview.freshness.stale', { days: freshness.days_stale })}
                </p>
              )}
            </>
          ) : (
            <p className="text-gray-400 text-sm">{t('common.loading')}</p>
          )}
        </div>

        {/* 近7天最优回测 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.best_recent')}</h3>
          {bestBtStatus === 'error' ? (
            <p className="text-xs text-red-500">{(bestBtError as Error).message}</p>
          ) : bestBt?.run_id ? (
            <>
              <p className="font-semibold text-gray-800 truncate" title={bestBt.strategy_id ?? ''}>
                {bestBt.strategy_id}
              </p>
              <div className="flex gap-3 mt-1 text-xs">
                <span>{t('page.overview.best_recent.sharpe')} <strong className="text-brand-600">{bestBt.sharpe}</strong></span>
                <span>{t('page.overview.best_recent.maxdd')} <strong className="text-red-500">{bestBt.max_drawdown}%</strong></span>
                {bestBt.cagr != null && <span>{t('page.overview.best_recent.cagr')} <strong className="text-green-600">{bestBt.cagr}%</strong></span>}
              </div>
              <Link to={`/backtests?run_id=${bestBt.run_id}`} className="text-xs text-brand-600 hover:underline mt-2 block">
                {t('page.overview.best_recent.view_detail')}
              </Link>
            </>
          ) : (
            <p className="text-gray-400 text-sm">{t('page.overview.best_recent.empty')}</p>
          )}
        </div>

        {/* Top5 因子 IC */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.ic_leaderboard')}</h3>
          {icBoardStatus === 'error' ? (
            <p className="text-xs text-red-500">{(icBoardError as Error).message}</p>
          ) : icBoard?.items.length ? (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500">
                  <th className="text-left py-0.5">{t('page.overview.ic.column.rank')}</th>
                  <th className="text-left py-0.5">{t('page.overview.ic.column.factor')}</th>
                  <th className="text-right py-0.5">{t('page.overview.ic.column.ic')}</th>
                  <th className="text-right py-0.5">{t('page.overview.ic.column.ir')}</th>
                  <th className="text-right py-0.5">{t('common.metric.win_rate')}</th>
                </tr>
              </thead>
              <tbody>
                {icBoard.items.map((f, i) => (
                  <tr key={f.factor_name} className="border-t border-gray-100">
                    <td className="py-1 text-gray-400">{i + 1}</td>
                    <td className="py-1 font-mono truncate max-w-[100px]" title={f.factor_name}>
                      {f.factor_name}
                    </td>
                    <td className={`py-1 text-right font-bold ${f.mean_ic > 0 ? 'text-green-600' : 'text-red-500'}`}>
                      {f.mean_ic.toFixed(4)}
                    </td>
                    <td className="py-1 text-right text-gray-600">{f.ir?.toFixed(2) ?? '—'}</td>
                    <td className="py-1 text-right text-gray-600">
                      {f.hit_rate != null ? `${(f.hit_rate * 100).toFixed(0)}%` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-gray-400 text-sm">{t('page.overview.ic.empty')}</p>
          )}
        </div>
      </div>

      {/* 增强行2：IC趋势 + 回测趋势 + 市场行情 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

        {/* IC 趋势 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.ic_trend')}</h3>
          {icTrendStatus === 'error' ? (
            <p className="text-xs text-red-500">{(icTrendError as Error).message}</p>
          ) : icSparkData.length > 0 ? (
            <SparkLine data={icSparkData} color="#10b981" height={48} />
          ) : (
            <p className="text-gray-400 text-sm">{t('page.overview.trend.empty')}</p>
          )}
        </div>

        {/* 回测趋势 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.backtest_trend')}</h3>
          {backtestTrendStatus === 'error' ? (
            <p className="text-xs text-red-500">{(backtestTrendError as Error).message}</p>
          ) : btSparkData.length > 0 ? (
            <SparkLine data={btSparkData} color="#6366f1" height={48} />
          ) : (
            <p className="text-gray-400 text-sm">{t('page.overview.trend.empty')}</p>
          )}
        </div>

        {/* 市场行情 */}
        <div className="card">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">{t('page.overview.section.market')}</h3>
          {marketStatus === 'error' ? (
            <p className="text-xs text-red-500">{(marketError as Error).message}</p>
          ) : Object.keys(marketItems).length > 0 ? (
            <div className="space-y-2">
              {[
                { symbol: 'sh000001', name: t('page.overview.market.sh_index') },
                { symbol: 'sz399001', name: t('page.overview.market.sz_index') },
                { symbol: 'sz399006', name: t('page.overview.market.gem_index') },
              ].map(({ symbol, name }) => {
                const q = marketItems[symbol]
                if (!q) return (
                  <div key={symbol} className="flex items-center justify-between text-xs">
                    <span className="text-gray-500">{name}</span>
                    <span className="text-gray-400">—</span>
                  </div>
                )
                const isUp = q.change_pct >= 0
                return (
                  <div key={symbol} className="flex items-center justify-between text-xs">
                    <span className="text-gray-500">{name}</span>
                    <div className="flex items-center gap-2">
                      <span className="font-mono">{q.price.toFixed(2)}</span>
                      <span className={isUp ? 'text-red-600' : 'text-green-600'}>
                        {isUp ? '+' : ''}{q.change_pct.toFixed(2)}%
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            <p className="text-gray-400 text-sm">{t('page.overview.market.loading')}</p>
          )}
        </div>
      </div>

      {/* 最近告警 */}
      {alertsStatus === 'error' ? (
        <ErrorCard titleKey="page.overview.section.recent_alerts" error={alertsError} />
      ) : (recentAlerts?.items.length ?? 0) > 0 && (
        <div className="card">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-700">{t('page.overview.section.recent_alerts')}</h3>
            <Link to="/alerts" className="text-xs text-brand-600 hover:underline">{t('page.overview.view_all')}</Link>
          </div>
          <ul className="divide-y divide-gray-100">
            {recentAlerts?.items.slice(0, 5).map(alert => (
              <li key={alert.alert_id} className="py-2 flex items-start gap-2">
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${SEVERITY_COLORS[alert.severity] ?? 'bg-gray-100 text-gray-800'}`}>
                  {alert.severity}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-gray-800 truncate">{alert.message}</p>
                  <p className="text-[10px] text-gray-400 mt-0.5">{alert.triggered_at?.slice(0, 16)}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Quick links */}
      <div>
        <h2 className="text-base font-semibold text-gray-700 mb-3">{t('page.overview.section.quick_links')}</h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <QuickLink to="/factors"    icon="🔬" label={t('page.overview.quick_link.factors.label')}    desc={t('page.overview.quick_link.factors.desc')} />
          <QuickLink to="/strategies" icon="⚙️" label={t('page.overview.quick_link.strategies.label')} desc={t('page.overview.quick_link.strategies.desc')} />
          <QuickLink to="/backtests"  icon="📈" label={t('page.overview.quick_link.backtests.label')}  desc={t('page.overview.quick_link.backtests.desc')} />
          <QuickLink to="/advisor"    icon="🤖" label={t('page.overview.quick_link.advisor.label')}    desc={t('page.overview.quick_link.advisor.desc')} />
          <QuickLink to="/ml"         icon="🧠" label={t('page.overview.quick_link.ml.label')}         desc={t('page.overview.quick_link.ml.desc')} />
          <QuickLink to="/knowledge"  icon="📄" label={t('page.overview.quick_link.knowledge.label')}  desc={t('page.overview.quick_link.knowledge.desc')} />
          <QuickLink to="/news"       icon="📰" label={t('page.overview.quick_link.news.label')}       desc={t('page.overview.quick_link.news.desc')} />
          <QuickLink to="/live"       icon="📡" label={t('page.overview.quick_link.live.label')}       desc={t('page.overview.quick_link.live.desc')} />
          <QuickLink to="/welcome"    icon="🚀" label={t('page.overview.quick_link.welcome.label')}   desc={t('page.overview.quick_link.welcome.desc')} />
        </div>
      </div>

      {/* Recent backtests */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold text-gray-700">{t('page.overview.section.recent_backtests')}</h2>
          <Link to="/backtests" className="text-xs text-brand-600 hover:underline">{t('page.overview.view_all')}</Link>
        </div>
        <div className="card p-0 overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                {[
                  t('page.overview.column.run_id'),
                  t('page.overview.column.strategy'),
                  t('page.overview.column.engine'),
                  t('page.overview.column.status'),
                  t('page.overview.column.started_at'),
                ].map(h => (
                  <th key={h} className="table-th">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {!backtests?.items.length && (
                <tr>
                  <td colSpan={5} className="table-td text-center text-gray-400 py-8">
                    {t('page.overview.empty_backtest')} · <Link to="/strategies" className="text-brand-600 hover:underline">{t('page.overview.create_strategy')}</Link>
                  </td>
                </tr>
              )}
              {backtests?.items.map(r => (
                <tr key={r.run_id} className="table-row">
                  <td className="table-td font-mono text-xs">{r.run_id.slice(0, 8)}…</td>
                  <td className="table-td font-medium">{r.strategy_id}</td>
                  <td className="table-td text-gray-500">{r.engine}</td>
                  <td className="table-td"><StatusBadge status={r.status} /></td>
                  <td className="table-td text-gray-400">{formatApiDateTime(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Recent knowledge docs */}
      {(knowledgeDocs?.items.length ?? 0) > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-base font-semibold text-gray-700">{t('page.overview.section.recent_docs')}</h2>
            <Link to="/knowledge" className="text-xs text-brand-600 hover:underline">{t('page.overview.view_all')}</Link>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {knowledgeDocs?.items.slice(0, 6).map(d => (
              <div key={d.doc_id} className="card py-3">
                <div className="font-medium text-sm text-gray-900 truncate">
                  {d.title || <em className="text-gray-400">{t('page.overview.untitled')}</em>}
                </div>
                <div className="flex gap-2 mt-1 text-xs text-gray-400">
                  <span>{d.source_name || t('page.overview.unknown_source')}</span>
                  <span>·</span>
                  <span className="capitalize">{d.logical_type}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* System info */}
      <div className="card bg-gradient-to-r from-brand-600 to-indigo-700 text-white">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-semibold text-lg">{t('page.overview.system_info.title')}</div>
            <div className="text-blue-100 text-sm mt-1">
              {t('page.overview.system_info.backend_api')} · <span className="font-mono text-xs">localhost:8000/api/docs</span>
            </div>
          </div>
          <div className="text-right text-sm text-blue-100 space-y-0.5">
            <div>{t('page.overview.system_info.research_only')}</div>
          </div>
        </div>
      </div>
    </div>
  )
}
