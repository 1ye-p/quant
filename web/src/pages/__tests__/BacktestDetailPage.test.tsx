import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import { BacktestDetailPage } from '../BacktestDetailPage'

vi.mock('@/lib/api', () => ({
  backtestsApi: {
    get: vi.fn(),
  },
}))

// Detail-level cards kick off extra queries / need completed-run data — stub them.
vi.mock('@/components/backtests/ValidationPanel', () => ({
  ValidationBadge: () => null,
}))
vi.mock('@/components/backtests/DecisionSummaryCard', () => ({
  DecisionSummaryCard: () => null,
}))

const { backtestsApi } = await import('@/lib/api')

function renderDetail(id: string) {
  // The tab bar renders regardless of the child route; render inside Routes so
  // the page's relative NavLinks resolve.
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/*" element={<BacktestDetailPage />} />
    </Routes>,
    { route: `/backtests/${id}` },
  )
}

const ML_TAB_LABELS = ['模型对比', '特征重要性', '模型诊断']
const BASE_TAB_LABEL = '总览'

describe('BacktestDetailPage ML tab conditional rendering', () => {
  it('hides ML tabs for a non-ML run', async () => {
    vi.mocked(backtestsApi.get).mockResolvedValue({
      run_id: 'run_1',
      strategy_id: 'dsl_top10',
      strategy_type: 'DSL',
      status: 'completed',
      engine: 'vector',
      dataset_version: 'demo_synthetic_v1',
      started_at: '2025-01-02T09:30:00Z',
      completed_at: '2025-01-02T09:31:00Z',
      metrics: undefined,
    })
    renderDetail('run_1')

    // Base tabs render...
    expect(await screen.findByRole('link', { name: BASE_TAB_LABEL })).toBeInTheDocument()
    // ...but none of the three ML tabs appear.
    for (const label of ML_TAB_LABELS) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
    }
  })

  it('shows the three ML tabs for an MLModelStrategy run', async () => {
    vi.mocked(backtestsApi.get).mockResolvedValue({
      run_id: 'run_2',
      strategy_id: 'ml_lgbm_v1',
      strategy_type: 'MLModelStrategy',
      status: 'completed',
      engine: 'vector',
      dataset_version: 'demo_synthetic_v1',
      started_at: '2025-01-02T09:30:00Z',
      completed_at: '2025-01-02T09:31:00Z',
      metrics: undefined,
    })
    renderDetail('run_2')

    for (const label of ML_TAB_LABELS) {
      expect(await screen.findByRole('link', { name: label })).toBeInTheDocument()
    }
  })
})
