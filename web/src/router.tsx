import { lazy } from 'react'
import { createBrowserRouter } from 'react-router-dom'
import { AppLayout } from '@/components/layout/AppLayout'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { NotFoundPage } from '@/pages/NotFoundPage'

// Helper: wrap named-export modules for React.lazy
const named = <T extends Record<string, unknown>>(
  loader: () => Promise<T>,
  name: keyof T,
) => lazy(() => loader().then(m => ({ default: m[name] as React.ComponentType })))

const OverviewPage  = named(() => import('@/pages/OverviewPage'), 'OverviewPage')
const WelcomePage   = named(() => import('@/pages/WelcomePage'), 'WelcomePage')
const DatasetsPage  = named(() => import('@/pages/DatasetsPage'), 'DatasetsPage')
const DataBrowserPage = named(() => import('@/pages/DataBrowserPage'), 'DataBrowserPage')
const ExternalIndicatorsImportPage = named(() => import('@/pages/ExternalIndicatorsImport'), 'ExternalIndicatorsImportPage')
const BacktestsListPage = named(() => import('@/pages/BacktestsListPage'), 'BacktestsListPage')
const BacktestDetailPage = named(() => import('@/pages/BacktestDetailPage'), 'BacktestDetailPage')
const BacktestComparePage = named(() => import('@/pages/BacktestComparePage'), 'BacktestComparePage')
const BacktestOverviewTab = named(() => import('@/pages/backtest-tabs/BacktestOverviewTab'), 'BacktestOverviewTab')
const BacktestTearsheetTab = named(() => import('@/pages/backtest-tabs/BacktestTearsheetTab'), 'BacktestTearsheetTab')
const BacktestOverfittingTab = named(() => import('@/pages/backtest-tabs/BacktestOverfittingTab'), 'BacktestOverfittingTab')
const BacktestFillsTab = named(() => import('@/pages/backtest-tabs/BacktestFillsTab'), 'BacktestFillsTab')
const BacktestWalkForwardTab = named(() => import('@/pages/backtest-tabs/BacktestWalkForwardTab'), 'BacktestWalkForwardTab')
const BacktestTcaTab = named(() => import('@/pages/backtest-tabs/BacktestTcaTab'), 'BacktestTcaTab')
const BacktestRiskTab = named(() => import('@/pages/backtest-tabs/BacktestRiskTab'), 'BacktestRiskTab')
const BacktestCalendarTab = named(() => import('@/pages/backtest-tabs/BacktestCalendarTab'), 'BacktestCalendarTab')
const BacktestAdvancedTab = named(() => import('@/pages/backtest-tabs/BacktestAdvancedTab'), 'BacktestAdvancedTab')
const BacktestModelCompareTab = named(() => import('@/pages/backtest-tabs/BacktestModelCompareTab'), 'BacktestModelCompareTab')
const BacktestFeatureImportanceTab = named(() => import('@/pages/backtest-tabs/BacktestFeatureImportanceTab'), 'BacktestFeatureImportanceTab')
const BacktestModelDiagnosticsTab = named(() => import('@/pages/backtest-tabs/BacktestModelDiagnosticsTab'), 'BacktestModelDiagnosticsTab')
const BacktestTradeAnalysisTab = named(() => import('@/pages/backtest-tabs/BacktestTradeAnalysisTab'), 'BacktestTradeAnalysisTab')
const BacktestRegimeTimelineTab = named(() => import('@/pages/backtest-tabs/BacktestRegimeTimelineTab'), 'BacktestRegimeTimelineTab')
const BacktestReportTab = named(() => import('@/pages/backtest-tabs/BacktestReportTab'), 'BacktestReportTab')
const KnowledgePage = named(() => import('@/pages/KnowledgePage'), 'KnowledgePage')
const AdvisorPage   = named(() => import('@/pages/AdvisorPage'), 'AdvisorPage')
const FactorsPage   = named(() => import('@/pages/FactorsPage'), 'FactorsPage')
const StrategiesPage = named(() => import('@/pages/StrategiesPage'), 'StrategiesPage')
const MLLabPage     = named(() => import('@/pages/MLLabPage'), 'MLLabPage')
const NewsPage      = named(() => import('@/pages/NewsPage'), 'NewsPage')
const LivePage      = named(() => import('@/pages/LivePage'), 'LivePage')
const TradingPage   = named(() => import('@/pages/TradingPage'), 'TradingPage')
const OptimizePage  = named(() => import('@/pages/OptimizePage'), 'OptimizePage')
const RiskPage      = named(() => import('@/pages/RiskPage'), 'RiskPage')
const ScoringPage   = named(() => import('@/pages/ScoringPage'), 'ScoringPage')
const AlertsPage    = named(() => import('@/pages/AlertsPage'), 'AlertsPage')
const TasksPage     = named(() => import('@/pages/TasksPage'), 'TasksPage')
const PipelinePage  = named(() => import('@/pages/PipelinePage'), 'PipelinePage')
const SharePage     = named(() => import('@/pages/SharePage'), 'SharePage')
const SettingsPage  = named(() => import('@/pages/SettingsPage'), 'SettingsPage')

export const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <ErrorBoundary>
        <AppLayout />
      </ErrorBoundary>
    ),
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'welcome',    element: <WelcomePage /> },
      { path: 'factors',    element: <FactorsPage /> },
      { path: 'strategies', element: <StrategiesPage /> },
      { path: 'ml',         element: <MLLabPage /> },
      {
        path: 'backtests',
        children: [
          { index: true, element: <BacktestsListPage /> },
          { path: 'compare', element: <BacktestComparePage /> },
          {
            path: ':id',
            element: <BacktestDetailPage />,
            children: [
              { index: true, element: <BacktestOverviewTab /> },
              { path: 'tearsheet', element: <BacktestTearsheetTab /> },
              { path: 'overfitting', element: <BacktestOverfittingTab /> },
              { path: 'fills', element: <BacktestFillsTab /> },
              { path: 'walkforward', element: <BacktestWalkForwardTab /> },
              { path: 'tca', element: <BacktestTcaTab /> },
              { path: 'risk', element: <BacktestRiskTab /> },
              { path: 'calendar', element: <BacktestCalendarTab /> },
              { path: 'advanced', element: <BacktestAdvancedTab /> },
              { path: 'model-compare', element: <BacktestModelCompareTab /> },
              { path: 'feature-importance', element: <BacktestFeatureImportanceTab /> },
              { path: 'model-diagnostics', element: <BacktestModelDiagnosticsTab /> },
              { path: 'trade-analysis', element: <BacktestTradeAnalysisTab /> },
              { path: 'regime_timeline', element: <BacktestRegimeTimelineTab /> },
              { path: 'report', element: <BacktestReportTab /> },
            ],
          },
        ],
      },
      { path: 'live',       element: <LivePage /> },
      { path: 'trading',    element: <TradingPage /> },
      { path: 'news',       element: <NewsPage /> },
      { path: 'optimize',   element: <OptimizePage /> },
      { path: 'risk',       element: <RiskPage /> },
      { path: 'scoring',    element: <ScoringPage /> },
      { path: 'datasets/external-indicators', element: <ExternalIndicatorsImportPage /> },
      { path: 'datasets',   element: <DatasetsPage /> },
      { path: 'data-browser', element: <DataBrowserPage /> },
      { path: 'knowledge',  element: <KnowledgePage /> },
      { path: 'advisor',    element: <AdvisorPage /> },
      { path: 'alerts',     element: <AlertsPage /> },
      { path: 'tasks',      element: <TasksPage /> },
      { path: 'pipeline',   element: <PipelinePage /> },
      { path: 'share/:shareId', element: <SharePage /> },
      { path: 'settings',   element: <SettingsPage /> },
    ],
  },
  {
    path: '*',
    element: <NotFoundPage />,
  },
])
