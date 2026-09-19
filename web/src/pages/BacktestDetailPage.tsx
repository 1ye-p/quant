import { NavLink, Outlet, useParams, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { backtestsApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { useEffect } from 'react'
import { useWorkflowStore } from '@/stores/workflowStore'
import { ValidationBadge } from '@/components/backtests/ValidationPanel'
import { DecisionSummaryCard } from '@/components/backtests/DecisionSummaryCard'

type TabDef = { id: string; path: string }

const TABS: TabDef[] = [
  { id: 'overview', path: '' },
  { id: 'tearsheet', path: 'tearsheet' },
  { id: 'overfitting', path: 'overfitting' },
  { id: 'fills', path: 'fills' },
  { id: 'walkforward', path: 'walkforward' },
  { id: 'tca', path: 'tca' },
  { id: 'risk', path: 'risk' },
  { id: 'calendar', path: 'calendar' },
  { id: 'advanced', path: 'advanced' },
  { id: 'model-compare', path: 'model-compare' },
  { id: 'feature-importance', path: 'feature-importance' },
  { id: 'model-diagnostics', path: 'model-diagnostics' },
  { id: 'trade-analysis', path: 'trade-analysis' },
  { id: 'regime_timeline', path: 'regime_timeline' },
  { id: 'report', path: 'report' },
]

export function BacktestDetailPage() {
  const { t } = useTranslation()
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const { data: detail } = useQuery({
    queryKey: queryKeys.backtests.detail(id!),
    queryFn: () => backtestsApi.get(id!),
    enabled: !!id,
  })

  const isWalkForward = detail?.engine === 'walk_forward'
  const visibleTabs = isWalkForward ? TABS : TABS.filter(tab => tab.id !== 'walkforward')

  // Workflow integration: update context when backtest detail loads
  const { currentWorkflow, updateContext } = useWorkflowStore()
  useEffect(() => {
    if (detail && currentWorkflow) {
      updateContext({
        backtestId: id,
        backtestResults: detail.metrics ?? null,
        strategyId: detail.strategy_id,
      })
    }
  }, [detail, id, currentWorkflow])

  if (!id) return null

  return (
    <div>
      {/* Header with back button */}
      <div className="flex items-center gap-3 mb-4">
        <button
          onClick={() => navigate('/backtests')}
          className="text-gray-400 hover:text-gray-600 text-sm"
        >
          {t('page.backtest.backToList')}
        </button>
        <h1 className="page-title mb-0">
          {detail?.strategy_id ?? t('page.backtest.title')}
        </h1>
        <span className="font-mono text-xs text-gray-400">{id.slice(0, 12)}...</span>
        {detail?.status === 'completed' && <ValidationBadge runId={id} />}
      </div>

      {/* Decision summary card (core metrics + validation verdict) */}
      {detail?.status === 'completed' && <DecisionSummaryCard detail={detail} />}

      {/* Tab navigation */}
      <div className="flex gap-1 mb-4 border-b border-gray-200 overflow-x-auto">
        {visibleTabs.map(({ id: tabId, path }) => (
          <NavLink
            key={tabId}
            to={path}
            end={path === ''}
            className={({ isActive }) =>
              `px-4 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap ${
                isActive
                  ? 'border-brand-600 text-brand-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`
            }
          >
            {t('page.backtest.tabs.' + tabId)}
          </NavLink>
        ))}
      </div>

      {/* Child route content */}
      <Outlet />
    </div>
  )
}
