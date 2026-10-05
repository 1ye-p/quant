/**
 * reportCharts — parse `[CHART:{type}:{json}]` markers from AI research report
 * markdown and map them to inline chart components.
 *
 * Marker format (backend: cquant.ai_advisor.chart_generator.ChartSpec.to_marker):
 *   [CHART:metric_cards:{"chart_type":"metric_cards","title":...,"data":[...],"config":{...}}]
 *
 * Data is fully embedded in the marker JSON — no additional data requests.
 *
 * Degradation rules:
 *   - unknown chart_type  → marker kept verbatim as a text segment (B3-2 renders it as code block)
 *   - malformed JSON      → marker kept verbatim + console.warn (rendering never crashes)
 */

import { lazy, Suspense } from 'react'
import {
  ResponsiveContainer,
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend,
  BarChart, Bar,
  PieChart, Pie, Cell,
} from 'recharts'

// ── Types ────────────────────────────────────────────────────────────────────

export interface ChartPayload {
  chart_type: string
  title: string
  data: Record<string, unknown>[]
  config?: Record<string, unknown>
}

export type ReportSegment =
  | { type: 'text'; value: string }
  | { type: 'chart'; value: string; payload: ChartPayload }

/** Chart types this module knows how to render. */
const KNOWN_CHART_TYPES = new Set(['metric_cards', 'line', 'bar', 'pie'])

// ── Marker parsing ───────────────────────────────────────────────────────────

const MARKER_HEAD_RE = /\[CHART:(\w+):/g

/**
 * Extract the JSON payload extent for a marker starting at `payloadStart`.
 * Bracket-depth tracking mirrors the backend parser (chart_generator.parse_markers)
 * so JSON containing nested `[]` / `{}` is handled correctly.
 * Returns the index *after* the closing `]`, or -1 if unterminated.
 */
function findMarkerEnd(text: string, payloadStart: number): number {
  let depth = 0
  for (let i = payloadStart; i < text.length; i++) {
    const ch = text[i]
    if (ch === '{') depth += 1
    else if (ch === '}') depth -= 1
    else if (ch === ']' && depth <= 0) return i + 1
  }
  return -1
}

/**
 * Parse report markdown into ordered text/chart segments.
 * Malformed or unknown-type markers are preserved verbatim as text segments.
 */
export function parseReportSegments(content: string): ReportSegment[] {
  const segments: ReportSegment[] = []
  MARKER_HEAD_RE.lastIndex = 0
  let cursor = 0
  let match: RegExpExecArray | null

  while ((match = MARKER_HEAD_RE.exec(content)) !== null) {
    const payloadStart = match.index + match[0].length
    const end = findMarkerEnd(content, payloadStart)
    if (end === -1) continue // unterminated — leave as plain text
    const rawMarker = content.slice(match.index, end)
    const jsonStr = content.slice(payloadStart, end - 1)

    let payload: ChartPayload | null = null
    try {
      const parsed = JSON.parse(jsonStr) as ChartPayload
      if (
        parsed &&
        typeof parsed === 'object' &&
        typeof parsed.chart_type === 'string' &&
        Array.isArray(parsed.data)
      ) {
        payload = parsed
      } else {
        console.warn('[reportCharts] malformed chart marker payload (schema mismatch):', rawMarker.slice(0, 120))
      }
    } catch (err) {
      console.warn('[reportCharts] malformed chart marker JSON, keeping verbatim:', rawMarker.slice(0, 120), err)
    }

    if (payload && payload.chart_type === match[1] && KNOWN_CHART_TYPES.has(match[1])) {
      if (match.index > cursor) segments.push({ type: 'text', value: content.slice(cursor, match.index) })
      segments.push({ type: 'chart', value: rawMarker, payload })
      cursor = end
    }
    // malformed / unknown type: no cursor advance — the marker stays inside text
  }
  if (cursor < content.length) segments.push({ type: 'text', value: content.slice(cursor) })
  return segments
}

// ── Chart components (styling follows AdvisorPage / OverviewPage card style) ──

const PIE_COLORS = ['#3b82f6', '#f97316', '#ef4444', '#22c55e', '#a855f7', '#06b6d4', '#eab308', '#ec4899']

function ChartTitle({ title }: { title: string }) {
  return <div className="text-xs font-bold uppercase tracking-wide text-gray-400 mb-2">{title}</div>
}

export function MetricCardsChart({ title, data }: { title: string; data: Record<string, unknown>[] }) {
  return (
    <div className="mb-3">
      <ChartTitle title={title} />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {data.map((card, i) => (
          <div key={i} className="bg-gray-50 rounded-lg px-3 py-2 border border-gray-100">
            <div className="text-xs text-gray-500">{String(card.label ?? '')}</div>
            <div className="text-lg font-semibold text-gray-800">{String(card.value ?? '')}</div>
            {card.delta !== undefined && (
              <div className={`text-xs ${Number(card.delta) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {Number(card.delta) >= 0 ? '+' : ''}{String(card.delta)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export function LineChartWidget({ title, data, config }: { title: string; data: Record<string, unknown>[]; config?: Record<string, unknown> }) {
  const xKey = String(config?.x_key ?? 'date')
  const yKeys = (config?.y_keys as string[] | undefined) ?? Object.keys(data[0] ?? {}).filter(k => k !== xKey)
  if (!data.length || !yKeys.length) return null
  return (
    <div className="mb-3" data-testid="report-line-chart">
      <ChartTitle title={title} />
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis dataKey={xKey} tick={{ fontSize: 10 }} />
          <YAxis tick={{ fontSize: 10 }} />
          <RechartsTooltip />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {yKeys.map((key, i) => (
            <Line key={key} type="monotone" dataKey={key} stroke={PIE_COLORS[i % PIE_COLORS.length]} dot={false} strokeWidth={1.5} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function BarChartWidget({ title, data, config }: { title: string; data: Record<string, unknown>[]; config?: Record<string, unknown> }) {
  const xKey = String(config?.x_key ?? 'category')
  const yKey = String(config?.y_key ?? 'value')
  if (!data.length) return null
  return (
    <div className="mb-3" data-testid="report-bar-chart">
      <ChartTitle title={title} />
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis dataKey={xKey} tick={{ fontSize: 10 }} />
          <YAxis tick={{ fontSize: 10 }} />
          <RechartsTooltip />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey={yKey} fill="#3b82f6" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function PieChartWidget({ title, data, config }: { title: string; data: Record<string, unknown>[]; config?: Record<string, unknown> }) {
  const nameKey = String(config?.name_key ?? 'name')
  const valueKey = String(config?.value_key ?? 'value')
  if (!data.length) return null
  return (
    <div className="mb-3" data-testid="report-pie-chart">
      <ChartTitle title={title} />
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie data={data} dataKey={valueKey} nameKey={nameKey} cx="50%" cy="50%" outerRadius={80} label>
            {data.map((_, i) => (
              <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
            ))}
          </Pie>
          <RechartsTooltip />
          <Legend wrapperStyle={{ fontSize: 11 }} />
        </PieChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Map a parsed payload to its chart component. Unknown types → null (kept as text upstream). */
export function ChartRenderer({ payload }: { payload: ChartPayload }) {
  switch (payload.chart_type) {
    case 'metric_cards':
      return <MetricCardsChart title={payload.title} data={payload.data} />
    case 'line':
      return <LineChartWidget title={payload.title} data={payload.data} config={payload.config} />
    case 'bar':
      return <BarChartWidget title={payload.title} data={payload.data} config={payload.config} />
    case 'pie':
      return <PieChartWidget title={payload.title} data={payload.data} config={payload.config} />
    default:
      return null
  }
}

// ── Lazy markdown ────────────────────────────────────────────────────────────

/**
 * Lazy-loaded markdown renderer (react-markdown + remark-gfm live in a separate
 * async chunk — see vite.config manualChunks 'markdown'). Report-tab only.
 */
export const LazyMarkdown = lazy(async () => {
  const mod = await import('./reportMarkdown')
  return { default: mod.MarkdownRenderer }
})

// ── Top-level report content renderer (consumed by B3-2 ReportTab pipeline) ──

export function ReportContent({ content }: { content: string }) {
  const segments = parseReportSegments(content)
  return (
    <>
      {segments.map((seg, i) =>
        seg.type === 'chart' ? (
          <ChartRenderer key={i} payload={seg.payload} />
        ) : (
          <Suspense key={i} fallback={<div className="text-xs text-gray-400 py-2">…</div>}>
            <LazyMarkdown content={seg.value} />
          </Suspense>
        )
      )}
    </>
  )
}
