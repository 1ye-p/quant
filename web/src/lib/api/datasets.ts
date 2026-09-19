/**
 * cQuant API — Datasets domain.
 */

import { api, request, type RequestConfig } from './client'

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
