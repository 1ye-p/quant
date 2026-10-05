/**
 * B3-2 report tab rendering:
 * - markdown + three chart marker types rendered inline
 * - unknown marker preserved with code-block appearance
 * - render failure falls back to raw <pre> (no white screen)
 * - raw-text / rendered toggle
 * - markdown renderer lazy-loaded (React.lazy structure)
 */

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { BacktestReportTab } from '../backtest-tabs/BacktestReportTab'
import { LazyMarkdown } from '@/lib/reportCharts'

// Recharts relies on ResizeObserver / SVG measuring that jsdom lacks — stub the
// chart primitives so we can assert on containers (same approach as B3-1 tests).
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: ({ data }: { data: unknown[] }) => <div data-testid="recharts-linechart" data-len={data.length} />,
  Line: () => null,
  BarChart: ({ data }: { data: unknown[] }) => <div data-testid="recharts-barchart" data-len={data.length} />,
  Bar: () => null,
  PieChart: () => <div data-testid="recharts-piechart" />,
  Pie: () => null,
  Cell: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}))

// Control knob for the lazy markdown module: default renders via the REAL
// react-markdown pipeline; tests can flip throwOnRender to simulate a broken
// renderer and exercise the fallback boundary.
const mdState = vi.hoisted(() => ({ throwOnRender: false }))
vi.mock('@/lib/reportMarkdown', async () => {
  const actual = await vi.importActual<typeof import('@/lib/reportMarkdown')>('@/lib/reportMarkdown')
  return {
    MarkdownRenderer: (props: { content: string }) => {
      if (mdState.throwOnRender) throw new Error('markdown renderer exploded')
      return <actual.MarkdownRenderer {...props} />
    },
  }
})

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    backtestsApi: {
      ...actual.backtestsApi,
      getReport: vi.fn(),
      generateReport: vi.fn(),
      pollJob: vi.fn(),
    },
  }
})

const mockedGetReport = vi.mocked(backtestsApi.getReport)

const marker = (type: string, payload: Record<string, unknown>) =>
  `[CHART:${type}:${JSON.stringify(payload)}]`

const REPORT_MD = [
  '## 回测研究报告',
  '',
  '概览段落，**关键结论**如下：',
  '',
  marker('metric_cards', {
    chart_type: 'metric_cards',
    title: 'Key Metrics',
    data: [
      { label: 'Sharpe', value: 1.23, delta: 0.1 },
      { label: 'MaxDD', value: '-8.5%' },
    ],
  }),
  '',
  marker('line', {
    chart_type: 'line',
    title: 'NAV',
    data: [
      { date: '2025-01-01', nav: 1.0 },
      { date: '2025-01-02', nav: 1.01 },
    ],
    config: { x_key: 'date', y_keys: ['nav'] },
  }),
  '',
  marker('bar', {
    chart_type: 'bar',
    title: 'Factor IC',
    data: [
      { category: 'SMA', value: 0.15 },
      { category: 'Rsi', value: 0.08 },
    ],
    config: { x_key: 'category', y_key: 'value' },
  }),
  '',
  '结尾段落。',
].join('\n')

function mockReport(contentMd: string) {
  mockedGetReport.mockResolvedValue({
    report_id: 'r0123456789abcdef',
    created_at: '2026-10-05T10:00:00',
    content_md: contentMd,
  } as Awaited<ReturnType<typeof backtestsApi.getReport>>)
}

function renderTab() {
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/report" element={<BacktestReportTab />} />
    </Routes>,
    { route: '/backtests/run-1/report' },
  )
}

beforeEach(() => {
  mdState.throwOnRender = false
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  mdState.throwOnRender = false
})

describe('BacktestReportTab — rendered view', () => {
  it('renders markdown headings/text plus all three chart marker types inline', async () => {
    mockReport(REPORT_MD)
    renderTab()

    // markdown (lazy-loaded) — heading + bold text from the real renderer
    expect(await screen.findByRole('heading', { name: '回测研究报告' }, { timeout: 8000 })).toBeInTheDocument()
    expect(screen.getByText('关键结论')).toBeInTheDocument()

    // metric_cards → cards (no recharts), line → LineChart, bar → BarChart
    expect(screen.getByText('Sharpe')).toBeInTheDocument()
    expect(screen.getByText('1.23')).toBeInTheDocument()
    expect(screen.getByTestId('recharts-linechart')).toBeInTheDocument()
    expect(screen.getByTestId('recharts-barchart')).toBeInTheDocument()
  }, 20000)

  it('keeps unknown chart markers visible with code-block appearance', async () => {
    const unknown = marker('scatter', {
      chart_type: 'scatter',
      title: 'Scatter',
      data: [{ x: 1, y: 2 }],
    })
    mockReport(`## 报告\n\n${unknown}\n\n正文。`)
    renderTab()

    const el = await screen.findByText(/\[CHART:scatter:/, {}, { timeout: 8000 })
    // fenced by the rendering layer → react-markdown renders it as a code block
    expect(el.closest('pre')).not.toBeNull()
    expect(el.closest('code')).not.toBeNull()
    expect(screen.getByText('正文。')).toBeInTheDocument()
  }, 20000)
})

describe('BacktestReportTab — fallback & toggle', () => {
  it('falls back to raw <pre> when the render pipeline throws (no white screen)', async () => {
    mockReport(REPORT_MD)
    mdState.throwOnRender = true
    renderTab()

    // fallback pre carries the full raw markdown — assert on a marker fragment
    const pre = await screen.findByTestId('report-fallback-pre', {}, { timeout: 8000 })
    expect(pre).toBeInTheDocument()
    expect(pre.textContent).toContain('[CHART:metric_cards:')
    expect(pre.textContent).toContain('## 回测研究报告')

    // page chrome intact (no crash to error screen)
    expect(screen.getByText('AI 研究报告')).toBeInTheDocument()
    // rendered-path content not shown
    expect(screen.queryByTestId('recharts-linechart')).not.toBeInTheDocument()
  }, 20000)

  it('toggles between rendered and raw text views', async () => {
    mockReport(REPORT_MD)
    renderTab()

    // default: rendered view, no raw pre
    expect(await screen.findByRole('heading', { name: '回测研究报告' }, { timeout: 8000 })).toBeInTheDocument()
    expect(screen.queryByTestId('report-raw-pre')).not.toBeInTheDocument()

    // → raw
    await userEvent.click(screen.getByTestId('report-view-toggle'))
    const pre = screen.getByTestId('report-raw-pre')
    expect(pre.textContent).toBe(REPORT_MD)
    expect(screen.queryByRole('heading', { name: '回测研究报告' })).not.toBeInTheDocument()
    expect(screen.getByText('查看渲染视图')).toBeInTheDocument()

    // → back to rendered
    await userEvent.click(screen.getByTestId('report-view-toggle'))
    expect(screen.queryByTestId('report-raw-pre')).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '回测研究报告' }, { timeout: 8000 })).toBeInTheDocument()
    expect(screen.getByText('查看原始文本')).toBeInTheDocument()
  }, 20000)
})

describe('BacktestReportTab — lazy markdown chunk', () => {
  it('LazyMarkdown is a React.lazy exotic component backed by a dynamic import promise', () => {
    // React.lazy internals: { $$typeof: REACT_LAZY_TYPE, _payload, _init }.
    // _payload starts as the dynamic-import thenable; once earlier tests have
    // rendered it, React rewrites it to { _status, _value }. Accept either.
    const lazy = LazyMarkdown as unknown as {
      _payload: { then?: unknown; _status?: number }
      _init: unknown
    }
    expect(typeof lazy).toBe('object')
    expect(lazy._init).toBeTypeOf('function')
    expect(lazy._payload).toBeDefined()
    const pendingThenable = typeof lazy._payload.then === 'function'
    const resolved = typeof lazy._payload._status === 'number'
    expect(pendingThenable || resolved).toBe(true)
  })
})
