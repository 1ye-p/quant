import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { ExternalIndicatorsPage } from '../ExternalIndicatorsPage'

vi.mock('@/lib/api', () => ({
  datasetsApi: {
    listExtIndCatalog: vi.fn().mockResolvedValue({
      total: 2,
      items: [
        {
          indicator_key: 'margin_balance',
          display_name: '两融余额',
          unit: null,
          description: null,
          source_type: 'csv',
          source_name: 'manual',
          pinned_source: null,
          source_config: null,
          available_date_rule: 'B' as const,
          frequency: 'daily' as const,
          backfill_start: null,
          enabled: true,
          last_refresh_at: '2026-09-28T10:00:00',
          last_status: 'ok',
          last_error: null,
          updated_at: null,
          latest_trade_date: '2026-09-25',
          stale: false,
        },
        {
          indicator_key: 'north_flow',
          display_name: '北向资金',
          unit: null,
          description: null,
          source_type: 'csv',
          source_name: 'manual',
          pinned_source: null,
          source_config: null,
          available_date_rule: 'B' as const,
          frequency: 'weekly' as const,
          backfill_start: null,
          enabled: true,
          last_refresh_at: null,
          last_status: 'never_run',
          last_error: null,
          updated_at: null,
          latest_trade_date: null,
          stale: true,
        },
      ],
    }),
  },
}))

describe('ExternalIndicatorsPage', () => {
  it('renders page title and catalog rows with stale badge', async () => {
    renderWithProviders(<ExternalIndicatorsPage />)
    expect(screen.getByText('外部指标管理')).toBeInTheDocument()
    expect(await screen.findByText('margin_balance')).toBeInTheDocument()
    expect(screen.getByText('north_flow')).toBeInTheDocument()
    // stale badge only on the stale row
    expect(screen.getByText('过期')).toBeInTheDocument()
    // four tabs
    expect(screen.getByText('指标列表')).toBeInTheDocument()
    expect(screen.getByText('CSV 导入')).toBeInTheDocument()
    expect(screen.getByText('内置目录')).toBeInTheDocument()
    expect(screen.getByText('自定义源')).toBeInTheDocument()
  })
})
