import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import {
  parseReportSegments,
  ChartRenderer,
  MetricCardsChart,
  ReportContent,
} from '../reportCharts'

// Recharts relies on ResizeObserver / SVG measuring that jsdom lacks — stub the
// chart primitives so we can assert on container/structure.
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

const marker = (type: string, payload: Record<string, unknown>) =>
  `[CHART:${type}:${JSON.stringify(payload)}]`

const metricPayload = {
  chart_type: 'metric_cards',
  title: 'Key Metrics',
  data: [
    { label: 'Sharpe', value: 1.23, delta: 0.1 },
    { label: 'MaxDD', value: '-8.5%' },
  ],
}

const linePayload = {
  chart_type: 'line',
  title: 'NAV',
  data: [
    { date: '2025-01-01', nav: 1.0 },
    { date: '2025-01-02', nav: 1.01 },
  ],
  config: { x_key: 'date', y_keys: ['nav'] },
}

const barPayload = {
  chart_type: 'bar',
  title: 'Factor IC',
  data: [
    { category: 'SMA', value: 0.15 },
    { category: 'Rsi', value: 0.08 },
  ],
  config: { x_key: 'category', y_key: 'value' },
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('parseReportSegments', () => {
  it('splits text around a single known marker', () => {
    const segs = parseReportSegments(`before ${marker('line', linePayload)} after`)
    expect(segs).toHaveLength(3)
    expect(segs[0]).toEqual({ type: 'text', value: 'before ' })
    expect(segs[1].type).toBe('chart')
    expect(segs[2]).toEqual({ type: 'text', value: ' after' })
  })

  it('parses all three marker types', () => {
    for (const payload of [metricPayload, linePayload, barPayload]) {
      const segs = parseReportSegments(marker(payload.chart_type, payload))
      expect(segs).toHaveLength(1)
      expect(segs[0].type).toBe('chart')
      if (segs[0].type === 'chart') expect(segs[0].payload).toEqual(payload)
    }
  })

  it('keeps unknown chart_type verbatim as text', () => {
    const m = marker('scatter', linePayload)
    const segs = parseReportSegments(`x ${m} y`)
    expect(segs).toEqual([{ type: 'text', value: `x ${m} y` }])
  })

  it('degrades malformed JSON to verbatim text + console.warn, no crash', () => {
    const m = '[CHART:line:{"chart_type":"line", BADJSON}]'
    const segs = parseReportSegments(`a ${m} b`)
    expect(segs).toEqual([{ type: 'text', value: `a ${m} b` }])
    expect(console.warn).toHaveBeenCalled()
  })

  it('degrades schema-mismatch payload (missing data array) to text', () => {
    const m = '[CHART:line:{"chart_type":"line","title":"t","data":"oops"}]'
    const segs = parseReportSegments(m)
    expect(segs).toEqual([{ type: 'text', value: m }])
    expect(console.warn).toHaveBeenCalled()
  })

  it('handles multiple markers interleaved with text', () => {
    const segs = parseReportSegments(
      `intro\n${marker('metric_cards', metricPayload)}\nmid\n${marker('bar', barPayload)}\nend`
    )
    expect(segs.map(s => s.type)).toEqual(['text', 'chart', 'text', 'chart', 'text'])
    if (segs[1].type === 'chart') expect(segs[1].payload.chart_type).toBe('metric_cards')
    if (segs[3].type === 'chart') expect(segs[3].payload.chart_type).toBe('bar')
    expect(segs[4].type === 'text' && segs[4].value).toBe('\nend')
  })

  it('handles JSON containing nested brackets inside strings', () => {
    const payload = {
      chart_type: 'metric_cards',
      title: 'Notes [v2]',
      data: [{ label: 'tag[0]', value: '[x]' }],
    }
    const segs = parseReportSegments(marker('metric_cards', payload))
    expect(segs).toHaveLength(1)
    expect(segs[0].type === 'chart' && segs[0].payload.title).toBe('Notes [v2]')
  })

  it('returns plain text unchanged when no markers', () => {
    expect(parseReportSegments('hello world')).toEqual([{ type: 'text', value: 'hello world' }])
  })
})

describe('chart components', () => {
  it('MetricCardsChart renders card labels and values', () => {
    render(<MetricCardsChart title={metricPayload.title} data={metricPayload.data} />)
    expect(screen.getByText('Sharpe')).toBeInTheDocument()
    expect(screen.getByText('1.23')).toBeInTheDocument()
    expect(screen.getByText('MaxDD')).toBeInTheDocument()
    expect(screen.getByText('+0.1')).toBeInTheDocument()
  })

  it('ChartRenderer maps line payload to LineChart', () => {
    render(<ChartRenderer payload={linePayload as never} />)
    expect(screen.getByTestId('recharts-linechart')).toBeInTheDocument()
    expect(screen.getByText('NAV')).toBeInTheDocument()
  })

  it('ChartRenderer maps bar payload to BarChart', () => {
    render(<ChartRenderer payload={barPayload as never} />)
    expect(screen.getByTestId('recharts-barchart')).toBeInTheDocument()
    expect(screen.getByText('Factor IC')).toBeInTheDocument()
  })

  it('ChartRenderer maps metric_cards payload', () => {
    render(<ChartRenderer payload={metricPayload as never} />)
    expect(screen.getByText('Key Metrics')).toBeInTheDocument()
    expect(screen.getByText('Sharpe')).toBeInTheDocument()
  })

  it('ChartRenderer returns null for unknown type', () => {
    const { container } = render(<ChartRenderer payload={{ chart_type: 'nope', title: 'x', data: [] }} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('ReportContent (segment pipeline)', () => {
  it('renders chart segments inline with markdown text segments', async () => {
    render(
      <ReportContent
        content={`## Header\n${marker('metric_cards', metricPayload)}\n**bold** text`}
      />
    )
    expect(await screen.findByText('Sharpe', {}, { timeout: 8000 })).toBeInTheDocument()
    expect(await screen.findByText('bold', {}, { timeout: 8000 })).toBeInTheDocument()
  })

  it('renders malformed markers without crashing (kept as text)', async () => {
    render(<ReportContent content={'[CHART:line:{broken] `code`'} />)
    expect(await screen.findByText('code', {}, { timeout: 8000 })).toBeInTheDocument()
  })
})
