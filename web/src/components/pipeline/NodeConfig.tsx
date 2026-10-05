/**
 * NodeConfig — Side panel for configuring a pipeline node.
 *
 * Shows different config fields depending on nodeType.
 */
import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import type { PipelineNodeData } from './PipelineDAG'

// ── Per-type field definitions ───────────────────────────────────────────────

interface FieldDef {
  key: string
  // i18n key under component.pipeline.config_field.*
  labelKey?: string
  type: 'text' | 'number' | 'date' | 'select'
  placeholder?: string
  options?: { value: string; labelKey?: string }[]
}

const FIELDS_BY_TYPE: Record<string, FieldDef[]> = {
  data: [
    { key: 'source', labelKey: 'source', type: 'select', options: [
      { value: 'tdx', labelKey: 'source_tdx' },
      { value: 'tushare', labelKey: 'source_tushare' },
      { value: 'akshare', labelKey: 'source_akshare' },
    ]},
    { key: 'start_date', labelKey: 'start_date', type: 'date' },
    { key: 'end_date', labelKey: 'end_date', type: 'date' },
  ],
  factor: [
    { key: 'factor_set', labelKey: 'factor_set', type: 'select', options: [
      { value: 'alpha158', labelKey: 'factor_alpha158' },
      { value: 'alpha101', labelKey: 'factor_alpha101' },
      { value: 'gtja191', labelKey: 'factor_gtja191' },
      { value: 'all', labelKey: 'factor_all' },
    ]},
    { key: 'universe', labelKey: 'universe', type: 'select', options: [
      { value: 'idx_hs300', labelKey: 'universe_hs300' },
      { value: 'idx_zz500', labelKey: 'universe_zz500' },
      { value: 'idx_zz1000', labelKey: 'universe_zz1000' },
      { value: 'all', labelKey: 'universe_all' },
    ]},
  ],
  model: [
    { key: 'model_name', labelKey: 'model_name', type: 'select', options: [
      { value: 'lightgbm', labelKey: 'model_lightgbm' },
      { value: 'xgboost', labelKey: 'model_xgboost' },
      { value: 'random_forest', labelKey: 'model_random_forest' },
    ]},
    { key: 'n_folds', labelKey: 'n_folds', type: 'number', placeholder: '5' },
  ],
  backtest: [
    { key: 'strategy_type', labelKey: 'strategy_type', type: 'select', options: [
      { value: 'top_n', labelKey: 'strategy_top_n' },
      { value: 'ml_signal', labelKey: 'strategy_ml_signal' },
      { value: 'multi_factor', labelKey: 'strategy_multi_factor' },
    ]},
    { key: 'top_n', labelKey: 'top_n', type: 'number', placeholder: '10' },
  ],
  optimize: [
    { key: 'method', labelKey: 'method', type: 'select', options: [
      { value: 'mvo', labelKey: 'method_mvo' },
      { value: 'risk_parity', labelKey: 'method_risk_parity' },
      { value: 'min_variance', labelKey: 'method_min_variance' },
    ]},
    { key: 'max_weight', labelKey: 'max_weight', type: 'number', placeholder: '0.1' },
  ],
}

// ── Props ───────────────────────────────────────────────────────────────────

export interface NodeConfigProps {
  nodeId: string
  data: PipelineNodeData
  onSave: (nodeId: string, config: Record<string, unknown>) => void
  onClose: () => void
}

export function NodeConfig({ nodeId, data, onSave, onClose }: NodeConfigProps) {
  const { t } = useTranslation()
  const fields = FIELDS_BY_TYPE[data.nodeType] ?? []
  const [values, setValues] = useState<Record<string, string>>({})

  // Initialize from existing config
  useEffect(() => {
    const initial: Record<string, string> = {}
    for (const f of fields) {
      const v = data.config?.[f.key]
      initial[f.key] = v != null ? String(v) : ''
    }
    setValues(initial)
  }, [data.config, fields])

  const handleChange = (key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }))
  }

  const handleSave = () => {
    // Convert numeric fields
    const config: Record<string, unknown> = {}
    for (const f of fields) {
      const raw = values[f.key]
      if (raw === '') continue
      config[f.key] = f.type === 'number' ? Number(raw) : raw
    }
    onSave(nodeId, config)
  }

  const fieldLabel = (f: FieldDef): string =>
    f.labelKey ? t(`component.pipeline.config_field.${f.labelKey}`) : f.key
  const optionLabel = (o: { value: string; labelKey?: string }): string =>
    o.labelKey ? t(`component.pipeline.config_option.${o.labelKey}`) : o.value

  return (
    <div className="fixed inset-y-0 right-0 w-80 bg-white shadow-xl border-l border-gray-200 z-50 flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-800">
          {t('component.pipeline.config_title', { label: data.label })}
        </h3>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600 text-lg leading-none"
          aria-label={t('component.pipeline.close')}
        >
          &times;
        </button>
      </div>

      {/* Fields */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {fields.length === 0 ? (
          <p className="text-sm text-gray-400">{t('component.pipeline.no_config_fields')}</p>
        ) : (
          fields.map((f) => (
            <div key={f.key}>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                {fieldLabel(f)}
              </label>
              {f.type === 'select' ? (
                <select
                  value={values[f.key] ?? ''}
                  onChange={(e) => handleChange(f.key, e.target.value)}
                  className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="">{t('component.pipeline.select_placeholder')}</option>
                  {f.options?.map((o) => (
                    <option key={o.value} value={o.value}>{optionLabel(o)}</option>
                  ))}
                </select>
              ) : (
                <input
                  type={f.type}
                  value={values[f.key] ?? ''}
                  onChange={(e) => handleChange(f.key, e.target.value)}
                  placeholder={f.placeholder}
                  className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              )}
            </div>
          ))
        )}
      </div>

      {/* Actions */}
      <div className="px-4 py-3 border-t border-gray-100 flex gap-2">
        <button
          onClick={onClose}
          className="flex-1 btn-secondary text-sm"
        >
          {t('common.cancel')}
        </button>
        <button
          onClick={handleSave}
          className="flex-1 btn-primary text-sm"
        >
          {t('common.save')}
        </button>
      </div>
    </div>
  )
}
