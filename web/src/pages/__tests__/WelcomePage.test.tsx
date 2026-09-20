import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { WelcomePage } from '../WelcomePage'

vi.mock('@/lib/api', () => ({
  demoApi: {
    seed: vi.fn().mockResolvedValue({
      seeded: true,
      dataset_version: 'demo_synthetic_v1',
      strategy_id: 'demo_momentum_top10',
      prices: { rows: 24000, assets: 50, start_date: '2024-01-02', end_date: '2025-12-31' },
      assets_registered: 50,
      indicator_rows: 479,
      feature_set_version: 'fsv_1',
      suggested_start_date: '2024-02-15',
      suggested_end_date: '2025-12-31',
    }),
    status: vi.fn().mockResolvedValue({ seeded: false, price_rows: 0, strategy_id: null, dataset_version: null }),
  },
  backtestsApi: {
    create: vi.fn().mockResolvedValue({ job_id: 'job_1', strategy_id: 'demo_momentum_top10', status: 'running' }),
    pollJob: vi.fn().mockResolvedValue({ job_id: 'job_1', status: 'completed', run_id: 'run_1', error: null }),
  },
}))

describe('WelcomePage', () => {
  beforeEach(() => {
    // jsdom in this setup exposes a non-functional localStorage — stub it
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
      clear: () => void store.clear(),
    })
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the 3-step guide', async () => {
    renderWithProviders(<WelcomePage />)
    // Step 1 title + action button share the same label — assert presence
    expect((await screen.findAllByText(/导入示例数据/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/运行示例回测/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/查看结果/).length).toBeGreaterThan(0)
  })

  it('marks onboarding complete after seed + backtest finish', async () => {
    renderWithProviders(<WelcomePage />)
    fireEvent.click(await screen.findByText('导入示例数据'))
    await waitFor(() => expect(screen.getByText(/已导入 50 只资产/)).toBeInTheDocument())
    fireEvent.click(screen.getByText('开始回测'))
    await waitFor(() => expect(screen.getByText(/引导完成/)).toBeInTheDocument())
    expect(localStorage.getItem('cquant_onboarded')).toBe('1')
  })
})
