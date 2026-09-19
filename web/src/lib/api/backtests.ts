/**
 * cQuant API — Backtests domain.
 */

import { api, type RequestConfig } from './client'
import type { ValidationSuite } from '../validationSummary'
import type {
  Backtest,
  BacktestFill,
  BacktestCreateParams,
  BacktestResult,
  BacktestJobStatus,
  BacktestCompareRun,
  WalkForwardFold,
  WalkForwardConfig,
  RoundTrip,
} from '../types'

// Re-export types for backward compatibility
export type { Backtest as BacktestRun, BacktestFill, WalkForwardConfig, WalkForwardFold }
export type { ValidationSuite }

// ── API ──────────────────────────────────────────────────────────────────────

export interface BacktestListParams {
  offset?: number
  limit?: number
  status?: string
  engine?: string
  strategy_id?: string
  start_date?: string
  end_date?: string
  sort_by?: 'started_at' | 'strategy_id' | 'status' | 'engine' | 'sharpe_ratio' | 'total_return' | 'max_drawdown'
  sort_order?: 'asc' | 'desc'
}

export const backtestsApi = {
  list: (params: BacktestListParams = {}, config?: RequestConfig) => {
    const { offset = 0, limit = 50, ...filters } = params
    const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
    for (const [k, v] of Object.entries(filters)) {
      if (v) qs.set(k, v)
    }
    return api.get<{ items: Backtest[]; total: number }>(
      `/backtests?${qs.toString()}`,
      config,
    )
  },

  get: (id: string, config?: RequestConfig) =>
    api.get<Backtest>(`/backtests/${id}`, config),

  getAnalysis: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/analysis`, config),

  getRisk: (id: string, limit = 20, config?: RequestConfig) =>
    api.get<{ items: Record<string, unknown>[]; total: number }>(
      `/backtests/${id}/risk?limit=${limit}`,
      config,
    ),

  create: (body: BacktestCreateParams, config?: RequestConfig) =>
    api.post<BacktestResult>('/backtests', body, config),

  getFills: (id: string, offset = 0, limit = 50, config?: RequestConfig) =>
    api.get<{ items: BacktestFill[]; total: number; offset: number; limit: number }>(
      `/backtests/${id}/fills?offset=${offset}&limit=${limit}`,
      config,
    ),

  pollJob: (jobId: string, config?: RequestConfig) =>
    api.get<BacktestJobStatus>(`/backtests/jobs/${jobId}`, config),

  triggerAnalysis: (id: string, params?: { embargo_days?: number }, config?: RequestConfig) =>
    api.post<{ job_id: string; run_id: string; status: string }>(
      `/backtests/${id}/analyze`,
      params,
      config,
    ),

  compare: (runIds: string, config?: RequestConfig) =>
    api.get<{ runs: BacktestCompareRun[] }>(
      `/backtests/compare?run_ids=${encodeURIComponent(runIds)}`,
      config,
    ),

  getWalkForwardFolds: (id: string, config?: RequestConfig) =>
    api.get<{
      run_id: string
      n_folds: number
      folds: WalkForwardFold[]
      aggregated: Record<string, number>
    }>(`/backtests/${id}/walk-forward-folds`, config),

  getTca: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/tca`, config),

  getRiskRolling: (id: string, window = 60, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(
      `/backtests/${id}/risk-rolling?window=${window}`,
      config,
    ),

  getDrawdowns: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/drawdowns`, config),

  getDrawdownTimeseries: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/drawdown-timeseries`, config),

  getReturnDistribution: (id: string, bins = 50, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(
      `/backtests/${id}/return-distribution?bins=${bins}`,
      config,
    ),

  getCorrelation: (id: string, window = 60, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(
      `/backtests/${id}/correlation?window=${window}`,
      config,
    ),

  getFactorExposure: (id: string, window = 20, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(
      `/backtests/${id}/factor-exposure?window=${window}`,
      config,
    ),

  getStressTest: (id: string, customStart?: string, customEnd?: string, config?: RequestConfig) => {
    const params = new URLSearchParams()
    if (customStart) params.set('custom_start', customStart)
    if (customEnd) params.set('custom_end', customEnd)
    const qs = params.toString()
    return api.get<Record<string, unknown>>(
      `/backtests/${id}/stress-test${qs ? `?${qs}` : ''}`,
      config,
    )
  },

  getRiskContribution: (id: string, window = 60, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(
      `/backtests/${id}/risk-contribution?window=${window}`,
      config,
    ),

  getCalendarAnalysis: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/calendar-analysis`, config),

  getTradeAnalysis: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/trade-analysis`, config),

  getRoundTrips: (id: string, config?: RequestConfig) =>
    api.get<{ total_round_trips: number; round_trips: RoundTrip[] }>(
      `/backtests/${id}/round-trips`,
      config,
    ),

  // Extended
  tearsheet: (id: string, config?: RequestConfig) =>
    api.get<Record<string, unknown>>(`/backtests/${id}/tearsheet`, config),

  validationWindows: (id: string, config?: RequestConfig) =>
    api.get<{ walk_forward: Record<string, unknown>[]; cpcv: Record<string, unknown>[] }>(
      `/backtests/${id}/validation-windows`,
      config,
    ),

  multipleTesting: (id: string, config?: RequestConfig) =>
    api.get<Record<string, Record<string, unknown>>>(
      `/backtests/${id}/multiple-testing`,
      config,
    ),

  statisticalTest: (
    body: { backtest_ids: string[]; test_type: string; confidence?: number; block_size?: number },
    config?: RequestConfig,
  ) =>
    api.post<{ test_type: string; results: Record<string, unknown> }>(
      '/backtests/compare/statistical-test',
      body,
      config,
    ),

  // ── Sensitivity Analysis ──────────────────────────────────────────────────

  runSensitivity: (
    runId: string,
    body: { param_grid: Record<string, any[]>; primary_metric?: string; max_combinations?: number },
    config?: RequestConfig,
  ) =>
    api.post<{ job_id: string; status: string }>(
      `/backtests/${runId}/sensitivity`,
      body,
      config,
    ),

  getSensitivityResult: (runId: string, jobId: string, config?: RequestConfig) =>
    api.get<{ job_id: string; status: string; result?: any; error?: string }>(
      `/backtests/${runId}/sensitivity/${jobId}`,
      config,
    ),

  getSensitivityHistory: (runId: string, config?: RequestConfig) =>
    api.get<{ history: Array<{ job_id: string; status: string; created_at: string; completed_at?: string; error?: string }> }>(
      `/backtests/${runId}/sensitivity/history`,
      config,
    ),

  // ── Validation Suite (Phase 4 T2) ──────────────────────────────────────────

  /** Trigger the one-click validation suite; poll via pollJob(job_id). */
  runValidationSuite: (runId: string, config?: RequestConfig) =>
    api.post<{ job_id: string; run_id: string; status: string }>(
      `/backtests/${runId}/validation-suite`,
      {},
      config,
    ),

  /** Latest validation suite result. 404 when never run. */
  getValidationSuite: (runId: string, config?: RequestConfig) =>
    api.get<ValidationSuite>(`/backtests/${runId}/validation-suite`, config),

  /** Regime state-interval timeline + contribution split (T4). */
  getRegimeTimeline: (id: string, config?: RequestConfig) =>
    api.get<RegimeTimeline>(
      `/backtests/${id}/regime-timeline`,
      config,
    ),

  /** Multi-dimensional strategy ranking across runs (T7). */
  rank: (
    body: { run_ids: string[]; weights?: Record<string, number> },
    config?: RequestConfig,
  ) =>
    api.post<StrategyRankResult>('/backtests/rank', body, config),

  // ── AI Research Report (Phase 4 T6) ───────────────────────────────────────

  /** Trigger AI report generation; poll via pollJob(job_id), then getReport. */
  generateReport: (runId: string, config?: RequestConfig) =>
    api.post<{ job_id: string; run_id: string; status: string }>(
      `/backtests/${runId}/report`,
      {},
      config,
    ),

  /** Latest AI research report (Chinese Markdown). 404 when never generated. */
  getReport: (runId: string, config?: RequestConfig) =>
    api.get<BacktestResearchReport>(`/backtests/${runId}/report`, config),
}

export interface BacktestResearchReport {
  report_id: string
  run_id: string
  content_md: string
  created_at: string
}

// ── Regime timeline / ranking types ─────────────────────────────────────────

export interface RegimeTimelineInterval {
  state: 'full' | 'reduced' | 'flat'
  scale: number
  start: string
  end: string
  days: number
  interval_return: number | null
}

export interface RegimeTimeline {
  run_id: string
  applicable: boolean
  reason?: string
  min_cycles?: number
  benchmark_asset_id?: string
  intervals?: RegimeTimelineInterval[]
  cycles?: number
  sufficient?: boolean
  contribution?: {
    held_return: number
    held_days: number
    flat_days: number
    out_of_market: {
      avoided: number
      missed: number
      benchmark_source: 'benchmark' | 'mean_return_approx' | 'none'
    }
  }
  nav_series?: { date: string; nav: number }[]
  scale_series?: { date: string; desired: number; actual: number | null }[]
}

export interface RankedStrategy {
  rank: number
  strategy_id: string
  run_id: string
  composite_score: number
  dimension_scores: Record<string, number>
  raw_metrics: Record<string, number | string>
}

export interface StrategyRankResult {
  ranked_strategies: RankedStrategy[]
  summary: {
    total_strategies: number
    top_strategy: RankedStrategy | null
    weights: Record<string, number>
  }
  weights: Record<string, number>
}

// ── Backward-compatible alias ───────────────────────────────────────────────

/** @deprecated Use `backtestsApi` which now includes tearsheet/validationWindows/multipleTesting. */
export const backtestExtApi = {
  tearsheet: backtestsApi.tearsheet,
  validationWindows: backtestsApi.validationWindows,
  multipleTesting: backtestsApi.multipleTesting,
}
