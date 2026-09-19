/**
 * External indicators CSV import wizard (Phase 1 T3).
 *
 * Flow: upload CSV -> column mapping -> preview first 10 rows ->
 * PIT availability question (A/B/C, C defaults to conservative B) ->
 * execute import -> render ImportReport.
 */

import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMutation } from '@tanstack/react-query'
import { datasetsApi, type ExtIndImportReport } from '@/lib/api'

type PitRule = 'A' | 'B' | 'C'

export function ExternalIndicatorsImportPage() {
  const { t } = useTranslation()
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [source, setSource] = useState('')
  const [indicatorKey, setIndicatorKey] = useState('')
  const [dateCol, setDateCol] = useState('')
  const [valueCol, setValueCol] = useState('')
  const [assetCol, setAssetCol] = useState('')
  const [pitRule, setPitRule] = useState<PitRule>('C')

  const preview = useMutation({
    mutationFn: (f: File) => datasetsApi.previewExternalIndicators(f),
  })

  const doImport = useMutation({
    mutationFn: (args: { file: File; rule: PitRule; sourceParam: string }) =>
      datasetsApi.importExternalIndicators(args.file, {
        source: args.sourceParam,
        indicator_key: indicatorKey,
        column_map: assetCol
          ? { [dateCol]: 'trade_date', [valueCol]: 'value', [assetCol]: 'asset_id' }
          : { [dateCol]: 'trade_date', [valueCol]: 'value' },
        // C ("not sure") -> conservative B: at most one day of staleness, never look-ahead
        available_date_rule: args.rule === 'A' ? 'A' : 'B',
      }),
  })

  const columns = preview.data?.columns ?? []

  const canImport =
    !!file && !!source.trim() && !!indicatorKey.trim() && !!dateCol && !!valueCol

  const handleFile = (f: File | null) => {
    setFile(f)
    setDateCol(''); setValueCol(''); setAssetCol('')
    doImport.reset()
    if (f) preview.mutate(f)
  }

  const colSelect = (
    value: string,
    onChange: (v: string) => void,
    allowNone: boolean,
  ) => (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="input-field text-sm w-full"
      disabled={!columns.length}
    >
      <option value="">{allowNone ? t('page.datasets.ext_ind.mapping.none') : t('page.datasets.ext_ind.mapping.select')}</option>
      {columns.map(c => (
        <option key={c} value={c}>{c}</option>
      ))}
    </select>
  )

  const report: ExtIndImportReport | undefined = doImport.data

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">{t('page.datasets.ext_ind.title')}</h1>
          <p className="page-subtitle">{t('page.datasets.ext_ind.subtitle')}</p>
        </div>
        <Link to="/datasets" className="btn-secondary text-xs">{t('page.datasets.ext_ind.back')}</Link>
      </div>

      {/* Step 1: file */}
      <div className="card p-4 space-y-3">
        <h3 className="text-sm font-medium text-gray-700">1. {t('page.datasets.ext_ind.step.file')}</h3>
        <input
          ref={fileRef}
          type="file"
          accept=".csv"
          className="hidden"
          onChange={e => handleFile(e.target.files?.[0] ?? null)}
        />
        <div className="flex items-center gap-3">
          <button className="btn-primary text-sm" onClick={() => fileRef.current?.click()}>
            {file ? file.name : t('page.datasets.ext_ind.file.choose')}
          </button>
          {preview.data && (
            <span className="text-xs text-gray-500">
              {t('page.datasets.ext_ind.file.row_count')}: {preview.data.total_rows.toLocaleString()}
            </span>
          )}
          {preview.isPending && <span className="text-xs text-gray-400">{t('common.loading')}</span>}
          {preview.error && <span className="text-xs text-red-500">{(preview.error as Error).message}</span>}
        </div>
      </div>

      {/* Step 2: config + mapping */}
      {file && (
        <div className="card p-4 space-y-4">
          <h3 className="text-sm font-medium text-gray-700">2. {t('page.datasets.ext_ind.step.mapping')}</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.ext_ind.config.source')}</label>
              <input value={source} onChange={e => setSource(e.target.value)} className="input-field text-sm w-full" placeholder="manual_csv" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.ext_ind.config.indicator_key')}</label>
              <input value={indicatorKey} onChange={e => setIndicatorKey(e.target.value)} className="input-field text-sm w-full" placeholder="margin_balance" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.ext_ind.mapping.trade_date')} *</label>
              {colSelect(dateCol, setDateCol, false)}
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.ext_ind.mapping.value')} *</label>
              {colSelect(valueCol, setValueCol, false)}
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t('page.datasets.ext_ind.mapping.asset_id')}</label>
              {colSelect(assetCol, setAssetCol, true)}
              <p className="text-[10px] text-gray-400 mt-1">{t('page.datasets.ext_ind.mapping.asset_hint')}</p>
            </div>
          </div>

          {/* Preview table */}
          {preview.data && (
            <div className="overflow-x-auto border rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-gray-50">
                  <tr>
                    {preview.data.columns.map(c => (
                      <th key={c} className="table-th text-left">{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, i) => (
                    <tr key={i}>
                      {preview.data!.columns.map(c => (
                        <td key={c} className="table-td">{String(row[c] ?? '')}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Step 3: PIT question */}
      {canImport && (
        <div className="card p-4 space-y-3">
          <h3 className="text-sm font-medium text-gray-700">3. {t('page.datasets.ext_ind.pit.question')}</h3>
          <div className="space-y-2">
            {(['A', 'B', 'C'] as const).map(rule => (
              <label key={rule} className={`flex items-start gap-2 p-2 rounded-lg border cursor-pointer text-sm ${pitRule === rule ? 'border-brand-500 bg-brand-50' : 'border-gray-200'}`}>
                <input
                  type="radio"
                  name="pit-rule"
                  className="mt-1"
                  checked={pitRule === rule}
                  onChange={() => setPitRule(rule)}
                />
                <span>
                  <strong>{rule}.</strong> {t(`page.datasets.ext_ind.pit.${rule.toLowerCase()}`)}
                  {rule === 'C' && (
                    <span className="block text-xs text-amber-600 mt-0.5">{t('page.datasets.ext_ind.pit.c_hint')}</span>
                  )}
                </span>
              </label>
            ))}
          </div>
          <button
            className="btn-primary text-sm"
            disabled={doImport.isPending}
            onClick={() =>
              doImport.mutate({ file: file!, rule: pitRule, sourceParam: source.trim() })
            }
          >
            {doImport.isPending ? t('common.loading') : t('page.datasets.ext_ind.btn.import')}
          </button>
          {doImport.error && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
              {(doImport.error as Error).message}
            </div>
          )}
        </div>
      )}

      {/* Step 4: report */}
      {report && (
        <div className="card p-4 space-y-3">
          <h3 className="text-sm font-medium text-gray-700">4. {t('page.datasets.ext_ind.report.title')}</h3>
          <div className="grid grid-cols-4 gap-3">
            {(['total', 'inserted', 'deduped', 'skipped'] as const).map(k => (
              <div key={k} className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500">{t(`page.datasets.ext_ind.report.${k}`)}</div>
                <div className="text-lg font-semibold mt-1">{report[k].toLocaleString()}</div>
              </div>
            ))}
          </div>
          {report.skipped_reasons.length > 0 && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2 max-h-40 overflow-y-auto">
              {report.skipped_reasons.map((r, i) => <div key={i}>{r}</div>)}
            </div>
          )}
          {report.warnings.length > 0 && (
            <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 max-h-40 overflow-y-auto">
              {report.warnings.map((w, i) => <div key={i}>{w}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
