/**
 * B4b-2 positions tab — tri-state 404 copy, both views (weight / industry),
 * Top-N + window controls, board-granularity note, `__other__` / `未知`
 * display keys, and the latest-date holdings table.
 */

// Mock ResizeObserver for recharts
globalThis.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { ApiError } from '@/lib/api/errors'
import { BacktestPositionsTab } from '../backtest-tabs/BacktestPositionsTab'

// jsdom gives ResponsiveContainer a zero-size box, so chart innards never
// render. Stub the container to pass children through.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  }
})

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    backtestsApi: {
      ...actual.backtestsApi,
      getPositionsSeries: vi.fn(),
    },
  }
})

const mockedSeries = vi.mocked(backtestsApi.getPositionsSeries)

function renderTab() {
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/positions" element={<BacktestPositionsTab />} />
    </Routes>,
    { route: '/backtests/run-123/positions' },
  )
}

function reasonError(reason: string): ApiError {
  return new ApiError('HTTP 404', { status: 404, details: { reason } })
}

const WEIGHT_PAYLOAD = (topN: number) => ({
  run_id: 'run-123',
  metric: 'weight' as const,
  top_n: topN,
  series: [
    {
      trade_date: '2026-01-05',
      positions: [
        { asset_id: 'SSE:600036', weight: 0.5, industry: 'Main' },
        { asset_id: 'SZSE:300750', weight: 0.3, industry: 'ChiNext' },
        { asset_id: '__other__', weight: 0.2, industry: null },
      ],
    },
    {
      trade_date: '2026-01-12',
      positions: [
        { asset_id: 'SSE:600036', weight: 0.6, industry: 'Main' },
        { asset_id: 'SZSE:300750', weight: 0.2, industry: '未知' },
        { asset_id: '__other__', weight: 0.2, industry: null },
      ],
    },
  ],
})

const INDUSTRY_PAYLOAD = {
  run_id: 'run-123',
  metric: 'industry' as const,
  top_n: 10,
  series: [
    { trade_date: '2026-01-05', weights: { Main: 0.5, ChiNext: 0.3, 未知: 0.2 } },
    { trade_date: '2026-01-12', weights: { Main: 0.7, 未知: 0.3 } },
  ],
}

/** Route mocks by (metric, topN): default weight@10 + table weight@50. */
function mockByMetric(metric: 'weight' | 'industry') {
  mockedSeries.mockImplementation(
    async (_id: string, m: 'weight' | 'industry', topN?: number) => {
      if (m === 'industry') return INDUSTRY_PAYLOAD as never
      return WEIGHT_PAYLOAD(topN ?? 10) as never
    },
  )
  void metric
}

describe('BacktestPositionsTab — tri-state 404', () => {
  beforeEach(() => { mockedSeries.mockReset() })

  it('renders run_not_found copy', async () => {
    mockedSeries.mockRejectedValue(reasonError('run_not_found'))
    renderTab()
    expect(await screen.findByTestId('positions-run_not_found')).toBeInTheDocument()
    expect(screen.getByText('回测运行不存在')).toBeInTheDocument()
  })

  it('renders no_position_data copy (legacy run, re-run needed)', async () => {
    mockedSeries.mockRejectedValue(reasonError('no_position_data'))
    renderTab()
    expect(await screen.findByTestId('positions-no_position_data')).toBeInTheDocument()
    expect(screen.getByText('该回测无持仓数据')).toBeInTheDocument()
  })

  it('falls back to generic error rendering for non-404 errors', async () => {
    mockedSeries.mockRejectedValue(new ApiError('HTTP 500', { status: 500 }))
    renderTab()
    expect(await screen.findByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByTestId('positions-no_position_data')).not.toBeInTheDocument()
  })
})

describe('BacktestPositionsTab — weight view', () => {
  beforeEach(() => { mockedSeries.mockReset(); mockByMetric('weight') })

  it('renders stacked chart, Top-N note, and latest-date holdings table', async () => {
    renderTab()

    expect(await screen.findByTestId('positions-chart')).toBeInTheDocument()
    // Top-N semantics note (raw-weight ordering, long-only context)
    expect(screen.getByTestId('positions-note').textContent).toContain('按权重原值降序')

    // Latest date table: rows of 2026-01-12 + as-of label
    expect(await screen.findByText('SSE:600036')).toBeInTheDocument()
    expect(screen.getByText('截至 2026-01-12')).toBeInTheDocument()
    expect(screen.getByText('60.00%')).toBeInTheDocument()
  })

  it('shows the Top-N selector only in weight view (5/10/20)', async () => {
    renderTab()
    const select = await screen.findByTestId('positions-topn-select') as HTMLSelectElement
    expect(select.value).toBe('10')
    const options = [...select.options].map(o => o.value)
    expect(options).toEqual(['5', '10', '20'])
  })

  it('renders __other__ as 其他 in the holdings table', async () => {
    renderTab()
    expect(await screen.findByText('其他')).toBeInTheDocument()
    expect(screen.queryByText('__other__')).not.toBeInTheDocument()
  })

  it('window control slices the series (末 1/4 keeps last date only)', async () => {
    renderTab()
    await screen.findByTestId('positions-chart')
    // Default 全期; switch to last quarter — table stays latest date regardless
    const win = screen.getByTestId('positions-window-select') as HTMLSelectElement
    await userEvent.selectOptions(win, 'quarter')
    expect(win.value).toBe('quarter')
    expect(await screen.findByText('截至 2026-01-12')).toBeInTheDocument()
  })
})

describe('BacktestPositionsTab — industry view', () => {
  beforeEach(() => { mockedSeries.mockReset(); mockByMetric('industry') })

  /** Render and switch to the industry view (default is weight). */
  async function renderIndustryView() {
    renderTab()
    await screen.findByTestId('positions-chart')
    await userEvent.click(screen.getByTestId('positions-view-industry'))
  }

  it('renders board-granularity note (listing board, not SW industry)', async () => {
    await renderIndustryView()
    expect(screen.getByTestId('positions-chart')).toBeInTheDocument()
    const note = screen.getByTestId('positions-note').textContent ?? ''
    expect(note).toContain('上市板块粒度')
    expect(note).toContain('非申万行业分类')
  })

  it('hides the Top-N selector in industry view', async () => {
    await renderIndustryView()
    expect(screen.queryByTestId('positions-topn-select')).not.toBeInTheDocument()
  })

  it('renders 未知 display key for backend-normalized empty industry', async () => {
    // Switch to industry to prove both views resolve, then check the holdings
    // table industry column (latest weight-shape date carries industry='未知').
    await renderIndustryView()
    await userEvent.click(screen.getByTestId('positions-view-weight'))
    expect(await screen.findByText('未知')).toBeInTheDocument()
    expect(screen.getByText('截至 2026-01-12')).toBeInTheDocument()
  })

  it('toggles views weight ↔ industry', async () => {
    renderTab()
    await screen.findByTestId('positions-chart')
    expect(screen.getByTestId('positions-note').textContent).not.toContain('上市板块粒度')
    await userEvent.click(screen.getByTestId('positions-view-industry'))
    expect(screen.getByTestId('positions-note').textContent).toContain('上市板块粒度')
    await userEvent.click(screen.getByTestId('positions-view-weight'))
    expect(screen.getByTestId('positions-note').textContent).toContain('按权重原值降序')
  })
})

describe('fillStackRows — churn zero-fill (review I3 regression)', () => {
  it('fills absent stack keys with 0 on membership-churn days', async () => {
    const { fillStackRows } = await import('../backtest-tabs/BacktestPositionsTab')
    const raw: Array<Record<string, number | string>> = [
      { date: '2026-01-05', SSE: 0.6, SZSE: 0.4 },
      { date: '2026-01-06', SSE: 1.0 }, // SZSE dropped out of the top-N
    ]
    const out = fillStackRows(raw, ['SSE', 'SZSE', '__other__'])
    expect(out[0]).toEqual({ date: '2026-01-05', SSE: 0.6, SZSE: 0.4, __other__: 0 })
    expect(out[1]).toEqual({ date: '2026-01-06', SSE: 1.0, SZSE: 0, __other__: 0 })
  })
})
