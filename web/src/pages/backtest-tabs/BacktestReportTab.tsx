/**
 * AI Research Report tab (Phase 4 T6):
 * - "Generate" button → POST /{run_id}/report → job polling → refetch GET
 * - Markdown rendered as preformatted text (no react-markdown dependency in
 *   this project; charts embedded as chart-spec text are shown verbatim —
 *   inline chart rendering is out of scope for this batch)
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import i18next from 'i18next'
import { toast } from 'sonner'
import { backtestsApi } from '@/lib/api'
import { queryKeys } from '@/lib/queryKeys'

export function BacktestReportTab() {
  const { t } = useTranslation()
  const { id: runId } = useParams<{ id: string }>()
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const reportQuery = useQuery({
    queryKey: queryKeys.backtests.report(runId ?? ''),
    queryFn: () => backtestsApi.getReport(runId!).catch(() => null),
    enabled: !!runId,
    staleTime: 60_000,
    retry: false,
  })

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
    setJobId(null)
  }, [])

  useEffect(() => {
    if (!jobId) return
    timerRef.current = setInterval(async () => {
      try {
        const job = await backtestsApi.pollJob(jobId)
        if (job.status === 'completed') {
          stopPolling()
          qc.invalidateQueries({ queryKey: queryKeys.backtests.report(runId!) })
          toast.success(i18next.t('page.backtest.report.done'))
        } else if (job.status === 'failed') {
          stopPolling()
          toast.error(job.error ?? i18next.t('page.backtest.report.failed'))
        }
      } catch {
        stopPolling()
        toast.error(i18next.t('page.backtest.report.failed'))
      }
    }, 2000)
    return stopPolling
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, qc, runId])

  const generateMutation = useMutation({
    mutationFn: () => backtestsApi.generateReport(runId!),
    onSuccess: (res) => setJobId(res.job_id),
    onError: (e: Error) => toast.error(e.message),
  })

  if (!runId) return null

  const report = reportQuery.data
  const isRunning = jobId !== null || generateMutation.isPending

  return (
    <div className="space-y-4">
      <div className="card p-4 flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h3 className="font-semibold text-gray-800">
            {t('page.backtest.report.title')}
          </h3>
          <p className="text-xs text-gray-400 mt-1">
            {t('page.backtest.report.subtitle')}
          </p>
        </div>
        <button
          className="btn btn-primary"
          disabled={isRunning}
          onClick={() => generateMutation.mutate()}
        >
          {isRunning
            ? t('page.backtest.report.generating')
            : t('page.backtest.report.generate')}
        </button>
      </div>

      {report ? (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-3 pb-2 border-b border-gray-100">
            <span className="text-xs text-gray-400 font-mono">
              {report.report_id.slice(0, 12)}...
            </span>
            <span className="text-xs text-gray-400">
              {t('page.backtest.report.generated_at')}: {report.created_at?.slice(0, 19)}
            </span>
          </div>
          <pre className="whitespace-pre-wrap break-words text-sm text-gray-700 font-sans leading-relaxed">
            {report.content_md}
          </pre>
          <p className="text-xs text-gray-400 mt-3 pt-2 border-t border-gray-100">
            {t('page.backtest.report.chart_spec_note')}
          </p>
        </div>
      ) : (
        !isRunning && (
          <div className="card p-8 text-center text-sm text-gray-400">
            {t('page.backtest.report.empty')}
          </div>
        )
      )}
    </div>
  )
}
