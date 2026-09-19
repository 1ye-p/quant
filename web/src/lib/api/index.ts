/**
 * cQuant API — Unified client module.
 *
 * Re-exports error classes, client utilities, and all domain APIs.
 */

// Error classes and utilities
export {
  ApiError,
  NetworkError,
  TimeoutError,
  AbortError,
  ErrorCode,
  type ErrorCode as ErrorCodeType,
  statusCodeToErrorCode,
  createApiErrorFromResponse,
  isRetryableError,
  isTimeoutError,
  isAbortError,
  getErrorMessage,
  getErrorStatus,
  getErrorCode,
} from './errors'

// Client functions and types
export {
  request,
  requestWithRetry,
  api,
  type RequestConfig,
  type RetryConfig,
} from './client'

// Domain APIs
export { backtestsApi, backtestExtApi } from './backtests'
export type {
  WalkForwardConfig,
  BacktestRun,
  BacktestFill,
} from './backtests'

export { mlApi } from './ml'
export type {
  ModelCatalogInfo,
  MLExperiment,
  MLJob,
  TrainingCurvePoint,
  PredictionBin,
  DiagnosticsData,
} from './ml'

export { factorsApi, factorAnalyticsApi, customFactorApi, dslApi } from './factors'

export { strategiesApi } from './strategies'

export { optimizeApi } from './optimize'
export type {
  SectorLimit,
  FactorExposureLimit,
  ConstraintConfig,
  OptimizeRequest,
  OptimizeResult,
  CovarianceRequest,
  CovarianceResult,
  FrontierPoint,
  FrontierResult,
  ViewSpec,
} from './optimize'

export { datasetsApi } from './datasets'
export type { DatasetVersion } from './datasets'

export { dashboardApi } from './dashboard'

export { knowledgeApi } from './knowledge'
export type { KnowledgeDoc, SearchHit, SearchResponse, QASource, QAResponse } from './knowledge'

export { advisorApi, advisorExtApi } from './advisor'
export type { ChatResponse } from './advisor'

export { newsApi } from './news'
export type { NewsEvent, NewsStats } from './news'

export { liveApi } from './live'
export type { LiveDeployment, LiveStrategy, LiveExecution } from './live'

export { alertsApi } from './alerts'
export type { AlertRule, AlertHistory, NotificationChannel, SilenceRule } from './alerts'

export { tradingApi } from './trading'
export type {
  TradeOrder,
  TradePosition,
  TradeAccount,
  TradePnL,
  AlgoOrderParams,
  AlgoSlice,
  AlgoOrderStatus,
} from './trading'

export { realtimeApi } from './realtime'
export type { RealtimeQuote } from './realtime'

export { shareApi } from './share'
export type { ShareCreateBody, ShareCreateResponse, ShareContent } from './share'

export { scoringApi } from './scoring'
export type { ScoringRun, ScoringResult, ScoringConfigBody } from './scoring'

export { jobsApi } from './jobs'

export { pipelineApi } from './pipeline'
export type { PipelineStage, PipelineStatusResponse } from './pipeline'

export { riskApi } from './risk'
export type {
  PolicyParam,
  PolicyInfo,
  SizerInfo,
  RiskCheckRequest,
  RiskCheckResult,
} from './risk'

export { indicatorsApi } from './indicators'
export type {
  IndicatorInfo,
  IndicatorParam,
  IndicatorCategories,
  EvaluateConditionResponse,
  ConditionPreviewResponse,
} from './indicators'

export { marketApi } from './market'
export type { OHLCV, PriceStats, PricesResponse } from './market'

export { queryApi } from './query'
export type {
  QueryBody,
  QueryWhereClause,
  QueryOrderByClause,
  QueryResult,
  CoverageResult,
} from './query'
