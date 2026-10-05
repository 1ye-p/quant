import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { dashboardApi } from '@/lib/api'
import { OverviewPage } from '../OverviewPage'

vi.mock('@/lib/api', () => ({
  datasetsApi: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
  alertsApi: {
    history: vi.fn().mockResolvedValue({ items: [], unread_count: 0 }),
  },
  backtestsApi: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
  mlApi: {
    listExperiments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
  liveApi: {
    listDeployments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
  realtimeApi: {
    quotes: vi.fn().mockResolvedValue({}),
  },
  knowledgeApi: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
  dashboardApi: {
    bestRecent: vi.fn().mockResolvedValue({ run_id: null }),
    backtestTrend: vi.fn().mockResolvedValue({ items: [] }),
    icTrend: vi.fn().mockResolvedValue({ items: [] }),
    icLeaderboard: vi.fn().mockResolvedValue({ items: [] }),
  },
}))

const icLeaderboardMock = vi.mocked(dashboardApi.icLeaderboard)

function leaderboardItems(versions: (string | null)[]) {
  return {
    items: versions.map((v, i) => ({
      factor_name: `factor_${i}`,
      mean_ic: 0.1 - i * 0.01,
      ir: 1.5 - i * 0.1,
      hit_rate: 0.6,
      algo_version: v,
    })),
  }
}

describe('OverviewPage', () => {
  it('renders page title', () => {
    renderWithProviders(<OverviewPage />)
    expect(screen.getByText(/cQuant 量化研究平台/)).toBeInTheDocument()
  })

  it('shows mixed-caliber banner when IC leaderboard mixes legacy (NULL) and v2_top20 rows', async () => {
    icLeaderboardMock.mockResolvedValue(
      leaderboardItems(['v2_top20', null]) as never,
    )
    renderWithProviders(<OverviewPage />)
    expect(await screen.findByText(/口径混合/)).toBeInTheDocument()
  })

  it('does not show banner when all rows are v2_top20', async () => {
    icLeaderboardMock.mockResolvedValue(
      leaderboardItems(['v2_top20', 'v2_top20']) as never,
    )
    renderWithProviders(<OverviewPage />)
    await screen.findByText('factor_0')
    expect(screen.queryByText(/口径混合/)).not.toBeInTheDocument()
  })

  it('does not show banner when all rows are legacy (all NULL algo_version)', async () => {
    icLeaderboardMock.mockResolvedValue(leaderboardItems([null, null]) as never)
    renderWithProviders(<OverviewPage />)
    await screen.findByText('factor_0')
    expect(screen.queryByText(/口径混合/)).not.toBeInTheDocument()
  })
})
