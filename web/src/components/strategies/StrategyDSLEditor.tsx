/**
 * L2 策略 DSL 编辑器 — 分段表单 + YAML 双模（Task 6 / P3S-3）。
 *
 * - 表单模式：基本信息 / score / position / risk / regime（三模式）分段编辑
 * - YAML 模式：Monaco（lazy，复用 FactorDSLEditor 范式），双向同步
 * - 校验：strategyDslSchema zod safeParse → 中文错误按字段路径映射红框
 * - 保存：strategy_type="DSL" + dsl_spec 作为标准策略 config_text
 */
import { useState, useMemo, lazy, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { strategiesApi, factorsApi, dslApi, datasetsApi } from '@/lib/api'
import { extendedQueryKeys } from '@/lib/queryKeys'
import { SIZER_NAMES, POLICY_NAMES, type StrategyDsl } from '@/lib/strategyDslSchema'
import {
  DEFAULT_DSL_FORM,
  formToYaml,
  yamlToForm,
  validateDsl,
  dslToStrategyConfig,
  type StrategyDslFormState,
} from '@/lib/strategyDslYaml'

const Editor = lazy(() => import('@monaco-editor/react'))

const FREQUENCIES = ['daily', 'weekly', 'monthly'] as const
const REGIME_MODES = ['threshold', 'switch', 'continuous'] as const

interface Props {
  /** 'new' 或已有策略 id（config 含 dsl_spec 时进入编辑） */
  strategyId: string | 'new'
  /** 现有策略的 config_text（JSON，含 dsl_spec）；新建时可传 '' */
  initialConfig?: string
  onClose: () => void
  /** 保存成功后回调（唤起回测） */
  onSaved: (strategyId: string, configText: string) => void
}

/** 从已有 config_text 提取 dsl_spec → 表单态。 */
function configToForm(configText: string): StrategyDslFormState | null {
  try {
    const parsed = JSON.parse(configText)
    if (parsed?.strategy_type !== 'DSL' || !parsed?.dsl_spec) return null
    const d = parsed.dsl_spec as StrategyDsl
    return {
      name: d.name ?? '',
      universe: d.universe ?? 'all',
      frequency: d.frequency ?? 'daily',
      benchmark: d.benchmark ?? '',
      score: (d.score ?? []).map((s) => ({ factor: s.factor, weight: s.weight })),
      position: {
        method: d.position?.method ?? 'equal_weight',
        params: d.position?.params ?? {},
        constraints: d.position?.constraints ?? {},
      },
      risk: (d.risk ?? []).map((r) => ({ type: r.type, params: r.params ?? {} })),
      regime: d.regime
        ? {
            mode: d.regime.mode,
            indicators: d.regime.indicators ?? {},
            initial: d.regime.initial ?? null,
            reevaluate: d.regime.reevaluate ?? null,
            states: d.regime.states ?? null,
            rules: d.regime.rules?.map((r) => ({
              when: r.when ?? null,
              position_scale: r.position_scale,
            })) ?? null,
            scale_expr: d.regime.scale_expr ?? null,
          }
        : null,
    }
  } catch {
    return null
  }
}

const errClass = (hasErr: boolean) => hasErr ? 'input border-red-500 ring-1 ring-red-500' : 'input'

/** 通用键值对编辑器（params / indicators / constraints）。 */
function KVEditor({
  value,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
}: {
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  keyPlaceholder: string
  valuePlaceholder: string
}) {
  const { t } = useTranslation()
  const entries = Object.entries(value)
  return (
    <div className="space-y-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-1.5 items-center">
          <input
            className="input flex-1 text-xs"
            value={k}
            placeholder={keyPlaceholder}
            onChange={(e) => {
              const next: Record<string, unknown> = {}
              for (const [kk, vv] of entries) next[kk === k ? e.target.value : kk] = vv
              delete next[k]
              next[e.target.value] = v
              onChange(next)
            }}
          />
          <input
            className="input flex-1 text-xs"
            value={String(v)}
            placeholder={valuePlaceholder}
            onChange={(e) => {
              const raw = e.target.value
              // 数字自动转 number，其余保持字符串（params 以标量为主）
              const parsed = raw !== '' && !Number.isNaN(Number(raw)) ? Number(raw) : raw
              onChange({ ...value, [k]: parsed })
            }}
          />
          <button
            className="text-gray-400 hover:text-red-500 text-sm px-1"
            onClick={() => {
              const next = { ...value }
              delete next[k]
              onChange(next)
            }}
            title={t('common.delete')}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        className="text-xs text-brand-600 hover:underline"
        onClick={() => onChange({ ...value, '': '' })}
      >
        + {t('component.strategy_dsl.add_kv')}
      </button>
    </div>
  )
}

/** 表达式旁的函数提示 popover（轻量，非自动补全）。 */
function FunctionHintButton() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const { data } = useQuery({
    queryKey: extendedQueryKeys.dsl.functions,
    queryFn: dslApi.functions,
    staleTime: 300_000,
    enabled: open,
  })
  return (
    <span className="relative inline-block">
      <button
        type="button"
        className="text-xs text-brand-600 hover:underline ml-1"
        onClick={() => setOpen(!open)}
      >
        ƒx
      </button>
      {open && (
        <div className="absolute z-20 left-0 mt-1 w-72 max-h-64 overflow-y-auto card p-2 text-xs shadow-lg">
          <div className="font-semibold text-gray-700 mb-1">
            {t('component.strategy_dsl.fn_hint_title')}
          </div>
          {(data?.functions ?? []).map((f) => (
            <div key={f.name} className="py-0.5 border-b border-gray-100 last:border-0">
              <code className="text-brand-600 font-mono">{f.name}</code>
              <span className="text-gray-400 ml-1">
                ({f.minArgs === f.maxArgs ? f.minArgs : `${f.minArgs}-${f.maxArgs}`})
              </span>
              <div className="text-gray-500">{f.description}</div>
            </div>
          ))}
          {!data && (
            <div className="text-gray-400">{t('component.strategy_dsl.fn_hint_loading')}</div>
          )}
        </div>
      )}
    </span>
  )
}

export function StrategyDSLEditor({ strategyId, initialConfig = '', onClose, onSaved }: Props) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [mode, setMode] = useState<'form' | 'yaml'>('form')
  const [form, setForm] = useState<StrategyDslFormState>(() => {
    const fromConfig = strategyId !== 'new' ? configToForm(initialConfig) : null
    return fromConfig ?? { ...DEFAULT_DSL_FORM, score: [{ factor: '', weight: 1 }] }
  })
  const [yamlText, setYamlText] = useState('')
  const [yamlSyntaxError, setYamlSyntaxError] = useState<string | null>(null)
  const [showErrors, setShowErrors] = useState(false)
  const [regimeOpen, setRegimeOpen] = useState(false)

  // 因子目录：score 下拉 + 存在性校验
  const { data: factorsCatalog } = useQuery({
    queryKey: ['factors', 'available'],
    queryFn: () => factorsApi.getAvailable(),
    staleTime: 300_000,
  })
  const { knownFactors, customFactors, factorOptions } = useMemo(() => {
    const known = new Set<string>()
    const custom = new Set<string>()
    const options: { value: string; label: string }[] = []
    for (const f of factorsCatalog?.factors ?? []) {
      if (f.is_custom) {
        custom.add(f.name)
        options.push({ value: `custom:${f.name}`, label: `custom:${f.name}` })
      } else {
        known.add(f.name)
        options.push({ value: f.name, label: f.label_zh ? `${f.name} · ${f.label_zh}` : f.name })
      }
    }
    return { knownFactors: known, customFactors: custom, factorOptions: options }
  }, [factorsCatalog])

  const { data: universes } = useQuery({
    queryKey: ['datasets', 'universes'],
    queryFn: datasetsApi.universes,
    staleTime: 300_000,
  })

  // YAML 模式下的语法解析（zod 校验基于解析后的 form）
  const parsedFromYaml = useMemo(
    () => (mode === 'yaml' ? yamlToForm(yamlText) : null),
    [mode, yamlText],
  )

  const validation = useMemo(() => {
    const target = mode === 'yaml' ? parsedFromYaml?.form : form
    if (!target) return { ok: false as const, errors: {} as Record<string, string>, dsl: undefined }
    return validateDsl(target, knownFactors, customFactors)
  }, [mode, form, parsedFromYaml, knownFactors, customFactors])

  const err = (path: string) => (showErrors || mode === 'yaml' ? validation.errors[path] : undefined)
  const errBox = (path: string) => {
    const m = err(path)
    return m ? <p className="mt-1 text-xs text-red-600">{m}</p> : null
  }

  function switchMode(next: 'form' | 'yaml') {
    if (next === 'yaml') {
      setYamlText(formToYaml(form))
      setYamlSyntaxError(null)
      setMode('yaml')
    } else {
      const result = yamlToForm(yamlText)
      if (!result.ok || !result.form) {
        setYamlSyntaxError(result.syntaxError ?? t('component.strategy_dsl.yaml_invalid'))
        toast.error(result.syntaxError ?? t('component.strategy_dsl.yaml_invalid'))
        return
      }
      setForm(result.form)
      setYamlSyntaxError(null)
      setMode('form')
    }
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const dsl = validation.dsl!
      const configText = dslToStrategyConfig(dsl)
      if (strategyId === 'new') {
        await strategiesApi.create({ strategy_id: dsl.name, config_text: configText })
      } else {
        await strategiesApi.update(strategyId, { config_text: configText })
      }
      return { id: strategyId === 'new' ? dsl.name : strategyId, configText }
    },
    onSuccess: ({ id, configText }) => {
      qc.invalidateQueries({ queryKey: extendedQueryKeys.strategies.list() })
      toast.success(t('component.strategy_dsl.saved_toast'))
      onSaved(id, configText)
    },
    onError: (e: Error) => {
      toast.error(t('component.strategy_dsl.save_failed', { message: e.message }))
    },
  })

  function handleSave() {
    setShowErrors(true)
    if (!validation.ok || !validation.dsl) {
      toast.error(t('component.strategy_dsl.validation_failed'))
      return
    }
    saveMutation.mutate()
  }

  const set = (patch: Partial<StrategyDslFormState>) => setForm((f) => ({ ...f, ...patch }))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-[860px] max-w-full max-h-[92vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="font-semibold text-gray-900">
            {strategyId === 'new'
              ? t('component.strategy_dsl.title_create')
              : t('component.strategy_dsl.title_edit', { id: strategyId })}
          </h2>
          <button className="text-gray-400 hover:text-gray-600" onClick={onClose}>✕</button>
        </div>

        {/* 模式切换 */}
        <div className="flex gap-1 px-4 pt-3">
          <button
            className={`px-3 py-1.5 text-sm rounded-t-lg border-b-2 ${
              mode === 'form' ? 'border-blue-600 text-blue-700 bg-blue-50' : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => switchMode('form')}
            disabled={mode === 'form'}
          >
            {t('component.strategy_dsl.mode_form')}
          </button>
          <button
            className={`px-3 py-1.5 text-sm rounded-t-lg border-b-2 ${
              mode === 'yaml' ? 'border-blue-600 text-blue-700 bg-blue-50' : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => switchMode('yaml')}
            disabled={mode === 'yaml'}
          >
            {t('component.strategy_dsl.mode_yaml')}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-5">
          {mode === 'yaml' ? (
            <>
              <Suspense fallback={<div className="h-[480px] bg-gray-50 animate-pulse rounded" />}>
                <Editor
                  height="480px"
                  language="yaml"
                  value={yamlText}
                  onChange={(v) => setYamlText(v ?? '')}
                  options={{
                    minimap: { enabled: false },
                    fontSize: 13,
                    scrollBeyondLastLine: false,
                    lineNumbers: 'on',
                  }}
                />
              </Suspense>
              {(yamlSyntaxError || (showErrors && !validation.ok)) && (
                <div className="text-sm text-red-600 bg-red-50 p-2 rounded space-y-1">
                  {yamlSyntaxError}
                  {showErrors && !validation.ok && !yamlSyntaxError &&
                    Object.entries(validation.errors).map(([path, message]) => (
                      <div key={path}>
                        <span className="font-mono text-xs">{path || 'root'}</span>: {message}
                      </div>
                    ))}
                </div>
              )}
            </>
          ) : (
            <>
              {/* ── 基本信息 ─────────────────────────────────────────── */}
              <section className="space-y-3">
                <h3 className="text-sm font-semibold text-gray-700">
                  {t('component.strategy_dsl.section_info')}
                </h3>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_name')}
                    </label>
                    <input
                      className={errClass(!!err('name'))}
                      value={form.name}
                      onChange={(e) => set({ name: e.target.value })}
                    />
                    {errBox('name')}
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_universe')}
                    </label>
                    <select
                      className="input"
                      value={form.universe}
                      onChange={(e) => set({ universe: e.target.value })}
                    >
                      {(universes?.predefined ?? []).map((u) => (
                        <option key={u.id} value={u.id}>{u.name}</option>
                      ))}
                      <option value="all">all</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_frequency')}
                    </label>
                    <div className="flex gap-4 text-sm pt-1.5">
                      {FREQUENCIES.map((f) => (
                        <label key={f} className="flex items-center gap-1.5 cursor-pointer">
                          <input
                            type="radio"
                            name="dsl-frequency"
                            checked={form.frequency === f}
                            onChange={() => set({ frequency: f })}
                            className="accent-blue-600"
                          />
                          {t(`component.strategy_dsl.freq_${f}`)}
                        </label>
                      ))}
                    </div>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_benchmark')}
                    </label>
                    <input
                      className="input"
                      placeholder={t('component.strategy_dsl.benchmark_placeholder')}
                      value={form.benchmark}
                      onChange={(e) => set({ benchmark: e.target.value })}
                    />
                  </div>
                </div>
              </section>

              {/* ── score ────────────────────────────────────────────── */}
              <section className="space-y-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  {t('component.strategy_dsl.section_score')}
                </h3>
                {errBox('score')}
                {form.score.map((item, i) => (
                  <div key={i} className="flex gap-2 items-start">
                    <div className="flex-1">
                      <select
                        className={errClass(!!err(`score.${i}.factor`))}
                        value={item.factor}
                        onChange={(e) => {
                          const score = [...form.score]
                          score[i] = { ...item, factor: e.target.value }
                          set({ score })
                        }}
                      >
                        <option value="">{t('component.strategy_dsl.factor_select')}</option>
                        {factorOptions.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                        {item.factor && !factorOptions.some((o) => o.value === item.factor) && (
                          <option value={item.factor}>{item.factor}</option>
                        )}
                      </select>
                      {errBox(`score.${i}.factor`)}
                    </div>
                    <div className="w-28">
                      <input
                        type="number"
                        step="0.1"
                        className={errClass(!!err(`score.${i}.weight`))}
                        value={item.weight}
                        onChange={(e) => {
                          const score = [...form.score]
                          score[i] = { ...item, weight: Number(e.target.value) }
                          set({ score })
                        }}
                      />
                      {errBox(`score.${i}.weight`)}
                    </div>
                    <button
                      className="text-gray-400 hover:text-red-500 text-sm px-1 mt-2"
                      disabled={form.score.length <= 1}
                      onClick={() => set({ score: form.score.filter((_, j) => j !== i) })}
                    >
                      ✕
                    </button>
                  </div>
                ))}
                <button
                  className="text-xs text-brand-600 hover:underline"
                  onClick={() => set({ score: [...form.score, { factor: '', weight: 1 }] })}
                >
                  + {t('component.strategy_dsl.add_factor')}
                </button>
              </section>

              {/* ── position ──────────────────────────────────────────── */}
              <section className="space-y-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  {t('component.strategy_dsl.section_position')}
                </h3>
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_method')}
                    </label>
                    <select
                      className={errClass(!!err('position.method'))}
                      value={form.position.method}
                      onChange={(e) => set({ position: { ...form.position, method: e.target.value } })}
                    >
                      {SIZER_NAMES.map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                    {errBox('position.method')}
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_params')}
                    </label>
                    <KVEditor
                      value={form.position.params}
                      onChange={(params) => set({ position: { ...form.position, params } })}
                      keyPlaceholder="key"
                      valuePlaceholder="value"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">
                      {t('component.strategy_dsl.label_constraints')}
                    </label>
                    <KVEditor
                      value={form.position.constraints}
                      onChange={(constraints) => set({ position: { ...form.position, constraints } })}
                      keyPlaceholder="max_weight"
                      valuePlaceholder="0.1"
                    />
                  </div>
                </div>
              </section>

              {/* ── risk ──────────────────────────────────────────────── */}
              <section className="space-y-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  {t('component.strategy_dsl.section_risk')}
                </h3>
                {errBox('risk')}
                {form.risk.map((item, i) => (
                  <div key={i} className="flex gap-2 items-start">
                    <div className="w-56">
                      <select
                        className={errClass(!!err(`risk.${i}.type`))}
                        value={item.type}
                        onChange={(e) => {
                          const risk = [...form.risk]
                          risk[i] = { ...item, type: e.target.value }
                          set({ risk })
                        }}
                      >
                        <option value="">{t('component.strategy_dsl.risk_select')}</option>
                        {POLICY_NAMES.map((p) => <option key={p} value={p}>{p}</option>)}
                      </select>
                      {errBox(`risk.${i}.type`)}
                    </div>
                    <div className="flex-1">
                      <KVEditor
                        value={item.params}
                        onChange={(params) => {
                          const risk = [...form.risk]
                          risk[i] = { ...item, params }
                          set({ risk })
                        }}
                        keyPlaceholder="threshold"
                        valuePlaceholder="0.08"
                      />
                    </div>
                    <button
                      className="text-gray-400 hover:text-red-500 text-sm px-1 mt-2"
                      onClick={() => set({ risk: form.risk.filter((_, j) => j !== i) })}
                    >
                      ✕
                    </button>
                  </div>
                ))}
                <button
                  className="text-xs text-brand-600 hover:underline"
                  onClick={() => set({ risk: [...form.risk, { type: '', params: {} }] })}
                >
                  + {t('component.strategy_dsl.add_risk')}
                </button>
              </section>

              {/* ── regime（可折叠，默认无）───────────────────────────── */}
              <section className="border rounded-lg">
                <button
                  className="w-full flex items-center justify-between p-3 text-sm font-semibold text-gray-700"
                  onClick={() => {
                    if (!form.regime) {
                      set({
                        regime: {
                          mode: 'threshold',
                          indicators: {},
                          initial: null,
                          reevaluate: null,
                          states: null,
                          rules: [{ when: null, position_scale: 1 }],
                          scale_expr: null,
                        },
                      })
                      setRegimeOpen(true)
                    } else {
                      setRegimeOpen(!regimeOpen)
                    }
                  }}
                >
                  <span>{t('component.strategy_dsl.section_regime')}</span>
                  <span className="text-xs font-normal text-gray-400">
                    {form.regime
                      ? t(`component.strategy_dsl.mode_${form.regime.mode}`)
                      : t('component.strategy_dsl.regime_none')}
                  </span>
                </button>
                {form.regime && regimeOpen && (
                  <div className="p-3 pt-0 space-y-3 border-t">
                    {errBox('regime')}
                    <div className="flex gap-4 text-sm">
                      {REGIME_MODES.map((m) => (
                        <label key={m} className="flex items-center gap-1.5 cursor-pointer">
                          <input
                            type="radio"
                            name="dsl-regime-mode"
                            checked={form.regime!.mode === m}
                            onChange={() => {
                              const base = form.regime!
                              set({
                                regime: {
                                  ...base,
                                  mode: m,
                                  rules: m === 'threshold' ? (base.rules ?? [{ when: null, position_scale: 1 }]) : null,
                                  states: m === 'switch' ? (base.states ?? [{ name: '', enter_when: '', position_scale: 1 }]) : null,
                                  scale_expr: m === 'continuous' ? (base.scale_expr ?? '') : null,
                                  initial: m === 'switch' ? (base.initial ?? '') : null,
                                },
                              })
                            }}
                            className="accent-blue-600"
                          />
                          {t(`component.strategy_dsl.mode_${m}`)}
                        </label>
                      ))}
                    </div>

                    {/* threshold：rules */}
                    {form.regime.mode === 'threshold' && (
                      <div className="space-y-2">
                        {errBox('regime.rules')}
                        {form.regime.rules?.map((r, i) => (
                          <div key={i} className="flex gap-2 items-center">
                            <input
                              className={`input flex-1 text-xs ${err(`regime.rules.${i}.when`) ? 'border-red-500' : ''}`}
                              placeholder={
                                r.when === null
                                  ? t('component.strategy_dsl.default_rule')
                                  : 'breadth_20d < 0.6'
                              }
                              value={r.when ?? ''}
                              onChange={(e) => {
                                const rules = [...form.regime!.rules!]
                                rules[i] = { ...r, when: e.target.value === '' ? null : e.target.value }
                                set({ regime: { ...form.regime!, rules } })
                              }}
                            />
                            <FunctionHintButton />
                            <input
                              type="number"
                              step="0.1" min={0} max={1}
                              className={`input w-20 text-xs ${err(`regime.rules.${i}.position_scale`) ? 'border-red-500' : ''}`}
                              value={r.position_scale}
                              onChange={(e) => {
                                const rules = [...form.regime!.rules!]
                                rules[i] = { ...r, position_scale: Number(e.target.value) }
                                set({ regime: { ...form.regime!, rules } })
                              }}
                            />
                            <button
                              className="text-gray-400 hover:text-red-500 text-sm"
                              onClick={() => set({
                                regime: { ...form.regime!, rules: form.regime!.rules!.filter((_, j) => j !== i) },
                              })}
                            >
                              ✕
                            </button>
                          </div>
                        ))}
                        <button
                          className="text-xs text-brand-600 hover:underline"
                          onClick={() => set({
                            regime: {
                              ...form.regime!,
                              rules: [...(form.regime!.rules ?? []), { when: '', position_scale: 0.5 }],
                            },
                          })}
                        >
                          + {t('component.strategy_dsl.add_rule')}
                        </button>
                        <p className="text-xs text-gray-400">
                          {t('component.strategy_dsl.threshold_hint')}
                        </p>
                      </div>
                    )}

                    {/* switch：states */}
                    {form.regime.mode === 'switch' && (
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <label className="text-xs text-gray-600">
                            {t('component.strategy_dsl.label_initial')}
                          </label>
                          <input
                            className={`input w-40 text-xs ${err('regime.initial') ? 'border-red-500' : ''}`}
                            value={form.regime.initial ?? ''}
                            onChange={(e) => set({ regime: { ...form.regime!, initial: e.target.value } })}
                          />
                          {errBox('regime.initial')}
                        </div>
                        {errBox('regime.states')}
                        {form.regime.states?.map((s, i) => (
                          <div key={i} className="flex gap-2 items-center">
                            <input
                              className={`input w-28 text-xs ${err(`regime.states.${i}.name`) ? 'border-red-500' : ''}`}
                              placeholder="risk_off"
                              value={s.name}
                              onChange={(e) => {
                                const states = [...form.regime!.states!]
                                states[i] = { ...s, name: e.target.value }
                                set({ regime: { ...form.regime!, states } })
                              }}
                            />
                            <input
                              className={`input flex-1 text-xs ${err(`regime.states.${i}.enter_when`) ? 'border-red-500' : ''}`}
                              placeholder="drawdown_20d > 0.08"
                              value={s.enter_when}
                              onChange={(e) => {
                                const states = [...form.regime!.states!]
                                states[i] = { ...s, enter_when: e.target.value }
                                set({ regime: { ...form.regime!, states } })
                              }}
                            />
                            <FunctionHintButton />
                            <input
                              type="number" step="0.1" min={0} max={1}
                              className={`input w-20 text-xs ${err(`regime.states.${i}.position_scale`) ? 'border-red-500' : ''}`}
                              value={s.position_scale}
                              onChange={(e) => {
                                const states = [...form.regime!.states!]
                                states[i] = { ...s, position_scale: Number(e.target.value) }
                                set({ regime: { ...form.regime!, states } })
                              }}
                            />
                            <button
                              className="text-gray-400 hover:text-red-500 text-sm"
                              onClick={() => set({
                                regime: { ...form.regime!, states: form.regime!.states!.filter((_, j) => j !== i) },
                              })}
                            >
                              ✕
                            </button>
                          </div>
                        ))}
                        <button
                          className="text-xs text-brand-600 hover:underline"
                          onClick={() => set({
                            regime: {
                              ...form.regime!,
                              states: [...(form.regime!.states ?? []), { name: '', enter_when: '', position_scale: 1 }],
                            },
                          })}
                        >
                          + {t('component.strategy_dsl.add_state')}
                        </button>
                      </div>
                    )}

                    {/* continuous：scale_expr */}
                    {form.regime.mode === 'continuous' && (
                      <div>
                        <div className="flex items-center gap-1">
                          <input
                            className={`input flex-1 text-xs ${err('regime.scale_expr') ? 'border-red-500' : ''}`}
                            placeholder="1 - clip(vol_20d / 0.05, 0, 0.8)"
                            value={form.regime.scale_expr ?? ''}
                            onChange={(e) => set({ regime: { ...form.regime!, scale_expr: e.target.value } })}
                          />
                          <FunctionHintButton />
                        </div>
                        {errBox('regime.scale_expr')}
                      </div>
                    )}

                    {/* 共用：indicators + reevaluate */}
                    <div className="grid grid-cols-2 gap-3 border-t pt-2">
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">
                          {t('component.strategy_dsl.label_indicators')}
                        </label>
                        <KVEditor
                          value={form.regime.indicators}
                          onChange={(indicators) => set({ regime: { ...form.regime!, indicators: indicators as Record<string, string> } })}
                          keyPlaceholder="dd"
                          valuePlaceholder="drawdown_20d"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">
                          {t('component.strategy_dsl.label_reevaluate')}
                        </label>
                        <div className="flex gap-4 text-sm pt-1.5">
                          {(['daily', 'rebalance'] as const).map((r) => (
                            <label key={r} className="flex items-center gap-1.5 cursor-pointer">
                              <input
                                type="radio"
                                name="dsl-reevaluate"
                                checked={form.regime!.reevaluate === r}
                                onChange={() => set({ regime: { ...form.regime!, reevaluate: r } })}
                                className="accent-blue-600"
                              />
                              {t(`component.strategy_dsl.reevaluate_${r}`)}
                            </label>
                          ))}
                        </div>
                      </div>
                    </div>

                    <button
                      className="text-xs text-red-500 hover:underline"
                      onClick={() => { set({ regime: null }); setRegimeOpen(false) }}
                    >
                      {t('component.strategy_dsl.remove_regime')}
                    </button>
                  </div>
                )}
              </section>
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t">
          <button className="btn-secondary" onClick={onClose}>{t('common.cancel')}</button>
          <button
            className="btn-primary"
            disabled={saveMutation.isPending}
            onClick={handleSave}
          >
            {saveMutation.isPending ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}
