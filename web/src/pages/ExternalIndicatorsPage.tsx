/**
 * External indicator management page (Phase 1 T6).
 *
 * Four tabs:
 *  1. Catalog list (stale badge, edit via PATCH whitelist, delete with
 *     purge-data dual semantics)
 *  2. CSV import wizard (existing ExternalIndicatorsImportPage, embedded)
 *  3. Built-in catalog (P2-4b: enable-with-backfill, per-key refresh, runs)
 *  4. Custom sources (P3 placeholder)
 */

import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  datasetsApi,
  type ExtIndBuiltin,
  type ExtIndCatalogEntry,
  type ExtIndCatalogPatch,
  type ExtIndRefreshSummary,
} from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { DataState } from '@/components/ui/DataState'
import { ExternalIndicatorsImportPage } from '@/pages/ExternalIndicatorsImport'

type TabKey = 'catalog' | 'import' | 'builtin' | 'custom'

const TABS: { key: TabKey; labelKey: string }[] = [
  { key: 'catalog', labelKey: 'page.datasets.extInd.tab.catalog' },
  { key: 'import', labelKey: 'page.datasets.extInd.tab.import' },
  { key: 'builtin', labelKey: 'page.datasets.extInd.tab.builtin' },
  { key: 'custom', labelKey: 'page.datasets.extInd.tab.custom' },
]

/** Modal shell (Tailwind, same overlay style as ConfirmDialog). */
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl p-6 max-w-md w-full mx-4 max-h-[85vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-gray-900 mb-4">{title}</h3>
        {children}
      </div>
    </div>
  )
}

/** Edit dialog: PATCH whitelist fields only (pinned_source/source_config are
 *  P2/P3-reserved and must never be sent). Empty optional fields are omitted
 *  from the payload — the backend keeps the stored value for absent fields. */
function EditDialog({
  entry,
  onClose,
}: {
  entry: ExtIndCatalogEntry
  onClose: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [displayName, setDisplayName] = useState(entry.display_name)
  const [unit, setUnit] = useState(entry.unit ?? '')
  const [description, setDescription] = useState(entry.description ?? '')
  const [frequency, setFrequency] = useState<'daily' | 'weekly'>(entry.frequency)
  const [enabled, setEnabled] = useState(entry.enabled)
  const [rule, setRule] = useState<'A' | 'B'>(entry.available_date_rule)
  const [backfillStart, setBackfillStart] = useState(entry.backfill_start ?? '')

  const mutation = useMutation({
    mutationFn: (body: ExtIndCatalogPatch) =>
      datasetsApi.patchExtIndCatalog(entry.indicator_key, body),
    onSuccess: () => {
      toast.success(t('page.datasets.extInd.toast.updated'))
      queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })
      onClose()
    },
    onError: (e: Error) => toast.error(`${t('page.datasets.extInd.toast.update_failed')}: ${e.message}`),
  })

  const handleSave = () => {
    const body: ExtIndCatalogPatch = {
      display_name: displayName.trim() || entry.indicator_key,
      frequency,
      enabled,
      available_date_rule: rule,
    }
    if (unit.trim()) body.unit = unit.trim()
    if (description.trim()) body.description = description.trim()
    if (backfillStart) body.backfill_start = backfillStart
    mutation.mutate(body)
  }

  return (
    <Modal title={t('page.datasets.extInd.edit.title', { key: entry.indicator_key })} onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.display_name')}</label>
          <input value={displayName} onChange={e => setDisplayName(e.target.value)} className="input-field text-sm w-full" />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.unit')}</label>
            <input value={unit} onChange={e => setUnit(e.target.value)} className="input-field text-sm w-full" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.frequency')}</label>
            <select value={frequency} onChange={e => setFrequency(e.target.value as 'daily' | 'weekly')} className="input-field text-sm w-full">
              <option value="daily">{t('page.datasets.extInd.edit.daily')}</option>
              <option value="weekly">{t('page.datasets.extInd.edit.weekly')}</option>
            </select>
          </div>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.description')}</label>
          <textarea value={description} onChange={e => setDescription(e.target.value)} rows={2} className="input-field text-sm w-full" />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.available_date_rule')}</label>
            <select value={rule} onChange={e => setRule(e.target.value as 'A' | 'B')} className="input-field text-sm w-full">
              <option value="A">{t('page.datasets.extInd.edit.rule_a')}</option>
              <option value="B">{t('page.datasets.extInd.edit.rule_b')}</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.extInd.edit.backfill_start')}</label>
            <input type="date" value={backfillStart} onChange={e => setBackfillStart(e.target.value)} className="input-field text-sm w-full" />
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
          {t('page.datasets.extInd.edit.enabled')}
        </label>
        <div className="flex gap-3 justify-end pt-2">
          <button className="btn-secondary text-sm" onClick={onClose}>{t('page.datasets.extInd.edit.cancel')}</button>
          <button className="btn-primary text-sm" disabled={mutation.isPending} onClick={handleSave}>
            {mutation.isPending ? t('common.loading') : t('page.datasets.extInd.edit.save')}
          </button>
        </div>
      </div>
    </Modal>
  )
}

/** Delete dialog with explicit dual semantics: catalog-only (default) vs
 *  purge data rows. purge_data defaults to false (data preserved). */
function DeleteDialog({
  entry,
  onClose,
}: {
  entry: ExtIndCatalogEntry
  onClose: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [purge, setPurge] = useState(false)

  const mutation = useMutation({
    mutationFn: () => datasetsApi.deleteExtIndCatalog(entry.indicator_key, purge),
    onSuccess: () => {
      toast.success(purge ? t('page.datasets.extInd.toast.deleted_purged') : t('page.datasets.extInd.toast.deleted'))
      queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })
      onClose()
    },
    onError: (e: Error) => toast.error(`${t('page.datasets.extInd.toast.delete_failed')}: ${e.message}`),
  })

  return (
    <Modal title={t('page.datasets.extInd.delete.title', { key: entry.indicator_key })} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-gray-600">{t('page.datasets.extInd.delete.message')}</p>
        <label className={`flex items-start gap-2 p-2 rounded-lg border cursor-pointer text-sm ${purge ? 'border-red-300 bg-red-50' : 'border-gray-200'}`}>
          <input
            type="checkbox"
            className="mt-1"
            checked={purge}
            onChange={e => setPurge(e.target.checked)}
          />
          <span>
            {t('page.datasets.extInd.delete.purge_label')}
            <span className="block text-xs text-red-600 mt-0.5">{t('page.datasets.extInd.delete.purge_hint')}</span>
          </span>
        </label>
        <div className="flex gap-3 justify-end pt-2">
          <button className="btn-secondary text-sm" onClick={onClose}>{t('page.datasets.extInd.delete.cancel')}</button>
          <button
            className={purge ? 'btn-danger text-sm' : 'btn-primary text-sm'}
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('common.loading')
              : purge
                ? t('page.datasets.extInd.delete.confirm_purge')
                : t('page.datasets.extInd.delete.confirm')}
          </button>
        </div>
      </div>
    </Modal>
  )
}

function statusBadgeClass(status: string | null): string {
  if (status === 'ok') return 'bg-green-100 text-green-700'
  if (status === 'error') return 'bg-red-100 text-red-700'
  if (status === 'running') return 'bg-blue-100 text-blue-700 animate-pulse'
  return 'bg-gray-100 text-gray-600'
}

function CatalogTab() {
  const { t } = useTranslation()
  const [editing, setEditing] = useState<ExtIndCatalogEntry | null>(null)
  const [deleting, setDeleting] = useState<ExtIndCatalogEntry | null>(null)

  const { data, isLoading, error, refetch, isRefetching } = useQuery({
    queryKey: queryKeys.datasets.extIndCatalog,
    queryFn: () => datasetsApi.listExtIndCatalog(),
  })

  const fmtTime = (s: string | null) => (s ? s.slice(0, 16).replace('T', ' ') : '—')

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500">{t('page.datasets.extInd.catalog.total', { count: data?.total ?? 0 })}</span>
        <button className="btn-secondary text-xs" onClick={() => refetch()} disabled={isRefetching}>
          {isRefetching ? t('common.loading') : t('page.datasets.extInd.catalog.refresh')}
        </button>
      </div>
      <DataState
        isLoading={isLoading}
        error={error}
        isEmpty={!data?.items.length}
        emptyText={t('page.datasets.extInd.catalog.empty')}
      >
        <div className="card p-0 overflow-hidden overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-gray-50">
              <tr>
                <th className="table-th text-left">{t('page.datasets.extInd.column.key')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.displayName')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.type')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.source')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.frequency')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.latestDate')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.status')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.lastRefresh')}</th>
                <th className="table-th text-right">{t('page.datasets.extInd.column.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {data?.items.map(item => (
                <tr key={item.indicator_key} className="table-row">
                  <td className="table-td font-medium">
                    {item.indicator_key}
                    {!item.enabled && (
                      <span className="ml-1 text-[10px] text-gray-400">({t('page.datasets.extInd.disabled')})</span>
                    )}
                  </td>
                  <td className="table-td">{item.display_name}</td>
                  <td className="table-td text-gray-500">{item.source_type}</td>
                  <td className="table-td text-gray-500">{item.source_name ?? '—'}</td>
                  <td className="table-td text-gray-500">{item.frequency}</td>
                  <td className="table-td">{item.latest_trade_date ?? '—'}</td>
                  <td className="table-td">
                    <div className="flex items-center gap-1 flex-wrap">
                      <span className={`px-2 py-0.5 rounded-full ${statusBadgeClass(item.last_status)}`}>
                        {item.last_status ?? '—'}
                      </span>
                      {item.stale && (
                        <span
                          className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium"
                          title={t('page.datasets.extInd.stale_hint')}
                        >
                          {t('page.datasets.extInd.stale')}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="table-td text-gray-500">{fmtTime(item.last_refresh_at)}</td>
                  <td className="table-td text-right space-x-2 whitespace-nowrap">
                    <button className="text-brand-600 hover:underline" onClick={() => setEditing(item)}>
                      {t('page.datasets.extInd.action.edit')}
                    </button>
                    <button className="text-red-600 hover:underline" onClick={() => setDeleting(item)}>
                      {t('page.datasets.extInd.action.delete')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataState>
      {editing && <EditDialog entry={editing} onClose={() => setEditing(null)} />}
      {deleting && <DeleteDialog entry={deleting} onClose={() => setDeleting(null)} />}
    </div>
  )
}

/** Toast a refresh summary: aggregate ok/error counts; surface per-key errors
 *  from the results list (each entry carries its own `error` string). */
function toastRefreshSummary(t: ReturnType<typeof useTranslation>['t'], s: ExtIndRefreshSummary) {
  const errors = s.results.filter(r => r.status === 'error')
  if (errors.length > 0) {
    const detail = errors
      .slice(0, 3)
      .map(r => `${r.indicator_key}: ${r.error ?? 'unknown'}`)
      .join('\n')
    toast.error(
      `${t('page.datasets.extInd.builtin.toast.refresh_done_errors', { ok: s.ok, error: s.error })}\n${detail}`,
      { duration: 8000 },
    )
  } else {
    toast.success(
      t('page.datasets.extInd.builtin.toast.refresh_done', { ok: s.ok, error: s.error }),
    )
  }
}

/** Built-in catalog tab (P2-4b): builtin registry rows with candidate-source
 *  readiness, enable-with-backfill, per-key refresh, run history, and the
 *  page-level "refresh all due" entry (the ONLY full-refresh trigger — row
 *  buttons always pass an explicit keys array to avoid accidental full runs). */
function BuiltinTab() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [enabling, setEnabling] = useState<ExtIndBuiltin | null>(null)
  const [confirmRefreshAll, setConfirmRefreshAll] = useState(false)
  const [runsKey, setRunsKey] = useState('')
  const [showRuns, setShowRuns] = useState(false)

  const builtins = useQuery({
    queryKey: queryKeys.datasets.extIndBuiltins,
    queryFn: () => datasetsApi.listExtIndBuiltins(),
  })

  const runs = useQuery({
    queryKey: queryKeys.datasets.extIndRuns(runsKey, 20),
    queryFn: () => datasetsApi.listExtIndRuns(runsKey, 20),
    enabled: showRuns,
  })

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })
    void queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndBuiltins })
    void queryClient.invalidateQueries({ queryKey: ['datasets', 'ext-ind-runs'] })
  }

  const enableMutation = useMutation({
    mutationFn: (b: { key: string; backfillStart: string }) =>
      datasetsApi.enableExtIndBuiltin(b.key, b.backfillStart || undefined),
    onSuccess: (data, vars) => {
      if (data.backfill === 'pending') {
        toast.info(t('page.datasets.extInd.builtin.toast.enabled_pending', { key: vars.key }))
      } else {
        toast.success(t('page.datasets.extInd.builtin.toast.enabled', { key: vars.key }))
      }
      invalidateAll()
      setEnabling(null)
    },
    onError: (e: Error) =>
      toast.error(`${t('page.datasets.extInd.builtin.toast.enable_failed')}: ${e.message}`),
  })

  const refreshMutation = useMutation({
    mutationFn: (keys: string[] | undefined) =>
      datasetsApi.refreshExtIndicators(keys ? { keys } : {}),
    onSuccess: s => {
      toastRefreshSummary(t, s)
      invalidateAll()
    },
    onError: (e: Error) =>
      toast.error(`${t('page.datasets.extInd.builtin.toast.refresh_failed')}: ${e.message}`),
  })

  const fmtTime = (s: string | null) => (s ? s.slice(0, 19).replace('T', ' ') : '—')
  const anyBusy = enableMutation.isPending || refreshMutation.isPending

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500">
          {t('page.datasets.extInd.builtin.total', { count: builtins.data?.total ?? 0 })}
        </span>
        <div className="flex gap-2">
          <button
            className="btn-secondary text-xs"
            onClick={() => setConfirmRefreshAll(true)}
            disabled={anyBusy}
          >
            {t('page.datasets.extInd.builtin.refresh_all')}
          </button>
          <button
            className="btn-secondary text-xs"
            onClick={() => void builtins.refetch()}
            disabled={builtins.isRefetching}
          >
            {builtins.isRefetching ? t('common.loading') : t('page.datasets.extInd.catalog.refresh')}
          </button>
        </div>
      </div>

      <DataState
        isLoading={builtins.isLoading}
        error={builtins.error}
        isEmpty={!builtins.data?.items.length}
        emptyText={t('page.datasets.extInd.builtin.empty')}
      >
        <div className="card p-0 overflow-hidden overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-gray-50">
              <tr>
                <th className="table-th text-left">{t('page.datasets.extInd.column.key')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.displayName')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.frequency')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.builtin.column.unit')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.builtin.column.sources')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.column.status')}</th>
                <th className="table-th text-left">{t('page.datasets.extInd.builtin.column.backfill_years')}</th>
                <th className="table-th text-right">{t('page.datasets.extInd.column.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {builtins.data?.items.map(item => (
                <tr key={item.indicator_key} className="table-row">
                  <td className="table-td font-medium">
                    {item.indicator_key}
                    {item.description && (
                      <span className="block text-[10px] text-gray-400">{item.description}</span>
                    )}
                  </td>
                  <td className="table-td">{item.display_name}</td>
                  <td className="table-td text-gray-500">{item.frequency}</td>
                  <td className="table-td text-gray-500">{item.unit ?? '—'}</td>
                  <td className="table-td">
                    <div className="flex flex-wrap gap-1">
                      {item.candidates.map(c => (
                        <span
                          key={c.name}
                          className={`px-2 py-0.5 rounded-full ${
                            c.ready ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                          }`}
                          title={
                            c.name === 'tushare' && !c.ready
                              ? t('page.datasets.extInd.builtin.tushare_hint')
                              : c.ready
                                ? t('page.datasets.extInd.builtin.source_ready')
                                : t('page.datasets.extInd.builtin.source_not_ready')
                          }
                        >
                          {c.name} {c.ready ? '✅' : '❌'}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="table-td">
                    <span
                      className={`px-2 py-0.5 rounded-full ${
                        item.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
                      }`}
                    >
                      {item.enabled
                        ? t('page.datasets.extInd.builtin.enabled_badge')
                        : t('page.datasets.extInd.builtin.disabled_badge')}
                    </span>
                  </td>
                  <td className="table-td text-gray-500">
                    {t('page.datasets.extInd.builtin.backfill_years_value', {
                      years: item.default_backfill_years,
                    })}
                  </td>
                  <td className="table-td text-right whitespace-nowrap">
                    {item.enabled ? (
                      <button
                        className="text-brand-600 hover:underline disabled:opacity-50"
                        disabled={anyBusy}
                        onClick={() => refreshMutation.mutate([item.indicator_key])}
                      >
                        {t('page.datasets.extInd.builtin.refresh_one')}
                      </button>
                    ) : (
                      <button
                        className="text-brand-600 hover:underline"
                        onClick={() => setEnabling(item)}
                      >
                        {t('page.datasets.extInd.builtin.enable')}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataState>

      {/* Run history (collapsed by default; filterable by key) */}
      <div className="card p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-medium text-gray-900">
            {t('page.datasets.extInd.builtin.runs.title')}
          </h4>
          <div className="flex items-center gap-2">
            <select
              className="input-field text-xs w-48"
              value={runsKey}
              onChange={e => setRunsKey(e.target.value)}
            >
              <option value="">{t('page.datasets.extInd.builtin.runs.all_keys')}</option>
              {builtins.data?.items.map(b => (
                <option key={b.indicator_key} value={b.indicator_key}>
                  {b.indicator_key}
                </option>
              ))}
            </select>
            <button className="btn-secondary text-xs" onClick={() => setShowRuns(v => !v)}>
              {showRuns
                ? t('page.datasets.extInd.builtin.runs.hide')
                : t('page.datasets.extInd.builtin.runs.show')}
            </button>
          </div>
        </div>
        {showRuns && (
          <DataState
            isLoading={runs.isLoading}
            error={runs.error}
            isEmpty={!runs.data?.items.length}
            emptyText={t('page.datasets.extInd.builtin.runs.empty')}
          >
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="table-th text-left">{t('page.datasets.extInd.column.key')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.column.source')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.builtin.runs.started')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.column.status')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.builtin.runs.rows')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.builtin.runs.range')}</th>
                    <th className="table-th text-left">{t('page.datasets.extInd.builtin.runs.error')}</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data?.items.map(r => (
                    <tr key={r.run_id} className="table-row">
                      <td className="table-td font-medium">{r.indicator_key}</td>
                      <td className="table-td text-gray-500">{r.source_name ?? '—'}</td>
                      <td className="table-td text-gray-500">{fmtTime(r.started_at)}</td>
                      <td className="table-td">
                        <div className="flex items-center gap-1">
                          <span className={`px-2 py-0.5 rounded-full ${statusBadgeClass(r.status === 'running' && r.interrupted ? 'interrupted' : r.status)}`}>
                            {r.status}
                          </span>
                          {r.interrupted && (
                            <span
                              className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium"
                              title={t('page.datasets.extInd.builtin.runs.interrupted_hint')}
                            >
                              {t('page.datasets.extInd.builtin.runs.interrupted')}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="table-td text-gray-500">
                        {r.rows_upserted ?? 0}/{r.rows_fetched ?? 0}
                      </td>
                      <td className="table-td text-gray-500">
                        {r.range_start && r.range_end ? `${r.range_start}..${r.range_end}` : '—'}
                      </td>
                      <td className="table-td text-red-600 max-w-48 truncate" title={r.error ?? undefined}>
                        {r.error ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataState>
        )}
      </div>

      {/* Enable dialog: optional backfill_start (empty = backend default) */}
      {enabling && (
        <Modal
          title={t('page.datasets.extInd.builtin.enable_title', { key: enabling.indicator_key })}
          onClose={() => setEnabling(null)}
        >
          <EnableDialogBody
            builtin={enabling}
            pending={enableMutation.isPending}
            onConfirm={backfillStart =>
              enableMutation.mutate({ key: enabling.indicator_key, backfillStart })
            }
            onCancel={() => setEnabling(null)}
          />
        </Modal>
      )}

      {/* Full refresh (due) confirm — the only full-refresh entry point */}
      {confirmRefreshAll && (
        <Modal
          title={t('page.datasets.extInd.builtin.refresh_all_confirm_title')}
          onClose={() => setConfirmRefreshAll(false)}
        >
          <div className="space-y-3">
            <p className="text-sm text-gray-600">
              {t('page.datasets.extInd.builtin.refresh_all_confirm_message')}
            </p>
            <div className="flex gap-3 justify-end pt-2">
              <button className="btn-secondary text-sm" onClick={() => setConfirmRefreshAll(false)}>
                {t('page.datasets.extInd.delete.cancel')}
              </button>
              <button
                className="btn-primary text-sm"
                disabled={refreshMutation.isPending}
                onClick={() => {
                  refreshMutation.mutate(undefined)
                  setConfirmRefreshAll(false)
                }}
              >
                {refreshMutation.isPending
                  ? t('common.loading')
                  : t('page.datasets.extInd.builtin.refresh_all')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

/** Body of the enable dialog: shows default backfill window hint and an
 *  optional backfill_start date input (empty = backend default years). */
function EnableDialogBody({
  builtin,
  pending,
  onConfirm,
  onCancel,
}: {
  builtin: ExtIndBuiltin
  pending: boolean
  onConfirm: (backfillStart: string) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [backfillStart, setBackfillStart] = useState('')

  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-600">
        {t('page.datasets.extInd.builtin.enable_message', {
          name: builtin.display_name,
          years: builtin.default_backfill_years,
        })}
      </p>
      <div>
        <label className="block text-xs text-gray-500 mb-1">
          {t('page.datasets.extInd.builtin.backfill_start_optional')}
        </label>
        <input
          type="date"
          value={backfillStart}
          onChange={e => setBackfillStart(e.target.value)}
          className="input-field text-sm w-full"
        />
      </div>
      <div className="flex gap-3 justify-end pt-2">
        <button className="btn-secondary text-sm" onClick={onCancel}>
          {t('page.datasets.extInd.edit.cancel')}
        </button>
        <button
          className="btn-primary text-sm"
          disabled={pending}
          onClick={() => onConfirm(backfillStart)}
        >
          {pending ? t('common.loading') : t('page.datasets.extInd.builtin.enable')}
        </button>
      </div>
    </div>
  )
}

function Placeholder({ text }: { text: string }) {
  return (
    <div className="card flex items-center justify-center h-64 text-gray-400 text-sm">{text}</div>
  )
}

export function ExternalIndicatorsPage() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabKey>('catalog')
  const queryClient = useQueryClient()

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">{t('page.datasets.extInd.title')}</h1>
        <p className="page-subtitle">{t('page.datasets.extInd.subtitle')}</p>
      </div>

      {/* Tab navigation (same style as DatasetsPage) */}
      <div className="flex gap-1 border-b border-gray-200">
        {TABS.map(tab => (
          <button
            key={tab.key}
            className={`px-4 py-2 text-sm font-medium transition-colors -mb-px ${
              activeTab === tab.key
                ? 'border-b-2 border-brand-500 text-brand-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab(tab.key)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      {activeTab === 'catalog' && <CatalogTab />}
      {activeTab === 'import' && (
        <ExternalIndicatorsImportPage
          embedded
          onImported={() => queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })}
        />
      )}
      {activeTab === 'builtin' && <BuiltinTab />}
      {activeTab === 'custom' && <Placeholder text={t('page.datasets.extInd.placeholder.custom')} />}
    </div>
  )
}
