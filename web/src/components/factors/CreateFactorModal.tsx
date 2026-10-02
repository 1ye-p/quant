/**
 * Modal for creating a custom factor.
 * Supports DSL and Polars expression types with preview.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { customFactorApi } from '@/lib/api'
import { toast } from 'sonner'
import { FactorDSLEditor } from './FactorDSLEditor'

interface CreateFactorModalProps {
  onClose: () => void
}

export function CreateFactorModal({ onClose }: CreateFactorModalProps) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [expr, setExpr] = useState('')
  const [desc, setDesc] = useState('')
  const [exprType, setExprType] = useState<'polars' | 'dsl'>('dsl')
  const [preview, setPreview] = useState<{
    valid: boolean; error: string | null
    preview: { asset_id: string; trade_date: string; value: number | null }[]
  } | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)

  const createMutation = useMutation({
    mutationFn: (body: { name: string; expression: string; description?: string; expression_type?: string }) =>
      customFactorApi.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['factors', 'definitions'] })
      qc.invalidateQueries({ queryKey: ['factors', 'available'] })
      toast.success(t('component.factors.create_factor_modal.toast_created'))
      onClose()
    },
    onError: (e: Error) => toast.error(t('component.factors.create_factor_modal.toast_create_failed', { message: e.message })),
  })

  async function handlePreview() {
    if (!expr.trim()) return
    setPreviewLoading(true)
    try {
      const result = await customFactorApi.preview({ expression: expr })
      setPreview(result)
    } catch (e: unknown) {
      setPreview({ valid: false, error: String(e), preview: [] })
    } finally {
      setPreviewLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
      <div className={`bg-white rounded-xl shadow-xl w-full ${exprType === 'dsl' ? 'max-w-4xl' : 'max-w-2xl'}`}>
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="font-semibold text-gray-900">{t('component.factors.create_factor_modal.title')}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <div className="p-4 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">{t('component.factors.create_factor_modal.label_name')} <span className="text-red-500">*</span></label>
              <input value={name} onChange={e => setName(e.target.value)}
                placeholder={t('component.factors.create_factor_modal.ph_name')} className="input w-full text-sm" />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">{t('component.factors.create_factor_modal.label_desc')}</label>
              <input value={desc} onChange={e => setDesc(e.target.value)}
                placeholder={t('component.factors.create_factor_modal.ph_desc')} className="input w-full text-sm" />
            </div>
          </div>
          <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-0.5 w-fit">
            {(['dsl', 'polars'] as const).map(tab => (
              <button key={tab}
                className={`px-3 py-1 text-xs rounded-md transition-colors ${exprType === tab ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                onClick={() => setExprType(tab)}>
                {tab === 'dsl' ? t('component.factors.create_factor_modal.tab_dsl') : t('component.factors.create_factor_modal.tab_polars')}
              </button>
            ))}
          </div>
          {exprType === 'dsl' ? (
            <FactorDSLEditor
              onSave={(expression, dslName) => {
                setExpr(expression)
                if (dslName && !name) setName(dslName)
                createMutation.mutate({
                  name: name || dslName || 'dsl_factor',
                  expression,
                  description: desc,
                  expression_type: 'dsl',
                })
              }}
              initialExpression={expr}
            />
          ) : (
            <>
              <div>
                <label className="block text-xs text-gray-600 mb-1">{t('component.factors.create_factor_modal.label_polars_expr')} <span className="text-red-500">*</span></label>
                <textarea rows={4} value={expr}
                  onChange={e => { setExpr(e.target.value); setPreview(null) }}
                  placeholder={t('component.factors.create_factor_modal.ph_polars')}
                  className="w-full font-mono text-sm border rounded p-2 focus:outline-none focus:ring-1 focus:ring-brand-500" />
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={handlePreview}
                  disabled={previewLoading || !expr.trim()} className="btn-secondary text-xs disabled:opacity-40">
                  {previewLoading ? t('component.factors.create_factor_modal.btn_previewing') : t('component.factors.create_factor_modal.btn_preview')}
                </button>
                {preview && (
                  <span className={`text-xs ${preview.valid ? 'text-green-600' : 'text-red-500'}`}>
                    {preview.valid ? t('component.factors.create_factor_modal.valid') : `${t('component.factors.create_factor_modal.invalid_prefix')} ${preview.error}`}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
        <div className="flex justify-end gap-2 p-4 border-t">
          <button onClick={onClose} className="btn-secondary text-sm">{t('component.factors.create_factor_modal.btn_cancel')}</button>
          {exprType === 'polars' && (
            <button onClick={() => createMutation.mutate({ name, expression: expr, description: desc })}
              disabled={!name.trim() || !expr.trim() || createMutation.isPending || preview?.valid === false}
              className="btn-primary text-sm disabled:opacity-40">
              {createMutation.isPending ? t('component.factors.create_factor_modal.btn_creating') : t('component.factors.create_factor_modal.btn_create')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
