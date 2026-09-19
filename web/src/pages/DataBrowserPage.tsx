/**
 * Data Browser — read-only structured query UI over whitelisted tables.
 *
 * Table/column/operator choices are validated server-side; this page only
 * builds the structured QueryBody (never raw SQL).
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { queryApi, type QueryWhereClause } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { DataState } from '@/components/ui/DataState'

const OPS = ['=', '!=', '>', '<', '>=', '<=', 'LIKE', 'IN', 'IS NULL', 'IS NOT NULL'] as const

interface WhereRow extends QueryWhereClause {
  _id: number
}

type ResultRow = Record<string, unknown> & { __idx: number }

function isoDaysAgo(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString().slice(0, 10)
}

function cellText(r: Record<string, unknown>, k: string): string {
  const v = r[k]
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export function DataBrowserPage() {
  const { t } = useTranslation()

  // -- query builder state -------------------------------------------------
  const [table, setTable] = useState('silver_prices_1d')
  const [columns, setColumns] = useState<string[]>([])   // empty = all
  const [whereRows, setWhereRows] = useState<WhereRow[]>([])
  const [sortCol, setSortCol] = useState('')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [limit, setLimit] = useState(200)
  const [body, setBody] = useState<Record<string, unknown> | null>(null)

  // -- coverage preset state -----------------------------------------------
  const [covStart, setCovStart] = useState(isoDaysAgo(365))
  const [covEnd, setCovEnd] = useState(isoDaysAgo(0))
  const [covTriggered, setCovTriggered] = useState(false)

  const tablesQuery = useQuery({
    queryKey: queryKeys.query.tables(),
    queryFn: () => queryApi.tables(),
    staleTime: 5 * 60_000,
  })
  const tables = tablesQuery.data?.tables ?? []

  const resultQuery = useQuery({
    queryKey: queryKeys.query.browser(body),
    queryFn: () => queryApi.query(body as never),
    enabled: body !== null,
  })

  const coverageQuery = useQuery({
    queryKey: queryKeys.query.coverage(covStart, covEnd, false),
    queryFn: () => queryApi.coverage(covStart, covEnd, 20, false),
    enabled: covTriggered && !!covStart && !!covEnd && covStart <= covEnd,
  })

  function buildBody(): Record<string, unknown> {
    const b: Record<string, unknown> = { table, limit }
    if (columns.length > 0) b.columns = columns
    const wheres = whereRows
      .filter(r => r.col && (r.op === 'IS NULL' || r.op === 'IS NOT NULL'
        || (r.val !== undefined && r.val !== '')))
      .map(({ _id: _ignored, ...w }) => w)
    if (wheres.length > 0) b.where = wheres
    if (sortCol) b.order_by = [{ col: sortCol, dir: sortDir }]
    return b
  }

  function execute() {
    setCovTriggered(false)
    setBody(buildBody())
  }

  function runExtIndPreset() {
    setCovTriggered(false)
    setBody({
      table: 'silver_external_indicators',
      where: [{ col: 'trade_date', op: '>=', val: isoDaysAgo(30) }],
      order_by: [{ col: 'trade_date', dir: 'desc' }],
      limit: 200,
    })
  }

  function addWhereRow() {
    setWhereRows(prev => [...prev, { _id: Date.now() + prev.length, col: '', op: '=', val: '' }])
  }

  function updateWhereRow(id: number, patch: Partial<QueryWhereClause>) {
    setWhereRows(prev => prev.map(r => (r._id === id ? { ...r, ...patch } : r)))
  }

  function removeWhereRow(id: number) {
    setWhereRows(prev => prev.filter(r => r._id !== id))
  }

  const rows: ResultRow[] = (resultQuery.data?.rows ?? []).map((r, i) => ({ ...r, __idx: i }))
  const resultCols: Column<ResultRow>[] = (resultQuery.data?.columns ?? []).map(c => ({
    key: c,
    label: c,
    sortable: true,
    searchable: true,
    render: (_v: unknown, row) => cellText(row, c),
  }))

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">{t('page.data_browser.title')}</h1>
        <p className="text-sm text-gray-500 mt-1">{t('page.data_browser.subtitle')}</p>
      </div>

      {/* Presets */}
      <section className="card p-4 space-y-3">
        <h2 className="text-sm font-medium">{t('page.data_browser.presets')}</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.data_browser.start_date')}</label>
            <input type="date" value={covStart} onChange={e => { setCovStart(e.target.value); setCovTriggered(false) }} className="input-field text-sm w-40" />
          </div>
          <div className="pb-1 text-gray-400">~</div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.data_browser.end_date')}</label>
            <input type="date" value={covEnd} onChange={e => { setCovEnd(e.target.value); setCovTriggered(false) }} className="input-field text-sm w-40" />
          </div>
          <button
            className="btn-primary text-sm"
            onClick={() => { setBody(null); setCovTriggered(true) }}
            disabled={!covStart || !covEnd || covStart > covEnd}
          >
            {t('page.data_browser.preset_coverage')}
          </button>
          <button className="btn-secondary text-sm" onClick={runExtIndPreset}>
            {t('page.data_browser.preset_ext_ind')}
          </button>
        </div>
        {covTriggered && (
          <DataState
            isLoading={coverageQuery.isLoading}
            error={coverageQuery.error}
            isEmpty={!coverageQuery.isLoading && !coverageQuery.error && !coverageQuery.data}
            emptyText={t('page.data_browser.no_data')}
          >
            {coverageQuery.data && (
              <div className="text-sm space-y-1">
                <div>
                  <span className="font-medium">{t('page.data_browser.n_assets')}:</span>{' '}
                  {coverageQuery.data.n_assets}
                  <span className="text-gray-400 ml-2">
                    ({coverageQuery.data.start_date} ~ {coverageQuery.data.end_date})
                  </span>
                </div>
                <div className="text-xs text-gray-500 break-all">
                  {t('page.data_browser.sample_assets')}: {coverageQuery.data.sample_assets.join(', ') || '—'}
                </div>
              </div>
            )}
          </DataState>
        )}
      </section>

      {/* Structured query builder */}
      <section className="card p-4 space-y-4">
        <h2 className="text-sm font-medium">{t('page.data_browser.builder')}</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.data_browser.table')}</label>
            <select
              value={table}
              onChange={e => { setTable(e.target.value); setColumns([]); setSortCol(''); setBody(null) }}
              className="input-field text-sm w-64"
            >
              {tables.map(tb => <option key={tb} value={tb}>{tb}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              {t('page.data_browser.columns')}
              {columns.length === 0 ? ` (${t('page.data_browser.all_columns')})` : ` (${columns.length})`}
            </label>
            <input
              value={columns.join(', ')}
              onChange={e => setColumns(e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
              placeholder={t('page.data_browser.columns_placeholder')}
              className="input-field text-sm w-72"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.data_browser.limit')}</label>
            <input
              type="number"
              min={1}
              max={1000}
              value={limit}
              onChange={e => setLimit(Math.min(1000, Math.max(1, Number(e.target.value) || 200)))}
              className="input-field text-sm w-24"
            />
          </div>
        </div>

        {/* WHERE builder */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">{t('page.data_browser.where')}</span>
            <button className="btn-secondary text-xs" onClick={addWhereRow}>
              + {t('page.data_browser.add_condition')}
            </button>
          </div>
          {whereRows.map(r => (
            <div key={r._id} className="flex items-center gap-2">
              <input
                value={r.col}
                onChange={e => updateWhereRow(r._id, { col: e.target.value })}
                placeholder={t('page.data_browser.col')}
                className="input-field text-sm w-44"
              />
              <select
                value={r.op}
                onChange={e => updateWhereRow(r._id, { op: e.target.value as QueryWhereClause['op'] })}
                className="input-field text-sm w-32"
              >
                {OPS.map(op => <option key={op} value={op}>{op}</option>)}
              </select>
              {r.op !== 'IS NULL' && r.op !== 'IS NOT NULL' && (
                <input
                  value={String(r.val ?? '')}
                  onChange={e => {
                    const raw = e.target.value
                    const parsed = r.op === 'IN'
                      ? raw.split(',').map(s => s.trim()).filter(Boolean)
                      : raw
                    updateWhereRow(r._id, { val: parsed })
                  }}
                  placeholder={r.op === 'IN' ? t('page.data_browser.value_list') : t('page.data_browser.value')}
                  className="input-field text-sm flex-1"
                />
              )}
              <button className="btn-secondary text-xs px-2" onClick={() => removeWhereRow(r._id)}>×</button>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.data_browser.order_by')}</label>
            <input
              value={sortCol}
              onChange={e => setSortCol(e.target.value)}
              placeholder={t('page.data_browser.col')}
              className="input-field text-sm w-44"
            />
          </div>
          <select
            value={sortDir}
            onChange={e => setSortDir(e.target.value as 'asc' | 'desc')}
            className="input-field text-sm w-24"
          >
            <option value="asc">ASC</option>
            <option value="desc">DESC</option>
          </select>
          <button className="btn-primary text-sm" onClick={execute}>
            {t('page.data_browser.execute')}
          </button>
        </div>
      </section>

      {/* Results */}
      <section className="card p-4">
        <h2 className="text-sm font-medium mb-3">
          {t('page.data_browser.results')}
          {resultQuery.data ? ` (${resultQuery.data.total})` : ''}
        </h2>
        {body === null ? (
          <p className="text-sm text-gray-400">{t('page.data_browser.hint')}</p>
        ) : (
          <DataState
            isLoading={resultQuery.isLoading}
            error={resultQuery.error}
            isEmpty={!resultQuery.isLoading && !resultQuery.error && rows.length === 0}
            emptyText={t('page.data_browser.no_data')}
          >
            <DataTable
              data={rows}
              columns={resultCols}
              rowKey="__idx"
              pageSize={20}
              enableExport
              exportFilename={`data_browser_${table}`}
            />
          </DataState>
        )}
      </section>
    </div>
  )
}
