/**
 * Backlog #1: fills_persisted=false run tag must surface in the Fills tab
 * (failure banner with the structured fills_error reason) instead of a
 * blank page.
 */

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { backtestsApi } from '@/lib/api'
import { BacktestFillsTab } from '../backtest-tabs/BacktestFillsTab'

vi.mock('@/lib/api', () => ({
  backtestsApi: {
    get: vi.fn(),
    getFills: vi.fn().mockResolvedValue({ items: [], total: 0, offset: 0, limit: 50 }),
  },
}))

const mockedGet = vi.mocked(backtestsApi.get)

function renderFillsTab() {
  return renderWithProviders(
    <Routes>
      <Route path="/backtests/:id/fills" element={<BacktestFillsTab />} />
    </Routes>,
    { route: '/backtests/run-123/fills' },
  )
}

describe('BacktestFillsTab — fills persistence failure surfacing', () => {
  beforeEach(() => {
    mockedGet.mockReset()
  })

  it('shows the failure banner + reason when tags.fills_persisted === false', async () => {
    mockedGet.mockResolvedValue({
      run_id: 'run-123',
      engine: 'vector',
      strategy_id: 's1',
      dataset_version: 'v1',
      started_at: '2026-01-01',
      completed_at: '2026-01-02',
      status: 'completed',
      tags: JSON.stringify({
        fills_persisted: false,
        fills_error: 'RuntimeError: simulated fills write failure',
      }),
    } as never)

    renderFillsTab()

    expect(await screen.findByTestId('fills-persist-failure')).toBeInTheDocument()
    expect(screen.getByText(/成交记录持久化失败/)).toBeInTheDocument()
    expect(screen.getByTestId('fills-persist-error')).toHaveTextContent(
      'RuntimeError: simulated fills write failure',
    )
  })

  it('does not show the banner when fills persisted fine (no tag)', async () => {
    mockedGet.mockResolvedValue({
      run_id: 'run-123',
      engine: 'vector',
      strategy_id: 's1',
      dataset_version: 'v1',
      started_at: '2026-01-01',
      completed_at: '2026-01-02',
      status: 'completed',
      tags: null,
    } as never)

    renderFillsTab()

    await waitFor(() => expect(mockedGet).toHaveBeenCalled())
    // give the query a tick to settle, then assert absence
    await waitFor(() => {
      expect(screen.queryByTestId('fills-persist-failure')).not.toBeInTheDocument()
    })
    expect(screen.queryByTestId('fills-persist-error')).not.toBeInTheDocument()
  })

  it('does not show the banner for malformed (non-dict) tags', async () => {
    mockedGet.mockResolvedValue({
      run_id: 'run-123',
      engine: 'vector',
      strategy_id: 's1',
      dataset_version: 'v1',
      started_at: '2026-01-01',
      completed_at: '2026-01-02',
      status: 'completed',
      tags: '["legacy", "tags"]',
    } as never)

    renderFillsTab()

    await waitFor(() => {
      expect(screen.queryByTestId('fills-persist-failure')).not.toBeInTheDocument()
    })
  })
})
