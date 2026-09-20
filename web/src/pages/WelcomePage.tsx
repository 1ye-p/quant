import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { demoApi, backtestsApi, type DemoSeedResult } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'

const ONBOARDED_KEY = 'cquant_onboarded'

export function isOnboarded(): boolean {
  try {
    return localStorage.getItem(ONBOARDED_KEY) === '1'
  } catch {
    return false
  }
}

export function markOnboarded(): void {
  try {
    localStorage.setItem(ONBOARDED_KEY, '1')
  } catch {
    // localStorage unavailable — onboarding simply re-shows next visit
  }
}

type StepStatus = 'pending' | 'running' | 'done' | 'error'

interface StepState {
  status: StepStatus
  error?: string
}

const STATUS_ICON: Record<StepStatus, string> = {
  pending: '○',
  running: '◐',
  done: '●',
  error: '✕',
}

export function WelcomePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [seed, setSeed] = useState<StepState>({ status: 'pending' })
  const [backtest, setBacktest] = useState<StepState>({ status: 'pending' })
  const [seedResult, setSeedResult] = useState<DemoSeedResult | null>(null)
  const [runId, setRunId] = useState<string | null>(null)

  // If the demo dataset was already seeded in a previous session, reflect it
  const { data: demoStatus } = useQuery({
    queryKey: ['demo-status'],
    queryFn: () => demoApi.status(),
  })

  useEffect(() => {
    if (demoStatus?.seeded && seed.status === 'pending') {
      setSeed({ status: 'done' })
    }
  }, [demoStatus, seed.status])

  // Poll the backtest job until it finishes
  const { data: jobStatus } = useQuery({
    queryKey: ['demo-backtest-job', runId],
    queryFn: () => backtestsApi.pollJob(runId!),
    enabled: !!runId && backtest.status === 'running',
    refetchInterval: (query: any) =>
      query.state.data?.status === 'running' ? 2000 : false,
  })

  useEffect(() => {
    if (!jobStatus || backtest.status !== 'running') return
    if (jobStatus.status === 'completed' && jobStatus.run_id) {
      setBacktest({ status: 'done' })
      markOnboarded()
      queryClient.invalidateQueries({ queryKey: queryKeys.backtests.all })
    } else if (jobStatus.status === 'failed') {
      setBacktest({ status: 'error', error: jobStatus.error ?? 'backtest failed' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobStatus])

  const seedMutation = useMutation({
    mutationFn: () => demoApi.seed(),
    onSuccess: (data) => {
      setSeedResult(data)
      setSeed({ status: 'done' })
    },
    onError: (err: Error) => setSeed({ status: 'error', error: err.message }),
  })

  const backtestMutation = useMutation({
    mutationFn: () =>
      backtestsApi.create({
        strategy_id: 'demo_momentum_top10',
        dataset_version: seedResult?.dataset_version ?? 'demo_synthetic_v1',
        start_date: seedResult?.suggested_start_date ?? '2024-03-01',
        end_date: seedResult?.suggested_end_date ?? '2025-12-31',
        strategy_type: 'DSL',
        top_n: 10,
        feature_set_version: seedResult?.feature_set_version || undefined,
      }),
    onSuccess: (data) => {
      setBacktest({ status: 'running' })
      setRunId(data.job_id)
    },
    onError: (err: Error) => setBacktest({ status: 'error', error: err.message }),
  })

  const allDone = seed.status === 'done' && backtest.status === 'done'

  const renderStep = (
    index: number,
    state: StepState,
    title: string,
    desc: string,
    actionLabel: string,
    onAction: (() => void) | null,
    doneExtra?: React.ReactNode,
  ) => (
    <div className="card flex items-start gap-4">
      <div className="flex flex-col items-center gap-1 pt-1">
        <span
          className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-bold ${
            state.status === 'done'
              ? 'bg-green-100 text-green-700'
              : state.status === 'error'
                ? 'bg-red-100 text-red-700'
                : state.status === 'running'
                  ? 'bg-blue-100 text-blue-700 animate-pulse'
                  : 'bg-gray-100 text-gray-500'
          }`}
        >
          {state.status === 'error' ? STATUS_ICON.error : state.status === 'done' ? '✓' : index}
        </span>
      </div>
      <div className="flex-1">
        <h3 className="font-semibold text-gray-900">{title}</h3>
        <p className="text-sm text-gray-500 mt-0.5">{desc}</p>
        {state.error && <p className="text-xs text-red-500 mt-1">{state.error}</p>}
        {state.status === 'done' && doneExtra}
        <div className="mt-3">
          {state.status === 'pending' && onAction && (
            <button className="btn-primary" onClick={onAction}>
              {actionLabel}
            </button>
          )}
          {state.status === 'running' && (
            <span className="text-sm text-blue-600">{t('page.welcome.running')}</span>
          )}
        </div>
      </div>
    </div>
  )

  return (
    <div className="max-w-3xl mx-auto">
      <h1 className="page-title">{t('page.welcome.title')}</h1>
      <p className="text-sm text-gray-500 mt-1 mb-6">{t('page.welcome.subtitle')}</p>

      <div className="flex flex-col gap-4">
        {renderStep(
          1,
          seedMutation.isPending ? { status: 'running' } : seed,
          t('page.welcome.step1.title'),
          t('page.welcome.step1.desc'),
          t('page.welcome.step1.action'),
          seed.status === 'done' ? null : () => seedMutation.mutate(),
          seedResult && (
            <p className="text-xs text-gray-400 mt-1">
              {t('page.welcome.step1.result', {
                assets: seedResult.prices.assets,
                rows: seedResult.prices.rows,
              })}
            </p>
          ),
        )}

        {renderStep(
          2,
          backtestMutation.isPending ? { status: 'running' } : backtest,
          t('page.welcome.step2.title'),
          t('page.welcome.step2.desc'),
          t('page.welcome.step2.action'),
          seed.status === 'done' && backtest.status !== 'done'
            ? () => backtestMutation.mutate()
            : null,
        )}

        {renderStep(
          3,
          allDone ? { status: 'done' } : { status: 'pending' },
          t('page.welcome.step3.title'),
          t('page.welcome.step3.desc'),
          '',
          null,
          jobStatus?.run_id && (
            <button
              className="btn-secondary mt-2"
              onClick={() => navigate(`/backtests/${jobStatus.run_id}`)}
            >
              {t('page.welcome.step3.action')} →
            </button>
          ),
        )}
      </div>

      {allDone && (
        <div className="card mt-6 border-l-4 border-green-400">
          <p className="text-sm text-gray-700">{t('page.welcome.done')}</p>
          <button className="btn-secondary mt-3" onClick={() => navigate('/')}>
            {t('page.welcome.backHome')}
          </button>
        </div>
      )}
    </div>
  )
}
