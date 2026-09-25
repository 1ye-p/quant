import { z } from 'zod'

// ── BacktestStatus ─────────────────────────────────────────────────────────

export const BacktestStatusSchema = z.enum([
  'pending',
  'running',
  'completed',
  'failed',
  'cancelled',
])

export type BacktestStatus = z.infer<typeof BacktestStatusSchema>

// ── Backtest Metrics ───────────────────────────────────────────────────────

export const BacktestMetricsSchema = z.object({
  total_return: z.number(),
  annualized_return: z.number(),
  annualized_volatility: z.number(),
  sharpe_ratio: z.number(),
  sortino_ratio: z.number(),
  max_drawdown: z.number(),
  calmar_ratio: z.number(),
  win_rate: z.number(),
  profit_factor: z.number(),
  var_95: z.number(),
  cvar_95: z.number(),
  beta: z.number().nullable(),
  total_trades: z.number(),
  trading_days: z.number(),
  information_ratio: z.number().nullable(),
  tracking_error: z.number().nullable(),
  alpha: z.number().nullable(),
  omega_ratio: z.number().nullable(),
  tail_ratio: z.number().nullable(),
  turnover_pct: z.number().nullable(),
  hhi: z.number().nullable(),
})

export type BacktestMetrics = z.infer<typeof BacktestMetricsSchema>

// ── Backtest ───────────────────────────────────────────────────────────────

export const BacktestSchema = z.object({
  run_id: z.string(),
  engine: z.string(),
  strategy_id: z.string(),
  dataset_version: z.string(),
  started_at: z.string(),
  completed_at: z.string().nullable(),
  status: z.string(),
  strategy_type: z.string().optional(),
  is_running_job: z.boolean().optional(),
  /** Raw run tags (JSON string from DuckDB; parsed defensively by consumers). */
  tags: z.string().nullable().optional(),
  metrics: BacktestMetricsSchema.optional(),
})

export type Backtest = z.infer<typeof BacktestSchema>

// ── BacktestFill ───────────────────────────────────────────────────────────

export const BacktestFillSchema = z.object({
  trade_date: z.string(),
  asset_id: z.string(),
  side: z.string(),
  qty: z.number(),
  price: z.number(),
  notional: z.number(),
  commission: z.number(),
  stamp_duty: z.number(),
  slippage: z.number(),
  total_cost: z.number(),
  order_idx: z.number().optional(),
})

export type BacktestFill = z.infer<typeof BacktestFillSchema>

// ── WalkForwardConfig ──────────────────────────────────────────────────────

export const WalkForwardConfigSchema = z.object({
  n_splits: z.number(),
  gap_days: z.number(),
  window_type: z.enum(['expanding', 'sliding']),
  step_days: z.number().optional(),
  purge_window: z.number(),
})

export type WalkForwardConfig = z.infer<typeof WalkForwardConfigSchema>

// ── BacktestCreateParams ───────────────────────────────────────────────────

export const BacktestCreateParamsSchema = z.object({
  strategy_id: z.string(),
  dataset_version: z.string(),
  start_date: z.string(),
  end_date: z.string(),
  top_n: z.number().optional(),
  sort_factor: z.string().optional(),
  feature_set_version: z.string().optional(),
  strategy_type: z.string().optional(),
  model_version: z.string().optional(),
  label_name: z.string().optional(),
  train_end_date: z.string().optional(),
  walk_forward: WalkForwardConfigSchema.optional(),
  eval_mode: z.string().optional(),
  ml_config: z
    .object({
      train_mode: z.enum(['existing', 'new']),
      model_type: z.string().optional(),
      model_id_prefix: z.string().optional(),
      n_splits: z.number().optional(),
      gap_days: z.number().optional(),
      model_params: z.record(z.unknown()).optional(),
    })
    .optional(),
  short_n: z.number().optional(),
  sector_map: z.record(z.string()).optional(),
  top_sectors: z.number().optional(),
  top_n_per_sector: z.number().optional(),
  sub_strategy_configs: z.array(z.record(z.unknown())).optional(),
  combo_method: z.string().optional(),
  universe_id: z.string().optional(),
  scoring_run_id: z.string().optional(),
  custom_weights: z.record(z.number()).optional(),
  benchmark_asset_id: z.string().optional(),
  missing_factor_strategy: z.enum(['fill_0', 'fill_median', 'exclude', 'risk_penalty']).optional(),
  penalty_per_missing: z.number().optional(),
})

export type BacktestCreateParams = z.infer<typeof BacktestCreateParamsSchema>

// ── BacktestResult (job response) ──────────────────────────────────────────

export const BacktestResultSchema = z.object({
  job_id: z.string(),
  strategy_id: z.string(),
  status: z.string(),
  warning: z.string().optional(),
})

export type BacktestResult = z.infer<typeof BacktestResultSchema>

// ── BacktestJobStatus ──────────────────────────────────────────────────────

export const BacktestJobStatusSchema = z.object({
  job_id: z.string(),
  status: z.string(),
  run_id: z.string().nullable(),
  error: z.string().nullable(),
})

export type BacktestJobStatus = z.infer<typeof BacktestJobStatusSchema>

// ── BacktestCompareRun ─────────────────────────────────────────────────────

export const BacktestCompareRunSchema = z.object({
  run_id: z.string(),
  strategy_id: z.string(),
  engine: z.string(),
  status: z.string(),
  started_at: z.string(),
  dataset_version: z.string(),
  metrics: z.record(z.number()),
  nav_series: z.array(z.object({ date: z.string(), nav: z.number() })),
})

export type BacktestCompareRun = z.infer<typeof BacktestCompareRunSchema>

// ── WalkForwardFold ────────────────────────────────────────────────────────

export const WalkForwardFoldSchema = z.object({
  fold_id: z.number(),
  train_start: z.string(),
  train_end: z.string(),
  test_start: z.string(),
  test_end: z.string(),
  fold_run_id: z.string(),
  metrics: z.record(z.unknown()),
})

export type WalkForwardFold = z.infer<typeof WalkForwardFoldSchema>

// ── RoundTrip (trade round-trip for MFE/MAE) ──────────────────────────────

export const RoundTripSchema = z.object({
  asset_id: z.string(),
  direction: z.enum(['long', 'short']),
  entry_date: z.string(),
  exit_date: z.string(),
  entry_price: z.number(),
  exit_price: z.number(),
  pnl: z.number(),
  pnl_pct: z.number(),
  mfe: z.number(),
  mae: z.number(),
  holding_days: z.number(),
})

export type RoundTrip = z.infer<typeof RoundTripSchema>
