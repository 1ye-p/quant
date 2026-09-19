/**
 * Backward compatibility layer for cQuant API.
 *
 * @deprecated Import from './api/' domain modules instead.
 * All exports are re-exported from the new modular structure.
 */

// ── Client utilities ───────────────────────────────────────────────────────
export {
  request,
  requestWithRetry,
  api,
  type RequestConfig,
  type RetryConfig,
} from './api/client'

// ── Domain APIs ────────────────────────────────────────────────────────────

export { datasetsApi } from './api/datasets'
export type { DatasetVersion, ExtIndPreview, ExtIndImportReport, ExtIndImportConfig } from './api/datasets'

export { dashboardApi } from './api/dashboard'

export { backtestsApi, backtestExtApi } from './api/backtests'
export type {
  WalkForwardConfig,
  BacktestRun,
  BacktestFill,
} from './api/backtests'

export { knowledgeApi } from './api/knowledge'
export type { KnowledgeDoc, SearchHit, SearchResponse, QASource, QAResponse } from './api/knowledge'

export { advisorApi, advisorExtApi } from './api/advisor'
export type { ChatResponse } from './api/advisor'

export { newsApi } from './api/news'
export type { NewsEvent, NewsStats } from './api/news'

export { strategiesApi } from './api/strategies'

export { mlApi } from './api/ml'
export type {
  ModelCatalogInfo,
  MLExperiment,
  MLJob,
  TrainingCurvePoint,
  PredictionBin,
  DiagnosticsData,
} from './api/ml'

export { liveApi } from './api/live'
export type { LiveDeployment, LiveStrategy, LiveExecution } from './api/live'

export { factorsApi, factorAnalyticsApi, customFactorApi, dslApi } from './api/factors'

export { scoringApi } from './api/scoring'
export type { ScoringRun, ScoringResult, ScoringConfigBody } from './api/scoring'

export { optimizeApi } from './api/optimize'
export type {
  SectorLimit,
  FactorExposureLimit,
  ConstraintConfig,
  ViewSpec,
  OptimizeRequest,
  OptimizeResult,
  CovarianceRequest,
  CovarianceResult,
  FrontierPoint,
  FrontierResult,
} from './api/optimize'

export { riskApi } from './api/risk'
export type {
  PolicyParam,
  PolicyInfo,
  SizerInfo,
  RiskCheckRequest,
  RiskCheckResult,
} from './api/risk'

export { tradingApi } from './api/trading'
export type {
  TradeOrder,
  TradePosition,
  TradeAccount,
  TradePnL,
} from './api/trading'

export { realtimeApi } from './api/realtime'
export type { RealtimeQuote } from './api/realtime'

export { jobsApi } from './api/jobs'

export { pipelineApi } from './api/pipeline'
export type { PipelineStage, PipelineStatusResponse } from './api/pipeline'

export { alertsApi } from './api/alerts'
export type { AlertRule, AlertHistory, NotificationChannel } from './api/alerts'

export { indicatorsApi } from './api/indicators'
export type { IndicatorInfo, IndicatorParam, IndicatorCategories } from './api/indicators'

export { queryApi } from './api/query'
export type { QueryBody, QueryWhereClause, QueryOrderByClause, QueryResult, CoverageResult } from './api/query'
