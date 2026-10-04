/**
 * B2 signals tab — tri-state 404 copy, normal state (date picker + detail
 * table with dynamic factor columns), missing-factors banner and
 * persistence-failure banner.
 */

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { BacktestSignalsTab } from '../backtest-tabs/BacktestSignalsTab'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    backtestsApi: {
      ...actual.backtestsApi,
      getSignalDates: vi.fn(),
      getSignalDetails: vi.fn(),
      get: vi.fn(),
    },
  }
})

const mockedDates = vi.mocked(backtestsApi.getSignalDates)
const mockedDetails = vi.mocked(backtestsApi.getSignalDetails)
const mockedGet = vi.mocked(backtestsApi.get)

function renderTab() {
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/signals" element={<BacktestSignalsTab />} />
    </Routes>,
    { route: '/backtests/run-123/signals' },
  )
}

function reasonError(reason: string): ApiError {
  return new ApiError('HTTP 404', { status: 404, details: { reason } })
}

const DATES_PAYLOAD = { dates: ['2026-01-05', '2026-01-12'], total: 2 }

const DETAILS_PAYLOAD = {
  items: [
    { asset_id: 'SSE:600036', score: 1.2345, rank: 1, action: 'enter',
      prev_weight: 0.0, new_weight: 0.5, factor_scores: { mom: 0.8, value: 0.4345 } },
    { asset_id: 'SSE:000001', score: null, rank: null, action: 'exit',
      prev_weight: 0.5, new_weight: 0.0, factor_scores: {} },
  ],
  total: 2,
  page: 0,
  page_size: 50,
}

describe('BacktestSignalsTab — tri-state 404', () => {
  beforeEach(() => {
    mockedDates.mockReset()
    mockedDetails.mockReset()
    mockedGet.mockReset()
    mockedGet.mockResolvedValue({ tags: '{}' } as never)
  })

  it('renders run_not_found copy', async () => {
    mockedDates.mockRejectedValue(reasonError('run_not_found'))
    renderTab()
    expect(await screen.findByTestId('signals-run_not_found')).toBeInTheDocument()
    expect(screen.getByText('回测运行不存在')).toBeInTheDocument()
  })

  it('renders unsupported_strategy_type copy (StaticTopN etc.)', async () => {
    mockedDates.mockRejectedValue(reasonError('unsupported_strategy_type'))
    renderTab()
    expect(await screen.findByTestId('signals-unsupported_strategy_type')).toBeInTheDocument()
    expect(screen.getByText('该策略类型不产生信号明细')).toBeInTheDocument()
  })

  it('renders no_signal_details copy (old run / sink failure)', async () => {
    mockedDates.mockRejectedValue(reasonError('no_signal_details'))
    renderTab()
    expect(await screen.findByTestId('signals-no_signal_details')).toBeInTheDocument()
    expect(screen.getByText('该回测无信号明细')).toBeInTheDocument()
  })

  it('falls back to generic error rendering for non-tri-state errors', async () => {
    mockedDates.mockRejectedValue(new ApiError('HTTP 500', { status: 500 }))
    renderTab()
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByTestId('signals-no_signal_details')).not.toBeInTheDocument()
  })
})

describe('BacktestSignalsTab — normal state', () => {
  beforeEach(() => {
    mockedDates.mockReset()
    mockedDetails.mockReset()
    mockedGet.mockReset()
    mockedGet.mockResolvedValue({ tags: '{}' } as never)
    mockedDates.mockResolvedValue(DATES_PAYLOAD as never)
    mockedDetails.mockResolvedValue(DETAILS_PAYLOAD as never)
  })

  it('renders date picker (default earliest date) and detail rows with dynamic factor columns', async () => {
    renderTab()

    expect(await screen.findByTestId('signals-date-select')).toBeInTheDocument()
    const select = screen.getByTestId('signals-date-select') as HTMLSelectElement
    expect(select.value).toBe('2026-01-05') // earliest date default

    // detail rows: asset + factor column header from factor_scores keys
    expect(await screen.findByText('SSE:600036')).toBeInTheDocument()
    expect(screen.getByText('mom')).toBeInTheDocument()
    expect(screen.getByText('value')).toBeInTheDocument()
    // action badge + weights
    expect(screen.getByText('买入')).toBeInTheDocument()
    expect(screen.getByText('退出')).toBeInTheDocument()
    expect(screen.getAllByText('50.00%').length).toBeGreaterThan(0)
  })
})

describe('BacktestSignalsTab — banners', () => {
  beforeEach(() => {
    mockedDates.mockReset()
    mockedDetails.mockReset()
    mockedGet.mockReset()
    mockedDates.mockResolvedValue(DATES_PAYLOAD as never)
    mockedDetails.mockResolvedValue(DETAILS_PAYLOAD as never)
  })

  it('shows the missing-factors warning banner when tags.signals_missing_factors is set', async () => {
    mockedGet.mockResolvedValue({
      tags: JSON.stringify({ signals_missing_factors: JSON.stringify(['ghost', 'alpha']) }),
    } as never)
    renderTab()

    const banner = await screen.findByTestId('signals-missing-factors')
    expect(banner).toBeInTheDocument()
    expect(screen.getByTestId('signals-missing-factors-list')).toHaveTextContent('alpha, ghost')
    expect(screen.getByText('部分因子未物化')).toBeInTheDocument()
  })

  it('shows the persistence-failure banner when tags.signals_persisted is false', async () => {
    mockedGet.mockResolvedValue({
      tags: JSON.stringify({
        signals_persisted: false,
        signals_error: 'RuntimeError: duckdb exploded',
      }),
    } as never)
    renderTab()

    expect(await screen.findByTestId('signals-persist-failure')).toBeInTheDocument()
    expect(screen.getByText('信号明细落盘失败')).toBeInTheDocument()
    expect(screen.getByTestId('signals-persist-error')).toHaveTextContent('duckdb exploded')
  })

  it('renders no banners for a clean run', async () => {
    mockedGet.mockResolvedValue({ tags: '{}' } as never)
    renderTab()

    expect(await screen.findByTestId('signals-date-select')).toBeInTheDocument()
    expect(screen.queryByTestId('signals-missing-factors')).not.toBeInTheDocument()
    expect(screen.queryByTestId('signals-persist-failure')).not.toBeInTheDocument()
  })
})
