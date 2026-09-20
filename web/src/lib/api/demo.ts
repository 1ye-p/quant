/**
 * cQuant API — Demo / onboarding domain.
 */

import { api, type RequestConfig } from './client'

export interface DemoSeedResult {
  seeded: boolean
  dataset_version: string
  strategy_id: string
  prices: { rows: number; assets: number; start_date: string; end_date: string }
  assets_registered: number
  indicator_rows: number
  feature_set_version: string
  suggested_start_date: string
  suggested_end_date: string
}

export interface DemoStatus {
  seeded: boolean
  price_rows: number
  strategy_id: string | null
  dataset_version: string | null
}

export const demoApi = {
  seed: (config?: RequestConfig) =>
    api.post<DemoSeedResult>('/demo/seed', {}, config),

  status: (config?: RequestConfig) =>
    api.get<DemoStatus>('/demo/status', config),
}
