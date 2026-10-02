/**
 * Custom sources tab (P3-5): manage `source_type='custom_http'` catalog rows.
 *
 * - Config form covering every CustomHTTPConfig field (method / url_template
 *   with `{date}` placeholder / headers+params k-v editors with `${ENV_VAR}`
 *   hint / date_param_style / jsonpath extraction / guards & limits / metadata).
 * - "Test connection" hits POST /external-indicators/test anytime while
 *   editing (nothing persisted): renders the ≤10-row sample table or a
 *   stage-mapped bilingual error (stage tags come from the backend's
 *   GuardError / pydantic 400 detail — see STAGE_KEYS).
 * - Save: create via POST catalog (409 → duplicate-key toast) or edit via
 *   PATCH. On edit, `source_config` is only sent when the config half is
 *   dirty, and redacted echo values (`***redacted***`) must be re-entered
 *   before a dirty config can be saved — plaintext secrets are stored
 *   server-side and never round-trip to the browser (backend redact_config).
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  datasetsApi,
  extractExtIndStageError,
  ApiError,
  type CustomHTTPSourceConfig,
  type ExtIndTestResult,
} from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { DataState } from '@/components/ui/DataState'

/** Marker the backend substitutes for masked secret header values on echo. */
const REDACTED = '***redacted***'

/** Stage tags the backend can attach to a 400 (http_guard GuardError stages
 *  + `config_invalid` from the save-time pydantic precheck). */
const STAGE_KEYS = [
  'config_invalid',
  'ssrf_blocked',
  'timeout',
  'oversized',
  'redirect_blocked',
  'json_parse',
  'path_miss',
  'env_missing',
  'dns',
  'too_many_requests',
  'http_error',
  'request_failed',
  'date_render',
] as const

type FieldKey =
  | 'indicator_key'
  | 'url_template'
  | 'records_path'
  | 'field_trade_date'
  | 'field_value'
  | 'method'
  | 'date_param_style'
  | 'headers'

interface KVRow {
  id: number
  key: string
  value: string
}

interface CustomFormState {
  indicatorKey: string
  displayName: string
  unit: string
  description: string
  method: 'GET' | 'POST'
  urlTemplate: string
  headers: KVRow[]
  params: KVRow[]
  dateParamStyle: 'yyyymmdd' | 'yyyy-mm-dd' | 'none'
  recordsPath: string
  fieldTradeDate: string
  fieldValue: string
  allowInsecure: boolean
  timeoutConnect: string
  timeoutTotal: string
  maxBytes: string
  frequency: 'daily' | 'weekly' | 'monthly'
  rule: 'A' | 'B'
  backfillStart: string
}

let kvSeq = 0
function toKVRows(record: Record<string, string>): KVRow[] {
  return Object.entries(record).map(([key, value]) => ({ id: ++kvSeq, key, value }))
}

function emptyForm(): CustomFormState {
  return {
    indicatorKey: '',
    displayName: '',
    unit: '',
    description: '',
    method: 'GET',
    urlTemplate: '',
    headers: [],
    params: [],
    dateParamStyle: 'yyyymmdd',
    recordsPath: '$.data.list',
    fieldTradeDate: 'trade_date',
    fieldValue: 'value',
    allowInsecure: false,
    timeoutConnect: '10',
    timeoutTotal: '30',
    maxBytes: '10485760',
    frequency: 'daily',
    rule: 'B',
    backfillStart: '',
  }
}

function kvToRecord(rows: KVRow[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const r of rows) {
    const k = r.key.trim()
    if (k) out[k] = r.value
  }
  return out
}

/** Build the API source_config payload from form state. Numeric fields fall
 *  back to defaults on unparsable input (backend clamps to its own caps). */
export function buildSourceConfig(f: CustomFormState): CustomHTTPSourceConfig {
  return {
    method: f.method,
    url_template: f.urlTemplate.trim(),
    headers: kvToRecord(f.headers),
    params: kvToRecord(f.params),
    date_param_style: f.dateParamStyle,
    extraction: {
      type: 'jsonpath',
      records_path: f.recordsPath.trim(),
      field_map: { trade_date: f.fieldTradeDate.trim(), value: f.fieldValue.trim() },
    },
    allow_insecure_http: f.allowInsecure,
    timeout_connect_sec: Number.parseInt(f.timeoutConnect, 10) || 10,
    timeout_total_sec: Number.parseInt(f.timeoutTotal, 10) || 30,
    max_bytes: Number.parseInt(f.maxBytes, 10) || 10485760,
  }
}

function formFromEntry(
  key: string,
  displayName: string,
  unit: string | null,
  description: string | null,
  frequency: 'daily' | 'weekly' | 'monthly',
  rule: 'A' | 'B',
  backfillStart: string | null,
  cfg: Record<string, unknown>,
): CustomFormState {
  const extraction = (cfg.extraction ?? {}) as Record<string, unknown>
  const fieldMap = (extraction.field_map ?? {}) as Record<string, unknown>
  return {
    indicatorKey: key,
    displayName,
    unit: unit ?? '',
    description: description ?? '',
    method: cfg.method === 'POST' ? 'POST' : 'GET',
    urlTemplate: typeof cfg.url_template === 'string' ? cfg.url_template : '',
    headers: toKVRows((cfg.headers ?? {}) as Record<string, string>),
    params: toKVRows((cfg.params ?? {}) as Record<string, string>),
    dateParamStyle:
      cfg.date_param_style === 'yyyy-mm-dd' || cfg.date_param_style === 'none'
        ? cfg.date_param_style
        : 'yyyymmdd',
    recordsPath: typeof extraction.records_path === 'string' ? extraction.records_path : '',
    fieldTradeDate: typeof fieldMap.trade_date === 'string' ? fieldMap.trade_date : '',
    fieldValue: typeof fieldMap.value === 'string' ? fieldMap.value : '',
    allowInsecure: cfg.allow_insecure_http === true,
    timeoutConnect: String(cfg.timeout_connect_sec ?? 10),
    timeoutTotal: String(cfg.timeout_total_sec ?? 30),
    maxBytes: String(cfg.max_bytes ?? 10485760),
    frequency,
    rule,
    backfillStart: backfillStart ?? '',
  }
}

/** Map a pydantic `config_invalid` message ("source_config 校验失败：
 *  loc: msg; loc: msg") onto form fields. Best-effort — unmatched segments
 *  stay in the top banner. */
function parseFieldErrors(message: string): { fieldErrors: Partial<Record<FieldKey, string>>; rest: string[] } {
  const fieldErrors: Partial<Record<FieldKey, string>> = {}
  const rest: string[] = []
  const compact = message.includes('：') ? message.slice(message.lastIndexOf('：') + 1) : message
  for (const seg of compact.split('; ')) {
    const idx = seg.indexOf(': ')
    if (idx < 0) {
      if (seg.trim()) rest.push(seg)
      continue
    }
    const loc = seg.slice(0, idx)
    const msg = seg.slice(idx + 2)
    if (loc === 'url_template') fieldErrors.url_template = msg
    else if (loc === 'method') fieldErrors.method = msg
    else if (loc === 'date_param_style') fieldErrors.date_param_style = msg
    else if (loc === 'extraction.records_path') fieldErrors.records_path = msg
    else if (loc.startsWith('extraction.field_map')) {
      if (msg.includes('trade_date')) fieldErrors.field_trade_date = msg
      else fieldErrors.field_value = msg
    } else if (loc === 'headers' || loc === 'params') {
      fieldErrors.headers = msg
    } else {
      rest.push(seg)
    }
  }
  return { fieldErrors, rest }
}

function FieldError({ msg }: { msg: string | undefined }) {
  if (!msg) return null
  return <p className="text-xs text-red-600 mt-1">{msg}</p>
}

function Label({ labelKey, hintKey }: { labelKey: string; hintKey?: string }) {
  const { t } = useTranslation()
  return (
    <label className="block text-xs text-gray-500 mb-1">
      {t(labelKey)}
      {hintKey && <span className="block text-[10px] text-gray-400">{t(hintKey)}</span>}
    </label>
  )
}

/** k-v row editor for headers / params. Empty-key rows are dropped at
 *  payload build time (kvToRecord). */
function KVEditor({
  rows,
  onChange,
  addLabel,
  hint,
}: {
  rows: KVRow[]
  onChange: (rows: KVRow[]) => void
  addLabel: string
  hint?: string
}) {
  const update = (id: number, patch: Partial<KVRow>) =>
    onChange(rows.map(r => (r.id === id ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-2">
      {hint && <p className="text-[10px] text-gray-400">{hint}</p>}
      {rows.map(r => (
        <div key={r.id} className="flex gap-2 items-center">
          <input
            value={r.key}
            onChange={e => update(r.id, { key: e.target.value })}
            className="input-field text-xs w-36"
          />
          <input
            value={r.value}
            onChange={e => update(r.id, { value: e.target.value })}
            className="input-field text-xs flex-1"
          />
          <button
            type="button"
            className="text-red-600 hover:underline text-xs"
            onClick={() => onChange(rows.filter(x => x.id !== r.id))}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn-secondary text-xs"
        onClick={() => onChange([...rows, { id: ++kvSeq, key: '', value: '' }])}
      >
        + {addLabel}
      </button>
    </div>
  )
}

function StageErrorBanner({ stage, message }: { stage: string | null; message: string }) {
  const { t } = useTranslation()
  const known = stage !== null && (STAGE_KEYS as readonly string[]).includes(stage)
  const stageKey = known ? stage! : 'unknown'
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-3 space-y-1">
      <p className="text-sm font-medium text-red-700">
        {t('page.datasets.extInd.custom.stage_label')}：{t(`page.datasets.extInd.custom.stage.${stageKey}`)}
      </p>
      <p className="text-xs text-red-600 break-all">{message}</p>
    </div>
  )
}

export function CustomTab() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const catalog = useQuery({
    queryKey: queryKeys.datasets.extIndCatalog,
    queryFn: () => datasetsApi.listExtIndCatalog(),
  })

  // 'idle' = row list; 'create' / 'edit' = form
  const [mode, setMode] = useState<'idle' | 'create' | 'edit'>('idle')
  const [editKey, setEditKey] = useState('')
  const [form, setForm] = useState<CustomFormState>(emptyForm)
  /** JSON snapshot of the loaded config — edit-mode PATCH only sends
   *  source_config when the current build differs from this. */
  const [configSnapshot, setConfigSnapshot] = useState('')
  const [loadingEdit, setLoadingEdit] = useState(false)

  const [testResult, setTestResult] = useState<ExtIndTestResult | null>(null)
  const [testError, setTestError] = useState<{ stage: string | null; message: string } | null>(null)
  const [saveError, setSaveError] = useState<{ stage: string | null; message: string } | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<FieldKey, string>>>({})

  const customRows = (catalog.data?.items ?? []).filter(i => i.source_type === 'custom_http')
  const set = (patch: Partial<CustomFormState>) => setForm(f => ({ ...f, ...patch }))

  const startCreate = () => {
    setMode('create')
    setEditKey('')
    setForm(emptyForm())
    setConfigSnapshot('')
    resetFeedback()
  }

  const resetFeedback = () => {
    setTestResult(null)
    setTestError(null)
    setSaveError(null)
    setFieldErrors({})
  }

  const startEdit = async (key: string) => {
    setLoadingEdit(true)
    resetFeedback()
    try {
      const d = await datasetsApi.getExtIndCatalog(key)
      const cfg =
        d.source_config && typeof d.source_config === 'object' ? d.source_config : {}
      const next = formFromEntry(
        d.indicator_key,
        d.display_name,
        d.unit,
        d.description,
        d.frequency,
        d.available_date_rule,
        d.backfill_start,
        cfg,
      )
      setForm(next)
      setConfigSnapshot(JSON.stringify(buildSourceConfig(next)))
      setEditKey(key)
      setMode('edit')
    } catch (e) {
      toast.error(`${t('page.datasets.extInd.custom.toast.load_failed')}: ${(e as Error).message}`)
    } finally {
      setLoadingEdit(false)
    }
  }

  const testMutation = useMutation({
    mutationFn: () => datasetsApi.testExtIndCustom(buildSourceConfig(form)),
    onSuccess: data => {
      setTestResult(data)
      setTestError(null)
    },
    onError: e => {
      setTestResult(null)
      const err = extractExtIndStageError(e)
      setTestError(err)
      if (err.stage === 'config_invalid') {
        setFieldErrors(prev => ({ ...prev, ...parseFieldErrors(err.message).fieldErrors }))
      }
    },
  })

  const invalidateCatalog = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.datasets.extIndCatalog })
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const config = buildSourceConfig(form)
      if (mode === 'create') {
        const body = {
          indicator_key: form.indicatorKey.trim(),
          display_name: form.displayName.trim() || form.indicatorKey.trim(),
          unit: form.unit.trim() || undefined,
          description: form.description.trim() || undefined,
          frequency: form.frequency,
          available_date_rule: form.rule,
          backfill_start: form.backfillStart || undefined,
          source_config: config,
        }
        return datasetsApi.createExtIndCustom(body)
      }
      // Edit: metadata always; source_config only when the config half is dirty
      const body = {
        display_name: form.displayName.trim() || form.indicatorKey,
        unit: form.unit.trim() || undefined,
        description: form.description.trim() || undefined,
        frequency: form.frequency,
        // `enabled` is deliberately NOT sent: absent fields keep their
        // stored value, and silently re-enabling a disabled row here would
        // be a surprise side effect (toggling lives in the catalog tab).
        available_date_rule: form.rule,
        backfill_start: form.backfillStart || undefined,
        source_config:
          JSON.stringify(config) === configSnapshot ? undefined : config,
      }
      return datasetsApi.patchExtIndCatalog(editKey, body)
    },
    onSuccess: () => {
      toast.success(t('page.datasets.extInd.custom.toast.saved'))
      invalidateCatalog()
      setMode('idle')
      setEditKey('')
    },
    onError: e => {
      if (e instanceof ApiError && e.status === 409) {
        toast.error(t('page.datasets.extInd.custom.toast.duplicate'))
        setFieldErrors(prev => ({
          ...prev,
          indicator_key: t('page.datasets.extInd.custom.toast.duplicate'),
        }))
        return
      }
      const err = extractExtIndStageError(e)
      setSaveError(err)
      if (err.stage === 'config_invalid') {
        setFieldErrors(prev => ({ ...prev, ...parseFieldErrors(err.message).fieldErrors }))
      } else {
        toast.error(`${t('page.datasets.extInd.custom.toast.save_failed')}: ${err.message}`)
      }
    },
  })

  const handleSave = () => {
    resetFeedback()
    // Light client-side checks — the backend precheck remains source of truth.
    const errors: Partial<Record<FieldKey, string>> = {}
    if (mode === 'create' && !/^[a-z_0-9]+$/.test(form.indicatorKey.trim())) {
      errors.indicator_key = t('page.datasets.extInd.custom.form.indicator_key_invalid')
    }
    if (!form.urlTemplate.trim()) errors.url_template = t('page.datasets.extInd.custom.form.required')
    if (!form.recordsPath.trim()) errors.records_path = t('page.datasets.extInd.custom.form.required')
    if (!form.fieldTradeDate.trim()) errors.field_trade_date = t('page.datasets.extInd.custom.form.required')
    if (!form.fieldValue.trim()) errors.field_value = t('page.datasets.extInd.custom.form.required')
    // Redacted echo values must be re-entered before a dirty config replaces
    // the stored one (otherwise the mask literal would be persisted as the
    // real secret — see component docstring).
    const configDirty = JSON.stringify(buildSourceConfig(form)) !== configSnapshot
    if (mode === 'edit' && configDirty && form.headers.some(h => h.value === REDACTED)) {
      errors.headers = t('page.datasets.extInd.custom.redacted_required')
    }
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return
    saveMutation.mutate()
  }

  const hasRedacted = form.headers.some(h => h.value === REDACTED)
  const anyBusy = saveMutation.isPending || loadingEdit

  // ── Row list (idle mode) ─────────────────────────────────────────────────
  if (mode === 'idle') {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">
            {t('page.datasets.extInd.custom.total', { count: customRows.length })}
          </span>
          <div className="flex gap-2">
            <button className="btn-secondary text-xs" onClick={() => void catalog.refetch()} disabled={catalog.isRefetching}>
              {catalog.isRefetching ? t('common.loading') : t('page.datasets.extInd.catalog.refresh')}
            </button>
            <button className="btn-primary text-xs" onClick={startCreate}>
              {t('page.datasets.extInd.custom.new')}
            </button>
          </div>
        </div>
        <DataState
          isLoading={catalog.isLoading}
          error={catalog.error}
          isEmpty={!customRows.length}
          emptyText={t('page.datasets.extInd.custom.empty')}
        >
          <div className="card p-0 overflow-hidden overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-gray-50">
                <tr>
                  <th className="table-th text-left">{t('page.datasets.extInd.column.key')}</th>
                  <th className="table-th text-left">{t('page.datasets.extInd.column.displayName')}</th>
                  <th className="table-th text-left">{t('page.datasets.extInd.column.frequency')}</th>
                  <th className="table-th text-left">{t('page.datasets.extInd.column.latestDate')}</th>
                  <th className="table-th text-left">{t('page.datasets.extInd.column.status')}</th>
                  <th className="table-th text-right">{t('page.datasets.extInd.column.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {customRows.map(item => (
                  <tr key={item.indicator_key} className="table-row">
                    <td className="table-td font-medium">{item.indicator_key}</td>
                    <td className="table-td">{item.display_name}</td>
                    <td className="table-td text-gray-500">{item.frequency}</td>
                    <td className="table-td">{item.latest_trade_date ?? '—'}</td>
                    <td className="table-td text-gray-500">{item.last_status ?? '—'}</td>
                    <td className="table-td text-right">
                      <button
                        className="text-brand-600 hover:underline"
                        disabled={anyBusy}
                        onClick={() => void startEdit(item.indicator_key)}
                      >
                        {t('page.datasets.extInd.action.edit')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </div>
    )
  }

  // ── Form (create / edit) ─────────────────────────────────────────────────
  const cfgPrefix = 'page.datasets.extInd.custom'
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">
          {mode === 'create'
            ? t(`${cfgPrefix}.new_title`)
            : t(`${cfgPrefix}.edit_title`, { key: editKey })}
        </h3>
        <button className="btn-secondary text-xs" onClick={() => setMode('idle')} disabled={anyBusy}>
          {t('page.datasets.extInd.edit.cancel')}
        </button>
      </div>

      {saveError && <StageErrorBanner stage={saveError.stage} message={saveError.message} />}

      <div className="card p-4 space-y-4">
        {/* Metadata */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <Label labelKey={`${cfgPrefix}.form.indicator_key`} hintKey={`${cfgPrefix}.form.indicator_key_hint`} />
            <input
              value={form.indicatorKey}
              onChange={e => set({ indicatorKey: e.target.value })}
              disabled={mode === 'edit'}
              placeholder="my_indicator"
              className="input-field text-sm w-full disabled:bg-gray-100 disabled:text-gray-500"
            />
            <FieldError msg={fieldErrors.indicator_key} />
          </div>
          <div>
            <Label labelKey="page.datasets.extInd.edit.display_name" />
            <input value={form.displayName} onChange={e => set({ displayName: e.target.value })} className="input-field text-sm w-full" />
          </div>
          <div>
            <Label labelKey="page.datasets.extInd.edit.unit" />
            <input value={form.unit} onChange={e => set({ unit: e.target.value })} className="input-field text-sm w-full" />
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <div>
            <Label labelKey="page.datasets.extInd.edit.frequency" />
            <select value={form.frequency} onChange={e => set({ frequency: e.target.value as CustomFormState['frequency'] })} className="input-field text-sm w-full">
              <option value="daily">{t('page.datasets.extInd.edit.daily')}</option>
              <option value="weekly">{t('page.datasets.extInd.edit.weekly')}</option>
              <option value="monthly">{t('page.datasets.extInd.edit.monthly')}</option>
            </select>
          </div>
          <div>
            <Label labelKey="page.datasets.extInd.edit.available_date_rule" />
            <select value={form.rule} onChange={e => set({ rule: e.target.value as 'A' | 'B' })} className="input-field text-sm w-full">
              <option value="A">{t('page.datasets.extInd.edit.rule_a')}</option>
              <option value="B">{t('page.datasets.extInd.edit.rule_b')}</option>
            </select>
          </div>
          <div>
            <Label labelKey="page.datasets.extInd.edit.backfill_start" />
            <input type="date" value={form.backfillStart} onChange={e => set({ backfillStart: e.target.value })} className="input-field text-sm w-full" />
          </div>
          <div className="md:col-span-1">
            <Label labelKey="page.datasets.extInd.edit.description" />
            <input value={form.description} onChange={e => set({ description: e.target.value })} className="input-field text-sm w-full" />
          </div>
        </div>

        {/* Request */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <div>
            <Label labelKey={`${cfgPrefix}.form.method`} />
            <select value={form.method} onChange={e => set({ method: e.target.value as 'GET' | 'POST' })} className="input-field text-sm w-full">
              <option value="GET">GET</option>
              <option value="POST">POST</option>
            </select>
            <FieldError msg={fieldErrors.method} />
          </div>
          <div className="md:col-span-3">
            <Label labelKey={`${cfgPrefix}.form.url_template`} hintKey={`${cfgPrefix}.form.url_template_hint`} />
            <input value={form.urlTemplate} onChange={e => set({ urlTemplate: e.target.value })} placeholder="https://api.example.com/v1/data?d={date}" className="input-field text-sm w-full" />
            <FieldError msg={fieldErrors.url_template} />
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <Label labelKey={`${cfgPrefix}.form.date_param_style`} />
            <select value={form.dateParamStyle} onChange={e => set({ dateParamStyle: e.target.value as CustomFormState['dateParamStyle'] })} className="input-field text-sm w-full">
              <option value="yyyymmdd">{t(`${cfgPrefix}.form.style_yyyymmdd`)}</option>
              <option value="yyyy-mm-dd">{t(`${cfgPrefix}.form.style_iso`)}</option>
              <option value="none">{t(`${cfgPrefix}.form.style_none`)}</option>
            </select>
            <FieldError msg={fieldErrors.date_param_style} />
          </div>
          <div>
            <Label labelKey={`${cfgPrefix}.form.records_path`} hintKey={`${cfgPrefix}.form.records_path_hint`} />
            <input value={form.recordsPath} onChange={e => set({ recordsPath: e.target.value })} className="input-field text-sm w-full" />
            <FieldError msg={fieldErrors.records_path} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label labelKey={`${cfgPrefix}.form.field_trade_date`} />
              <input value={form.fieldTradeDate} onChange={e => set({ fieldTradeDate: e.target.value })} className="input-field text-sm w-full" />
              <FieldError msg={fieldErrors.field_trade_date} />
            </div>
            <div>
              <Label labelKey={`${cfgPrefix}.form.field_value`} />
              <input value={form.fieldValue} onChange={e => set({ fieldValue: e.target.value })} className="input-field text-sm w-full" />
              <FieldError msg={fieldErrors.field_value} />
            </div>
          </div>
        </div>

        {/* Headers / params */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <Label labelKey={`${cfgPrefix}.form.headers`} hintKey={`${cfgPrefix}.form.kv_hint`} />
            <KVEditor
              rows={form.headers}
              onChange={headers => set({ headers })}
              addLabel={t(`${cfgPrefix}.form.kv_add`)}
            />
            {hasRedacted && (
              <p className="text-[10px] text-amber-600 mt-1">{t(`${cfgPrefix}.redacted_hint`)}</p>
            )}
            <FieldError msg={fieldErrors.headers} />
          </div>
          <div>
            <Label labelKey={`${cfgPrefix}.form.params`} hintKey={`${cfgPrefix}.form.kv_hint`} />
            <KVEditor
              rows={form.params}
              onChange={params => set({ params })}
              addLabel={t(`${cfgPrefix}.form.kv_add`)}
            />
          </div>
        </div>

        {/* Guards & limits */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <label className="flex items-center gap-2 text-sm text-gray-700 pb-1.5">
            <input type="checkbox" checked={form.allowInsecure} onChange={e => set({ allowInsecure: e.target.checked })} />
            {t(`${cfgPrefix}.form.allow_insecure_http`)}
          </label>
          <div>
            <Label labelKey={`${cfgPrefix}.form.timeout_connect`} />
            <input type="number" min={1} max={60} value={form.timeoutConnect} onChange={e => set({ timeoutConnect: e.target.value })} className="input-field text-sm w-full" />
          </div>
          <div>
            <Label labelKey={`${cfgPrefix}.form.timeout_total`} />
            <input type="number" min={1} max={120} value={form.timeoutTotal} onChange={e => set({ timeoutTotal: e.target.value })} className="input-field text-sm w-full" />
          </div>
          <div>
            <Label labelKey={`${cfgPrefix}.form.max_bytes`} />
            <input type="number" min={1} value={form.maxBytes} onChange={e => set({ maxBytes: e.target.value })} className="input-field text-sm w-full" />
          </div>
        </div>

        <div className="flex flex-wrap gap-3 justify-end pt-2 border-t border-gray-100">
          <button
            className="btn-secondary text-sm"
            disabled={testMutation.isPending}
            onClick={() => {
              setTestResult(null)
              setTestError(null)
              testMutation.mutate()
            }}
          >
            {testMutation.isPending ? t('common.loading') : t(`${cfgPrefix}.test`)}
          </button>
          <button className="btn-primary text-sm" disabled={saveMutation.isPending} onClick={handleSave}>
            {saveMutation.isPending ? t('common.loading') : t(`${cfgPrefix}.save`)}
          </button>
        </div>
      </div>

      {/* Test result: sample table or stage-mapped error */}
      {testResult && (
        <div className="card p-4 space-y-2">
          <h4 className="text-sm font-medium text-green-700">{t(`${cfgPrefix}.test_ok`)}</h4>
          <p className="text-xs text-gray-500 break-all">
            {t(`${cfgPrefix}.resolved_url`)}: {testResult.diagnostics.resolved_url}
            ｜ {t(`${cfgPrefix}.rows_parsed`)}: {testResult.diagnostics.rows_parsed}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-gray-50">
                <tr>
                  <th className="table-th text-left">trade_date</th>
                  <th className="table-th text-left">value</th>
                </tr>
              </thead>
              <tbody>
                {testResult.sample.map((r, i) => (
                  <tr key={`${r.trade_date}-${i}`} className="table-row">
                    <td className="table-td">{r.trade_date}</td>
                    <td className="table-td">{r.value ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {testError && <StageErrorBanner stage={testError.stage} message={testError.message} />}
    </div>
  )
}
