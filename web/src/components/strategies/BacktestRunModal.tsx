import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { backtestsApi, datasetsApi, mlApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'

interface BacktestRunModalProps {
  strategyId: string
  configText: string
  onClose: () => void
}

export function BacktestRunModal({ strategyId, configText, onClose }: BacktestRunModalProps) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const parsed = useMemo(() => {
    try { return JSON.parse(configText) } catch { return {} }
  }, [configText])

  const factors: string[] = parsed.factors ?? ['ret_20d']
  const defaultTopN = parsed.top_n ?? 10

  const [startDate, setStartDate] = useState('2025-01-01')
  const [endDate, setEndDate] = useState('2025-06-30')
  const [topN, setTopN] = useState(String(defaultTopN))
  const [sortFactor, setSortFactor] = useState(factors[0])
  const [datasetVersion, setDatasetVersion] = useState('')
  const [universeId, setUniverseId] = useState(parsed.universe_id ?? 'all')
  const [customAssets, setCustomAssets] = useState('')
  // UI 预选沪深300（服务端不做静默默认，用户可显式切回"无基准"）
  const [benchmarkId, setBenchmarkId] = useState('SSE:000300')
  const [scoringRunId, setScoringRunId] = useState('')
  const [scoringWarning, setScoringWarning] = useState('')
  const [mlModelVersion, setMlModelVersion] = useState(
    (parsed as Record<string, unknown>).model_version as string
    ?? (parsed as Record<string, unknown>).model_id as string
    ?? ''
  )
  const [mlLabelName, setMlLabelName] = useState(
    (parsed as Record<string, unknown>).label_name as string ?? 'ret_5d'
  )
  const [mlTrainMode, setMlTrainMode] = useState<'existing' | 'new'>(
    (parsed as Record<string, unknown>).ml_config
      ? ((parsed as Record<string, unknown>).ml_config as Record<string, unknown>).train_mode as 'existing' | 'new' ?? 'existing'
      : 'existing'
  )
  const [mlModelType, setMlModelType] = useState(
    ((parsed as Record<string, unknown>).ml_config as Record<string, unknown>)?.model_type as string ?? 'lgbm'
  )
  const [mlNfolds, setMlNfolds] = useState(
    ((parsed as Record<string, unknown>).ml_config as Record<string, unknown>)?.n_splits as number ?? 3
  )
  const [mlGapDays, setMlGapDays] = useState(
    ((parsed as Record<string, unknown>).ml_config as Record<string, unknown>)?.gap_days as number ?? 5
  )

  const [splitMode, setSplitMode] = useState<'none' | 'oos' | 'walkforward'>('none')
  const [trainEndDate, setTrainEndDate] = useState('2024-01-01')
  const [evalMode, setEvalMode] = useState<'test' | 'valid' | 'all'>('test')

  const [wfSplits, setWfSplits] = useState(3)
  const [wfGapDays, setWfGapDays] = useState(5)
  const [wfWindowType, setWfWindowType] = useState<'expanding' | 'sliding'>('expanding')
  const [wfTrainRatio, setWfTrainRatio] = useState(70)
  const [wfValidRatio, setWfValidRatio] = useState(15)

  const { data: datasets } = useQuery({
    queryKey: queryKeys.datasets.list(10),
    queryFn: () => datasetsApi.list(10),
  })

  const { data: universes } = useQuery({
    queryKey: ['datasets', 'universes'],
    queryFn: datasetsApi.universes,
    staleTime: 300_000,
  })

  const { data: mlExperiments } = useQuery({
    queryKey: ['ml', 'experiments', 'completed'],
    queryFn: () => mlApi.experiments(100),
    enabled: parsed.strategy_type === 'MLModelStrategy',
    staleTime: 30_000,
    select: (data) => data.items?.filter(
      (e: { status: string }) => e.status === 'done'
    ) ?? [],
  })

  const { data: modelsCatalog } = useQuery({
    queryKey: ['ml', 'models', 'catalog'],
    queryFn: mlApi.modelsCatalog,
    enabled: parsed.strategy_type === 'MLModelStrategy',
    staleTime: 300_000,
  })

  // Load pending scoring run from sessionStorage
  useEffect(() => {
    const pending = sessionStorage.getItem('pendingScoring')
    if (pending) {
      try {
        const { runId, start, end } = JSON.parse(pending)
        setScoringRunId(runId ?? '')
        if (start) setStartDate(start)
        if (end) setEndDate(end)
      } catch { /* ignore */ }
      sessionStorage.removeItem('pendingScoring')
    }
  }, [])

  // Auto-select current dataset
  useEffect(() => {
    if (datasets?.items.length && !datasetVersion) {
      const current = datasets.items.find(d => d.is_current) ?? datasets.items[0]
      setDatasetVersion(current.version_id)
    }
  }, [datasets, datasetVersion])

  const [jobId, setJobId] = useState<string | null>(null)

  const { data: jobStatus } = useQuery({
    queryKey: ['backtest-job', jobId],
    queryFn: () => backtestsApi.pollJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query: any) =>
      query.state.data?.status === 'running' ? 2000 : false,
  })

  const runMutation = useMutation({
    mutationFn: (body: Parameters<typeof backtestsApi.create>[0]) => backtestsApi.create(body),
    onSuccess: (data) => {
      if (data.warning) {
        setScoringWarning(data.warning as string)
      }
      queryClient.invalidateQueries({ queryKey: queryKeys.backtests.all })
      if (data.job_id) {
        setJobId(data.job_id)
      } else {
        queryClient.refetchQueries({ queryKey: queryKeys.backtests.all }).then(() => {
          onClose()
          navigate('/backtests')
        })
      }
    },
    onError: (error: Error) => {
      toast.error(t('component.backtest_run_modal.status.submit_failed_prefix') + error.message)
    },
  })

  // Job completion handler
  useEffect(() => {
    if (jobStatus?.status === 'completed') {
      queryClient.refetchQueries({ queryKey: queryKeys.backtests.all }).then(() => {
        onClose()
        navigate(`/backtests?run_id=${jobStatus.run_id}`)
      })
    } else if (jobStatus?.status === 'failed') {
      toast.error(t('component.backtest_run_modal.status.failed_prefix') + (jobStatus.error ?? t('component.backtest_run_modal.status.unknown_error')))
      queryClient.invalidateQueries({ queryKey: queryKeys.backtests.all })
    }
  }, [jobStatus, queryClient, onClose, navigate])

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-[500px] max-w-full max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b flex-shrink-0">
          <h2 className="font-semibold text-gray-900">{t('component.backtest_run_modal.title')}</h2>
          <button className="text-gray-400 hover:text-gray-600" onClick={onClose}>✕</button>
        </div>
        <div className="p-4 space-y-4 overflow-y-auto flex-1">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.strategy')}</label>
            <div className="px-3 py-2 bg-gray-50 border rounded-lg text-sm">{strategyId}</div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t('common.start_date')}</label>
              <input type="date" className="input w-full" value={startDate} onChange={e => setStartDate(e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t('common.end_date')}</label>
              <input type="date" className="input w-full" value={endDate} onChange={e => setEndDate(e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.param.top_n')}</label>
              <input type="number" className="input w-full" value={topN} onChange={e => setTopN(e.target.value)} min={1} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.sort_factor')}</label>
              <select className="input w-full" value={sortFactor} onChange={e => setSortFactor(e.target.value)}>
                {factors.map(f => <option key={f} value={f}>{f}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.dataset')}</label>
            <select className="input w-full" value={datasetVersion} onChange={e => setDatasetVersion(e.target.value)}>
              {datasets?.items.map(d => (
                <option key={d.version_id} value={d.version_id}>
                  {d.dataset_name} ({d.start_date} ~ {d.end_date}) — {d.asset_count ?? '?'}{t('component.backtest_run_modal.unit.stocks_suffix')}
                  {d.is_current ? ' ✓' : ''}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('component.strategies.params.stock_pool')}</label>
            <select
              className="input w-full"
              value={universeId}
              onChange={e => {
                setUniverseId(e.target.value)
                if (e.target.value !== 'custom') setCustomAssets('')
              }}
            >
              {universes?.predefined.map(u => (
                <option key={u.id} value={u.id}>{u.name} — {u.description}</option>
              ))}
              <option value="custom">{t('component.backtest_run_modal.option.custom_pool')}</option>
            </select>
          </div>
          {universeId === 'custom' && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.custom_assets')}</label>
              <input
                type="text"
                className="input w-full"
                value={customAssets}
                onChange={e => setCustomAssets(e.target.value)}
                placeholder="SSE:600036,SZSE:000001,SZSE:300750"
              />
            </div>
          )}
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.benchmark')}</label>
            <select value={benchmarkId} onChange={e => setBenchmarkId(e.target.value)} className="input w-full">
              <option value="">{t('component.backtest_run_modal.option.no_benchmark')}</option>
              <option value="SSE:000300">{t('component.backtest_run_modal.option.benchmark_csi300')}</option>
              <option value="SSE:000905">{t('component.backtest_run_modal.option.benchmark_csi500')}</option>
              <option value="SSE:000852">{t('component.backtest_run_modal.option.benchmark_csi1000')}</option>
              <option value="SSE:000001">{t('component.backtest_run_modal.option.benchmark_sse')}</option>
              <option value="SZSE:399001">{t('component.backtest_run_modal.option.benchmark_szse')}</option>
            </select>
            {!benchmarkId && (
              <div className="text-xs text-amber-600 mt-1">
                ⚠ {t('component.backtest_run_modal.hint.no_benchmark')}
              </div>
            )}
          </div>

          {/* ML Model Config */}
          {parsed.strategy_type === 'MLModelStrategy' && (
            <div className="border rounded-lg p-3 bg-blue-50 space-y-3">
              <h4 className="text-sm font-medium text-blue-800">{t('component.backtest_run_modal.label.ml_config')}</h4>
              <div>
                <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.label.train_mode')}</label>
                <div className="flex gap-4 text-sm">
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name="ml-train-mode"
                      checked={mlTrainMode === 'existing'}
                      onChange={() => setMlTrainMode('existing')}
                      className="accent-blue-600"
                    />
                    <span>{t('component.backtest_run_modal.option.use_existing_model')}</span>
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name="ml-train-mode"
                      checked={mlTrainMode === 'new'}
                      onChange={() => setMlTrainMode('new')}
                      className="accent-blue-600"
                    />
                    <span>{t('component.backtest_run_modal.option.new_train_and_backtest')}</span>
                  </label>
                </div>
              </div>

              {mlTrainMode === 'existing' && (
                <div>
                  <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.label.select_trained_model')}</label>
                  <select
                    value={mlModelVersion}
                    onChange={e => {
                      setMlModelVersion(e.target.value)
                      const exp = mlExperiments?.find(
                        (ex: { run_id: string; model_id?: string }) =>
                          ex.run_id === e.target.value || ex.model_id === e.target.value
                      )
                      if (exp?.target_name) setMlLabelName(exp.target_name)
                    }}
                    className="input w-full text-sm"
                  >
                    <option value="">{t('component.backtest_run_modal.option.select_completed_experiment')}</option>
                    {mlExperiments?.map((exp: {
                      run_id: string
                      model_id?: string
                      trainer_name?: string
                      target_name?: string
                      metrics?: { sharpe?: number }
                      started_at?: string | number
                    }) => (
                      <option key={exp.run_id} value={exp.model_id ?? exp.run_id}>
                        {exp.run_id.slice(0, 10)}... · {exp.trainer_name ?? '—'} · target={exp.target_name ?? '—'}
                        {exp.metrics?.sharpe != null ? ` · Sharpe=${exp.metrics.sharpe.toFixed(2)}` : ''}
                      </option>
                    ))}
                  </select>
                  {mlExperiments !== undefined && mlExperiments.length === 0 && (
                    <p className="text-xs text-gray-400 mt-1">
                      {t('component.backtest_run_modal.hint.no_completed_experiments')}
                    </p>
                  )}
                </div>
              )}

              {mlTrainMode === 'new' && (
                <div className="space-y-3">
                  <div>
                    <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.label.model_type')}</label>
                    <select
                      value={mlModelType}
                      onChange={e => setMlModelType(e.target.value)}
                      className="input w-full text-sm"
                    >
                      {modelsCatalog
                        ? Object.entries(modelsCatalog).map(([key, info]) => (
                            <option key={key} value={key}>
                              {info.display_name} ({info.model_type})
                            </option>
                          ))
                        : <>
                            <option value="lgbm">LightGBM</option>
                            <option value="xgb">XGBoost</option>
                          </>
                      }
                    </select>
                    {modelsCatalog && modelsCatalog[mlModelType]?.description && (
                      <p className="text-xs text-gray-400 mt-1">
                        {modelsCatalog[mlModelType].description}
                      </p>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.param.wf_folds')}</label>
                      <input
                        type="number"
                        className="input w-full text-sm"
                        value={mlNfolds}
                        onChange={e => setMlNfolds(Number(e.target.value))}
                        min={2}
                        max={10}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.label.purge_gap')}</label>
                      <input
                        type="number"
                        className="input w-full text-sm"
                        value={mlGapDays}
                        onChange={e => setMlGapDays(Number(e.target.value))}
                        min={0}
                        max={30}
                      />
                    </div>
                  </div>
                  <p className="text-xs text-blue-600 bg-blue-100 rounded px-2 py-1">
                    {t('component.backtest_run_modal.hint.auto_train_walkforward', { folds: mlNfolds })}
                  </p>
                </div>
              )}

              <div>
                <label className="block text-xs text-gray-600 mb-1">{t('component.backtest_run_modal.label.prediction_label')}</label>
                <select
                  value={mlLabelName}
                  onChange={e => setMlLabelName(e.target.value)}
                  className="input w-full text-sm"
                >
                  <option value="ret_1d">{t('component.backtest_run_modal.label_option.ret_1d')}</option>
                  <option value="ret_5d">{t('component.backtest_run_modal.label_option.ret_5d')}</option>
                  <option value="ret_10d">{t('component.backtest_run_modal.label_option.ret_10d')}</option>
                  <option value="ret_20d">{t('component.backtest_run_modal.label_option.ret_20d')}</option>
                </select>
              </div>
            </div>
          )}

          {scoringRunId && (
            <div className="p-2 bg-purple-50 border border-purple-200 rounded text-xs text-purple-700">
              {t('component.backtest_run_modal.hint.scoring_using')}<span className="font-mono">{scoringRunId.slice(0, 12)}...</span>
              <br />{t('component.backtest_run_modal.hint.scoring_locked_range')}
            </div>
          )}

          {/* Data Split Section */}
          <div className="border-t pt-4">
            <label className="block text-sm text-gray-600 mb-2">{t('component.backtest_run_modal.label.data_split')}</label>
            <select
              className="input w-full"
              value={splitMode}
              onChange={e => setSplitMode(e.target.value as typeof splitMode)}
            >
              <option value="none">{t('component.backtest_run_modal.option.split_none')}</option>
              <option value="oos">{t('component.backtest_run_modal.option.split_oos')}</option>
              <option value="walkforward">{t('component.backtest_run_modal.option.split_walkforward')}</option>
            </select>
          </div>

          {splitMode === 'oos' && (
            <div className="grid grid-cols-2 gap-4 pl-4 border-l-2 border-blue-200">
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.train_end_date')}</label>
                <input type="date" className="input w-full" value={trainEndDate}
                  onChange={e => setTrainEndDate(e.target.value)} />
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.eval_mode')}</label>
                <select className="input w-full" value={evalMode}
                  onChange={e => setEvalMode(e.target.value as typeof evalMode)}>
                  <option value="test">{t('component.backtest_run_modal.option.eval_test')}</option>
                  <option value="valid">{t('component.backtest_run_modal.option.eval_valid')}</option>
                  <option value="all">{t('component.backtest_run_modal.option.eval_all')}</option>
                </select>
              </div>
            </div>
          )}

          {splitMode === 'walkforward' && (
            <div className="pl-4 border-l-2 border-green-200 space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.split_count')}</label>
                  <input type="number" className="input w-full" value={wfSplits}
                    onChange={e => setWfSplits(Number(e.target.value))} min={2} max={10} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.gap_days')}</label>
                  <input type="number" className="input w-full" value={wfGapDays}
                    onChange={e => setWfGapDays(Number(e.target.value))} min={0} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.label.window_type')}</label>
                  <select className="input w-full" value={wfWindowType}
                    onChange={e => setWfWindowType(e.target.value as typeof wfWindowType)}>
                    <option value="expanding">{t('component.backtest_run_modal.option.window_expanding')}</option>
                    <option value="sliding">{t('component.backtest_run_modal.option.window_sliding')}</option>
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.param.train_pct')}</label>
                  <input type="number" className="input w-full" value={wfTrainRatio}
                    onChange={e => setWfTrainRatio(Number(e.target.value))} min={10} max={90} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.param.valid_pct')}</label>
                  <input type="number" className="input w-full" value={wfValidRatio}
                    onChange={e => setWfValidRatio(Number(e.target.value))} min={5} max={30} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t('component.backtest_run_modal.param.test_pct')}</label>
                  <input type="number" className="input w-full" disabled
                    value={100 - wfTrainRatio - wfValidRatio} />
                </div>
              </div>
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 p-4 border-t flex-wrap flex-shrink-0">
          {scoringWarning && (
            <div className="w-full text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
              ⚠ {scoringWarning}
            </div>
          )}
          {jobId && jobStatus?.status === 'running' && (
            <div className="flex items-center gap-2 text-sm text-blue-600">
              <div className="animate-spin w-4 h-4 border-2 border-blue-600 border-t-transparent rounded-full" />
              {t('common.running')}
            </div>
          )}
          {jobId && jobStatus?.status === 'failed' && (
            <div className="text-sm text-red-600">
              {t('component.backtest_run_modal.status.failed_prefix')}{jobStatus.error ?? t('component.backtest_run_modal.status.unknown_error')}
            </div>
          )}
          <button className="btn-secondary" onClick={onClose}>
            {jobId ? t('component.backtest_run_modal.btn.close') : t('common.cancel')}
          </button>
          {!jobId && (
            <button
              className="btn-primary"
              disabled={runMutation.isPending || !datasetVersion}
              onClick={() => {
                const body: Record<string, unknown> = {
                  strategy_id: strategyId,
                  dataset_version: datasetVersion,
                  start_date: startDate,
                  end_date: endDate,
                  top_n: Number(topN) || 10,
                  sort_factor: sortFactor,
                  strategy_type: parsed.strategy_type ?? 'StaticTopN',
                  universe_id: universeId === 'custom' ? 'all' : universeId,
                  benchmark_asset_id: benchmarkId || "",
                  ...(scoringRunId ? { scoring_run_id: scoringRunId } : {}),
                }
                if (parsed.strategy_type === 'MarketNeutral') {
                  body.short_n = parsed.short_n ?? 10
                }
                if (parsed.strategy_type === 'SectorRotation') {
                  body.sector_map = parsed.sector_map ?? {}
                  body.top_sectors = parsed.top_sectors ?? 3
                  body.top_n_per_sector = parsed.top_n_per_sector ?? 3
                }
                if (parsed.strategy_type === 'Combo') {
                  body.sub_strategy_configs = parsed.sub_strategy_configs ?? []
                  body.combo_method = parsed.combo_method ?? 'equal_weight'
                }
                if (parsed.strategy_type === 'MLModelStrategy') {
                  if (mlTrainMode === 'existing') {
                    body.model_version = mlModelVersion
                      || (parsed as Record<string, unknown>).model_version
                      || (parsed as Record<string, unknown>).model_id
                      || ''
                  }
                  body.label_name = mlLabelName
                    || (parsed as Record<string, unknown>).label_name
                    || 'ret_5d'
                  body.ml_config = {
                    train_mode: mlTrainMode,
                    ...(mlTrainMode === 'new' ? {
                      model_type: mlModelType,
                      n_splits: mlNfolds,
                      gap_days: mlGapDays,
                    } : {}),
                  }
                }
                if (parsed.strategy_type === 'CustomWeightStrategy') {
                  body.custom_weights = (parsed as Record<string, unknown>).custom_weights ?? {}
                }
                if (parsed.strategy_type === 'BreakoutPullback') {
                  body.breakout_config = (parsed as Record<string, unknown>).breakout_config ?? {}
                }
                // Forward missing factor handling config
                const mfh = (parsed as Record<string, unknown>).missing_factor_handling
                if (mfh && mfh !== 'fill_0') {
                  body.missing_factor_strategy = mfh
                  if (mfh === 'risk_penalty') {
                    body.penalty_per_missing = (parsed as Record<string, unknown>).penalty_per_missing ?? 0.5
                  }
                }
                if (splitMode === 'oos') {
                  body.train_end_date = trainEndDate
                  body.eval_mode = evalMode
                }
                if (splitMode === 'walkforward') {
                  body.walk_forward = {
                    n_splits: wfSplits,
                    gap_days: wfGapDays,
                    window_type: wfWindowType,
                    purge_window: 0,
                  }
                }
                runMutation.mutate(body as any)
              }}
            >
              {runMutation.isPending ? t('component.backtest_run_modal.btn.running') : t('component.backtest_run_modal.btn.run')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
