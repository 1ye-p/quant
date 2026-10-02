/**
 * cQuant API — Datasets domain.
 */

import { api, request, type RequestConfig } from './client'
import { ApiError } from './errors'

// ── Types (not yet in types/) ────────────────────────────────────────────────

export interface DatasetVersion {
  version_id: string
  dataset_name: string
  frequency: string
  start_date: string
  end_date: string
  asset_count: number | null
  row_count: number | null
  source: string
  created_at: string
  is_current: boolean
}

// ── API ────────────────────────────────────────────────────────────────────


function extIndForm(file: File, config?: ExtIndImportConfig): FormData {
  const form = new FormData()
  form.append('file', file)
  if (config) form.append('config', JSON.stringify(config))
  return form
}

export const datasetsApi = {
  list: (limit = 50, config?: RequestConfig) =>
    api.get<{ items: DatasetVersion[]; total: number }>(`/datasets?limit=${limit}`, config),

  get: (id: string, config?: RequestConfig) =>
    api.get<DatasetVersion>(`/datasets/${id}`, config),

  universes: (config?: RequestConfig) =>
    api.get<{
      predefined: { id: string; name: string; description: string }[]
      available_assets: string[]
      total_assets: number
    }>('/datasets/universes', config),

  quality: (version = '', config?: RequestConfig) =>
    api.get<{
      version: string
      stats: {
        n_assets: number
        min_date: string
        max_date: string
        total_rows: number
        recent_assets: number
        null_rate: number
        outlier_count: number
      }
      daily_coverage: { trade_date: string; n_assets: number }[]
      bottom_assets: { asset_id: string; valid_days: number }[]
    }>(`/datasets/quality?version=${encodeURIComponent(version)}`, config),

  scheduleStatus: (config?: RequestConfig) =>
    api.get<{
      enabled: boolean
      last_run: string | null
      last_status: 'success' | 'error' | 'running' | null
      last_error: string | null
      next_run: string | null
      last_data_date: string | null
    }>('/datasets/schedule', config),

  triggerIngest: (config?: RequestConfig) =>
    api.post<{ status: string }>('/datasets/schedule/trigger', undefined, config),

  freshness: (config?: RequestConfig) =>
    api.get<{ last_updated: string | null; days_stale: number }>(
      '/datasets/freshness',
      config,
    ),

  // ── New endpoints for enhanced DatasetsPage ─────────────────────────────

  getPreview: (
    id: string,
    params?: { offset?: number; limit?: number },
    config?: RequestConfig,
  ) => {
    const offset = params?.offset ?? 0
    const limit = params?.limit ?? 50
    return api.get<{
      columns: string[]
      rows: Record<string, unknown>[]
      total: number
      offset: number
      limit: number
    }>(`/datasets/${encodeURIComponent(id)}/preview?offset=${offset}&limit=${limit}`, config)
  },

  getFieldStats: (id: string, config?: RequestConfig) =>
    api.get<{
      fields: {
        name: string
        type: string
        count: number
        null_count: number
        null_rate: number
        unique_count: number
        min: number | string | null
        max: number | string | null
        mean: number | null
        std: number | null
      }[]
    }>(`/datasets/${encodeURIComponent(id)}/field-stats`, config),

  getQualityReport: (id: string, config?: RequestConfig) =>
    api.get<{
      score: number
      total_rows: number
      total_fields: number
      issues: {
        field: string
        type: string
        count: number
        percentage: number
      }[]
      suggestions: string[]
    }>(`/datasets/${encodeURIComponent(id)}/quality-report`, config),

  getAnomalies: (id: string, config?: RequestConfig) =>
    api.get<{
      anomalies: {
        type: 'outlier' | 'missing' | 'duplicate' | 'invalid'
        field: string
        count: number
        examples: string[]
      }[]
    }>(`/datasets/${encodeURIComponent(id)}/anomalies`, config),

  compareVersions: (versionA: string, versionB: string, config?: RequestConfig) =>
    api.get<{
      version_a: string
      version_b: string
      row_changes: { version_a_count: number; version_b_count: number; added: number; removed: number }
      field_changes: { added_fields: string[]; removed_fields: string[]; common_fields: string[] }
      field_stats: {
        field: string
        version_a: { min: number; max: number; mean: number; null_rate: number }
        version_b: { min: number; max: number; mean: number; null_rate: number }
        change: { mean_diff: number; mean_pct_change: number }
      }[]
    }>(`/datasets/compare?version_a=${encodeURIComponent(versionA)}&version_b=${encodeURIComponent(versionB)}`, config),

  previewExternalIndicators: (file: File, config?: RequestConfig) =>
    request<ExtIndPreview>('/datasets/external-indicators/preview', {
      method: 'POST',
      body: extIndForm(file) as unknown as BodyInit,
      headers: {},
      ...config,
    }),

  importExternalIndicators: (file: File, config: ExtIndImportConfig, reqConfig?: RequestConfig) =>
    request<ExtIndImportReport>('/datasets/external-indicators/import', {
      method: 'POST',
      body: extIndForm(file, config) as unknown as BodyInit,
      headers: {},
      ...reqConfig,
    }),

  // ── External indicator catalog (P1-5 CRUD) ──────────────────────────────

  listExtIndCatalog: (config?: RequestConfig) =>
    api.get<{ items: ExtIndCatalogEntry[]; total: number }>(
      '/datasets/external-indicators/catalog',
      config,
    ),

  getExtIndCatalog: (key: string, config?: RequestConfig) =>
    api.get<ExtIndCatalogDetail>(
      `/datasets/external-indicators/catalog/${encodeURIComponent(key)}`,
      config,
    ),

  patchExtIndCatalog: (key: string, body: ExtIndCatalogPatch, config?: RequestConfig) =>
    api.patch<ExtIndCatalogDetail>(
      `/datasets/external-indicators/catalog/${encodeURIComponent(key)}`,
      body,
      config,
    ),

  deleteExtIndCatalog: (key: string, purgeData = false, config?: RequestConfig) =>
    api.delete<ExtIndDeleteResult>(
      `/datasets/external-indicators/catalog/${encodeURIComponent(key)}?purge_data=${purgeData}`,
      config,
    ),

  // ── Built-in catalog + refresh + runs (P2) ───────────────────────────────

  listExtIndBuiltins: (config?: RequestConfig) =>
    api.get<{ items: ExtIndBuiltin[]; total: number }>(
      '/datasets/external-indicators/builtins',
      config,
    ),

  enableExtIndBuiltin: (key: string, backfillStart?: string, config?: RequestConfig) =>
    api.post<ExtIndEnableResult>(
      `/datasets/external-indicators/builtins/${encodeURIComponent(key)}/enable`,
      backfillStart ? { backfill_start: backfillStart } : {},
      config,
    ),

  refreshExtIndicators: (
    body: { keys?: string[]; backfill?: boolean },
    config?: RequestConfig,
  ) =>
    api.post<ExtIndRefreshSummary>(
      '/datasets/external-indicators/refresh',
      body,
      config,
    ),

  listExtIndRuns: (key = '', limit = 50, config?: RequestConfig) =>
    api.get<{ items: ExtIndRun[]; total: number }>(
      `/datasets/external-indicators/runs?key=${encodeURIComponent(key)}&limit=${limit}`,
      config,
    ),

  // ── Custom HTTP sources (P3) ────────────────────────────────────────────

  /** Create a custom_http catalog row. 201 on success; 409 duplicate key;
   *  400 with a `{stage, message}` detail on bad configs (see
   *  extractExtIndStageError). */
  createExtIndCustom: (body: ExtIndCustomCreateBody, config?: RequestConfig) =>
    api.post<ExtIndCatalogDetail>('/datasets/external-indicators/catalog', body, config),

  /** Connection test — single guarded fetch, nothing persisted. Needs a long
   *  client timeout: the guard may wait out the full server-side budget
   *  (up to 120 s) before returning a stage-tagged 400. */
  testExtIndCustom: (sourceConfig: CustomHTTPSourceConfig, config?: RequestConfig) =>
    api.post<ExtIndTestResult>(
      '/datasets/external-indicators/test',
      { source_config: sourceConfig },
      { timeout: 130_000, ...config },
    ),
}

/** Extract the backend's stage-tagged 400 detail shape
 *  (`HTTPException(detail={"stage": ..., "message": ...})`) from an ApiError.
 *  Non-object details (409 duplicate / plain string 400s) yield stage=null. */
export function extractExtIndStageError(e: unknown): { stage: string | null; message: string } {
  if (e instanceof ApiError) {
    const detail = (e.details as { detail?: unknown } | undefined)?.detail
    if (detail !== null && typeof detail === 'object') {
      const { stage, message } = detail as { stage?: unknown; message?: unknown }
      return {
        stage: typeof stage === 'string' ? stage : null,
        message: typeof message === 'string' ? message : e.message,
      }
    }
    return { stage: null, message: e.message }
  }
  return { stage: null, message: e instanceof Error ? e.message : String(e) }
}

// ── External indicator catalog types ────────────────────────────────────────

/** One catalog row as returned by GET /external-indicators/catalog (list or detail).
 *  `latest_trade_date` / `stale` are live-freshness fields computed server-side.
 *  `source_config` is a JSON string for csv rows and a (redacted) object for
 *  custom_http rows — see CustomHTTPSourceConfig. */
export interface ExtIndCatalogEntry {
  indicator_key: string
  display_name: string
  unit: string | null
  description: string | null
  source_type: string
  source_name: string | null
  pinned_source: string | null
  source_config: string | Record<string, unknown> | null
  available_date_rule: 'A' | 'B'
  frequency: 'daily' | 'weekly' | 'monthly'
  backfill_start: string | null
  enabled: boolean
  last_refresh_at: string | null
  last_status: string | null
  last_error: string | null
  updated_at: string | null
  latest_trade_date: string | null
  stale: boolean
}

/** Detail response = catalog row + tail preview (last 30 data rows, ascending). */
export interface ExtIndCatalogDetail extends ExtIndCatalogEntry {
  preview: { trade_date: string; value: number | null; available_date: string }[]
}

/** PATCH whitelist. `pinned_source` remains a reserved field the backend
 *  rejects; `source_config` (P3-4) is custom_http-only and replaces the whole
 *  stored config when provided. Absent fields keep their stored values. */
export interface ExtIndCatalogPatch {
  display_name?: string
  unit?: string
  description?: string
  frequency?: 'daily' | 'weekly' | 'monthly'
  enabled?: boolean
  available_date_rule?: 'A' | 'B'
  backfill_start?: string
  source_config?: CustomHTTPSourceConfig
}

/** DELETE response. `purged_data` echoes the purge_data query param. */
export interface ExtIndDeleteResult {
  deleted: string
  purged_data: boolean
  detail: string
}

// ── Built-in catalog / refresh / runs types (P2) ────────────────────────────

/** One built-in indicator as returned by GET /external-indicators/builtins. */
export interface ExtIndBuiltin {
  indicator_key: string
  display_name: string
  unit: string | null
  description: string | null
  available_date_rule: 'A' | 'B'
  frequency: 'daily' | 'weekly' | 'monthly'
  default_backfill_years: number
  candidates: { name: string; ready: boolean }[]
  enabled: boolean
}

/** POST /builtins/{key}/enable response = catalog row + backfill payload.
 *  `backfill` is "pending" when the refresh module is not yet wired, else a
 *  RefreshSummary-shaped object (loose typing: backend returns either). */
export interface ExtIndEnableResult extends ExtIndCatalogEntry {
  backfill: 'pending' | Record<string, unknown>
}

/** Per-key result row inside a RefreshSummary. */
export interface ExtIndRefreshResult {
  indicator_key: string
  source: string | null
  status: 'ok' | 'error' | 'skipped'
  rows_fetched: number
  rows_upserted: number
  error: string | null
  range_start: string | null
  range_end: string | null
}

/** POST /external-indicators/refresh response. */
export interface ExtIndRefreshSummary {
  trigger: string
  started_at: string
  finished_at: string | null
  results: ExtIndRefreshResult[]
  ok: number
  error: number
}

/** One refresh_log row as returned by GET /external-indicators/runs.
 *  `interrupted` is a rendering-layer flag (status='running' stale > 1h). */
export interface ExtIndRun {
  run_id: number
  indicator_key: string
  source_name: string | null
  started_at: string | null
  finished_at: string | null
  status: string
  trigger: string | null
  range_start: string | null
  range_end: string | null
  rows_fetched: number | null
  rows_upserted: number | null
  error: string | null
  interrupted: boolean
}


// ── External indicators CSV import ──────────────────────────────────────────

export interface ExtIndPreview {
  columns: string[]
  rows: Record<string, string | number | null>[]
  total_rows: number
}

export interface ExtIndImportReport {
  total: number
  inserted: number
  deduped: number
  skipped: number
  skipped_reasons: string[]
  warnings: string[]
}

export interface ExtIndImportConfig {
  source: string
  indicator_key: string
  column_map: Record<string, string>
  /** A = available on trade_date, B = next trading day (conservative default) */
  available_date_rule: 'A' | 'B'
}

// ── Custom HTTP source types (P3) ──────────────────────────────────────────

/** Mirror of the backend CustomHTTPConfig pydantic model
 *  (datahub/pipelines/indicator_sources/http_config.py). Sent verbatim as
 *  `source_config` on create / PATCH / test. */
export interface CustomHTTPSourceConfig {
  method: 'GET' | 'POST'
  url_template: string
  /** Values may reference server-side env vars as `${VAR}` (rendered by the
   *  backend, never stored as secrets in the browser). */
  headers: Record<string, string>
  params: Record<string, string>
  date_param_style: 'yyyymmdd' | 'yyyy-mm-dd' | 'none'
  extraction: {
    type: 'jsonpath'
    records_path: string
    field_map: Record<string, string>
  }
  allow_insecure_http: boolean
  timeout_connect_sec: number
  timeout_total_sec: number
  max_bytes: number
}

/** POST /external-indicators/catalog (custom_http creation) body. */
export interface ExtIndCustomCreateBody {
  indicator_key: string
  display_name?: string
  unit?: string
  description?: string
  frequency: 'daily' | 'weekly' | 'monthly'
  available_date_rule: 'A' | 'B'
  backfill_start?: string
  source_name?: string
  source_config: CustomHTTPSourceConfig
}

/** POST /external-indicators/test success response. */
export interface ExtIndTestResult {
  sample: { trade_date: string; value: number | null }[]
  diagnostics: {
    resolved_url: string
    status: string
    rows_parsed: number
    field_map_hit: boolean
  }
}
