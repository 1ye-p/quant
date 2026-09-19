/**
 * cQuant API — Read-only data browser (structured query) domain.
 */

import { api, type RequestConfig } from './client'

export interface QueryWhereClause {
  col: string
  op: '=' | '!=' | '>' | '<' | '>=' | '<=' | 'LIKE' | 'IN' | 'IS NULL' | 'IS NOT NULL'
  val?: unknown
}

export interface QueryOrderByClause {
  col: string
  dir: 'asc' | 'desc'
}

export interface QueryBody {
  table: string
  columns?: string[] | null
  where?: QueryWhereClause[] | null
  order_by?: QueryOrderByClause[] | null
  limit?: number
}

export interface QueryResult {
  table: string
  columns: string[]
  rows: Record<string, unknown>[]
  total: number
}

export interface CoverageResult {
  start_date: string
  end_date: string
  n_assets: number
  sample_assets: string[]
  include_indices: boolean
}

export const queryApi = {
  tables: (config?: RequestConfig) =>
    api.get<{ tables: string[] }>('/query/tables', config),

  query: (body: QueryBody, config?: RequestConfig) =>
    api.post<QueryResult>('/query', body, config),

  coverage: (
    start_date: string,
    end_date: string,
    sample_limit = 20,
    include_indices = false,
    config?: RequestConfig,
  ) =>
    api.post<CoverageResult>(
      '/query/coverage',
      { start_date, end_date, sample_limit, include_indices },
      config,
    ),
}
