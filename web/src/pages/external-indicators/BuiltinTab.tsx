/**
 * Built-in catalog tab (P2-4b), split out of ExternalIndicatorsPage.tsx in
 * P3-5 (pure move — logic unchanged): builtin registry rows with
 * candidate-source readiness, enable-with-backfill, per-key refresh, run
 * history, and the page-level "refresh all due" entry (the ONLY full-refresh
 * trigger — row buttons always pass an explicit keys array to avoid
 * accidental full runs).
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  datasetsApi,
  type ExtIndBuiltin,
  type ExtIndRefreshSummary,
} from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { DataState } from '@/components/ui/DataState'
import { Modal, statusBadgeClass } from './shared'

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

export function BuiltinTab() {
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
