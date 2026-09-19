import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { strategiesApi } from '@/lib/api'
import { extendedQueryKeys } from '@/lib/queryKeys'
import { toast } from 'sonner'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { StrategyTable } from '@/components/strategies/StrategyTable'
import { StrategyForm } from '@/components/strategies/StrategyForm'
import { StrategyDSLEditor } from '@/components/strategies/StrategyDSLEditor'
import { BacktestRunModal } from '@/components/strategies/BacktestRunModal'
import { OptimizationReportModal } from '@/components/strategies/OptimizationReportModal'

const DEFAULT_CONFIG = JSON.stringify({
  strategy_id: "my_strategy",
  universe: { exchange: ["SSE", "SZSE"], min_liquidity: 1000000 },
  rebalance_frequency: "1d",
  risk_limits: { max_position_pct: 0.10, max_gross_leverage: 1.0 },
  market_rule: { market: "CN", adj_type: "forward" },
  factors: ["ret_20d", "vol_20d"],
  sizer: "equal_weight"
}, null, 2)

export function StrategiesPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const [editingId, setEditingId] = useState<string | 'new' | null>(null)
  const [configText, setConfigText] = useState(DEFAULT_CONFIG)
  const configTextRef = useRef(configText)
  configTextRef.current = configText
  const [backtestStrategyId, setBacktestStrategyId] = useState<string | null>(null)
  const [backtestConfigText, setBacktestConfigText] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const [dslEditing, setDslEditing] = useState<string | 'new' | null>(null)
  const [dslInitialConfig, setDslInitialConfig] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: extendedQueryKeys.strategies.list(),
    queryFn: strategiesApi.list,
  })

  const [reportStrategyId, setReportStrategyId] = useState<string | null>(null)

  // Fetch latest optimization report status per strategy (badge source).
  const strategyIds = data?.items.map((s) => s.strategy_id) ?? []
  const reportQueries = useQueries({
    queries: strategyIds.map((id) => ({
      queryKey: extendedQueryKeys.strategies.optimizationReport(id),
      queryFn: () => strategiesApi.optimizationReport(id),
      retry: false as const,
      staleTime: 60_000,
    })),
  })
  const optimizationStatuses: Record<string, string> = {}
  strategyIds.forEach((id, i) => {
    const q = reportQueries[i]
    if (q?.data) optimizationStatuses[id] = q.data.status
  })

  // Handle navigation state (from other pages)
  useEffect(() => {
    const state = location.state as {
      prefill?: { strategy_id?: string; config?: string }
      openBacktest?: boolean
      scoringRunId?: string
      scoringDateRange?: { start?: string; end?: string }
    } | null
    if (!state) return
    const { prefill, openBacktest, scoringRunId, scoringDateRange } = state
    if (prefill) {
      if (prefill.strategy_id) {
        // Will be handled by StrategyForm
      }
      if (prefill.config) setConfigText(prefill.config)
      setEditingId('new')
    }
    if (openBacktest && prefill?.strategy_id && !prefill.config) {
      setBacktestStrategyId(prefill.strategy_id)
      setBacktestConfigText(prefill.config ?? '')
    }
    if (scoringRunId) {
      sessionStorage.setItem('pendingScoring', JSON.stringify({
        runId: scoringRunId,
        start: scoringDateRange?.start,
        end: scoringDateRange?.end,
      }))
    }
  }, [location.state])

  // Pre-fill config when navigating from MLLabPage with URL params
  useEffect(() => {
    const mlModel = searchParams.get('ml_model')
    const strategyType = searchParams.get('strategy_type')
    if (mlModel && strategyType === 'MLModelStrategy') {
      try {
        const config = JSON.parse(configTextRef.current)
        config.strategy_type = 'MLModelStrategy'
        config.model_id = mlModel
        config.feature_set_version = searchParams.get('feature_set_version') || ''
        config.label_name = searchParams.get('target_name') || 'ret_5d'
        setConfigText(JSON.stringify(config, null, 2))
        setEditingId('new')
      } catch { /* ignore parse errors */ }
    }
  }, [searchParams])

  const deleteMutation = useMutation({
    mutationFn: (id: string) => strategiesApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: extendedQueryKeys.strategies.list() })
      toast.success(t('page.strategies.deleted_toast'))
      setDeleteTarget(null)
    },
    onError: (err: Error) => {
      toast.error(t('page.strategies.delete_failed', { message: err.message }))
      setDeleteTarget(null)
    },
  })

  function openEdit(item: { strategy_id: string; config_text: string }) {
    // DSL 策略（config 含 dsl_spec）路由到 DSL 编辑器
    try {
      const parsed = JSON.parse(item.config_text)
      if (parsed?.strategy_type === 'DSL' && parsed?.dsl_spec) {
        setDslEditing(item.strategy_id)
        setDslInitialConfig(item.config_text)
        return
      }
    } catch { /* 非 JSON 配置走通用编辑器 */ }
    setEditingId(item.strategy_id)
    setConfigText(item.config_text)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="page-title">{t('page.strategies.title')}</h1>
          <p className="page-subtitle">{t('page.strategies.subtitle')}</p>
        </div>
        <div className="flex gap-2">
          <button
            className="btn-secondary"
            onClick={() => {
              setDslEditing('new')
              setDslInitialConfig('')
            }}
          >
            {t('page.strategies.create_dsl_btn')}
          </button>
          <button
            className="btn-primary"
            onClick={() => {
              setEditingId('new')
              setConfigText(DEFAULT_CONFIG)
            }}
          >
            {t('page.strategies.create_btn')}
          </button>
        </div>
      </div>

      <StrategyTable
        strategies={data?.items ?? []}
        isLoading={isLoading}
        optimizationStatuses={optimizationStatuses}
        onEdit={openEdit}
        onBacktest={(s) => {
          setBacktestStrategyId(s.strategy_id)
          setBacktestConfigText(s.config_text)
        }}
        onDelete={(id) => setDeleteTarget(id)}
        onViewReport={(id) => setReportStrategyId(id)}
      />

      {editingId && (
        <StrategyForm
          editingId={editingId}
          initialConfig={configText}
          onClose={() => setEditingId(null)}
        />
      )}

      {dslEditing && (
        <StrategyDSLEditor
          strategyId={dslEditing}
          initialConfig={dslInitialConfig}
          onClose={() => setDslEditing(null)}
          onSaved={(id, configText) => {
            setDslEditing(null)
            // 保存后唤起回测（与内置策略同一入口）
            setBacktestStrategyId(id)
            setBacktestConfigText(configText)
          }}
        />
      )}

      {backtestStrategyId && (
        <BacktestRunModal
          strategyId={backtestStrategyId}
          configText={backtestConfigText}
          onClose={() => setBacktestStrategyId(null)}
        />
      )}

      {reportStrategyId && (
        <OptimizationReportModal
          strategyId={reportStrategyId}
          onClose={() => setReportStrategyId(null)}
        />
      )}

      <ConfirmDialog
        isOpen={deleteTarget !== null}
        title={t('page.strategies.confirm_delete_title')}
        message={t('page.strategies.confirm_delete_message', { name: deleteTarget })}
        confirmLabel={t('common.delete')}
        variant="danger"
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget)
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}
