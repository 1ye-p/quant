/**
 * cQuant API — Dashboard domain.
 */

import { api, type RequestConfig } from './client'

// ── API ────────────────────────────────────────────────────────────────────

export const dashboardApi = {
  bestRecent: (days = 7, config?: RequestConfig) =>
    api.get<{
      run_id: string | null
      strategy_id: string | null
      sharpe: number | null
      max_drawdown: number | null
      cagr: number | null
    }>(`/backtests/best-recent?days=${days}`, config),

  icLeaderboard: (limit = 5, config?: RequestConfig) =>
    api.get<{
      items: {
        factor_name: string
        mean_ic: number
        ir: number
        hit_rate: number
        /** IC 口径版本：'v2_top20' = Top20% 截面口径；null = 历史行（Top100） */
        algo_version?: string | null
      }[]
    }>(`/factors/ic-leaderboard?limit=${limit}`, config),

  backtestTrend: (days = 30, config?: RequestConfig) =>
    api.get<{ items: { date: string; count: number }[]; days: number }>(
      `/datasets/backtest-trend?days=${days}`,
      config,
    ),

  icTrend: (days = 30, config?: RequestConfig) =>
    api.get<{ items: { date: string; avg_ic: number }[]; days: number }>(
      `/factors/ic-trend?days=${days}`,
      config,
    ),
}
