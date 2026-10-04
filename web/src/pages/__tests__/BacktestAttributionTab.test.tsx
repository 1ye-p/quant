/**
 * B1 attribution tab — tri-state 404 copy, normal-state cards/tables,
 * and the empty-sector placeholder.
 */

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { BacktestAttributionTab } from '../backtest-tabs/BacktestAttributionTab'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    backtestsApi: {
      ...actual.backtestsApi,
      getAttribution: vi.fn(),
    },
  }
})

const mockedGet = vi.mocked(backtestsApi.getAttribution)

function renderTab() {
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/attribution" element={<BacktestAttributionTab />} />
    </Routes>,
    { route: '/backtests/run-123/attribution' },
  )
}

function reasonError(reason: string): ApiError {
  return new ApiError('HTTP 404', { status: 404, details: { reason } })
}

const NORMAL_PAYLOAD = {
  analysis_run_id: 'ar-1',
  summary: {
    total_return: 0.12,
    benchmark_return: 0.08,
    active_return: 0.04,
    allocation: 0.01,
    selection: 0.02,
    interaction: 0.01,
  },
  periods: [
    { date: '2026-01-05', allocation: 0.005, selection: 0.01, interaction: 0.002 },
    { date: '2026-01-12', allocation: 0.005, selection: 0.01, interaction: 0.008 },
  ],
  sectors: [
    { sector: 'Finance', port_weight: 0.6, bench_weight: 0.5, port_return: 0.15, bench_return: 0.1 },
    { sector: 'Tech', port_weight: 0.4, bench_weight: 0.5, port_return: 0.08, bench_return: 0.05 },
  ],
}

describe('BacktestAttributionTab — tri-state 404', () => {
  beforeEach(() => {
    mockedGet.mockReset()
  })

  it('renders run_not_found copy', async () => {
    mockedGet.mockRejectedValue(reasonError('run_not_found'))
    renderTab()
    expect(await screen.findByTestId('attribution-run_not_found')).toBeInTheDocument()
    expect(screen.getByText('回测运行不存在')).toBeInTheDocument()
  })

  it('renders no_analysis_run copy', async () => {
    mockedGet.mockRejectedValue(reasonError('no_analysis_run'))
    renderTab()
    expect(await screen.findByTestId('attribution-no_analysis_run')).toBeInTheDocument()
    expect(screen.getByText('该回测尚未运行分析套件')).toBeInTheDocument()
  })

  it('renders no_attribution copy (production primary path)', async () => {
    mockedGet.mockRejectedValue(reasonError('no_attribution'))
    renderTab()
    expect(await screen.findByTestId('attribution-no_attribution')).toBeInTheDocument()
    expect(screen.getByText('无基准或无行业映射，未产生归因')).toBeInTheDocument()
  })

  it('falls back to generic error rendering for non-tri-state errors', async () => {
    mockedGet.mockRejectedValue(new ApiError('HTTP 500', { status: 500 }))
    renderTab()
    await waitFor(() => expect(mockedGet).toHaveBeenCalled())
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByTestId('attribution-no_attribution')).not.toBeInTheDocument()
  })
})

describe('BacktestAttributionTab — normal state', () => {
  beforeEach(() => {
    mockedGet.mockReset()
  })

  it('renders six metric cards and both tables with derived sector effects', async () => {
    mockedGet.mockResolvedValue(NORMAL_PAYLOAD as never)
    renderTab()

    // Summary cards
    expect(await screen.findByText('Brinson 归因总览')).toBeInTheDocument()
    expect(screen.getByText('12.00%')).toBeInTheDocument()   // total return
    expect(screen.getByText('8.00%')).toBeInTheDocument()    // benchmark
    expect(screen.getByText('4.00%')).toBeInTheDocument()    // active

    // Sector table rows — Finance: alloc 0.1·0.02 + selec 0.5·0.05 + inter 0.1·0.05 = 3.20%
    expect(await screen.findByText('Finance')).toBeInTheDocument()
    expect(screen.getByText('Tech')).toBeInTheDocument()
    expect(screen.getAllByText('3.20%').length).toBeGreaterThan(0)
    expect(screen.getByText('合计贡献')).toBeInTheDocument()

    // Period table
    expect(screen.getByText('2026-01-05')).toBeInTheDocument()
    expect(screen.getByText('分期明细')).toBeInTheDocument()
  })

  it('shows the empty-sector placeholder when sectors is [] (distinct from tri-state)', async () => {
    mockedGet.mockResolvedValue({ ...NORMAL_PAYLOAD, sectors: [] } as never)
    renderTab()
    expect(await screen.findByTestId('attribution-sectors-empty')).toBeInTheDocument()
    expect(screen.getByText('无行业映射数据')).toBeInTheDocument()
    expect(screen.queryByTestId('attribution-no_attribution')).not.toBeInTheDocument()
  })
})
