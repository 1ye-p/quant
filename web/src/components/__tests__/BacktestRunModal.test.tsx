import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { backtestsApi } from '@/lib/api'
import { BacktestRunModal } from '../strategies/BacktestRunModal'

vi.mock('@/lib/api', () => ({
  backtestsApi: {
    create: vi.fn(),
    pollJob: vi.fn().mockResolvedValue({ job_id: 'job_1', status: 'running', run_id: null, error: null }),
  },
  datasetsApi: {
    list: vi.fn().mockResolvedValue({
      items: [
        { version_id: 'ds_v1', dataset_name: 'demo', start_date: '2025-01-01', end_date: '2025-06-30', asset_count: 10, is_current: true },
      ],
    }),
    universes: vi.fn().mockResolvedValue({
      predefined: [
        { id: 'all', name: '全部股票', description: '不限制股票池' },
        { id: 'idx_hs300', name: '沪深300', description: '沪深300指数成分股' },
        { id: 'idx_zz500', name: '中证500', description: '中证500指数成分股' },
      ],
    }),
  },
  mlApi: {
    experiments: vi.fn().mockResolvedValue({ items: [] }),
    modelsCatalog: vi.fn().mockResolvedValue({}),
  },
}))

const mockedCreate = vi.mocked(backtestsApi.create)

async function openAndRun() {
  renderWithProviders(
    <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
  )
  // Wait for dataset auto-selection to enable the run button (title and button share the label)
  const runBtn = await screen.findByRole('button', { name: '执行回测' })
  await waitFor(() => expect((runBtn as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(runBtn)
  await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
}

describe('BacktestRunModal rebalance frequency selector (P0\')', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
    } as never)
  })

  it('renders the selector with three options and safe default 1w (P4)', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const select = await screen.findByTestId('rebalance-frequency-select') as HTMLSelectElement
    expect(select.value).toBe('1w')
    const options = Array.from(select.querySelectorAll('option')).map(o => o.value)
    expect(options).toEqual(['1d', '1w', '1mo'])
  })

  it('sends rebalance_frequency: "1w" in the payload by default (P4 safe default)', async () => {
    await openAndRun()
    expect(mockedCreate).toHaveBeenCalledTimes(1)
    const body = mockedCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.rebalance_frequency).toBe('1w')
  })

  it('sends the selected daily frequency in the payload', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const select = await screen.findByTestId('rebalance-frequency-select')
    fireEvent.change(select, { target: { value: '1d' } })
    const runBtn = await screen.findByRole('button', { name: '执行回测' })
    await waitFor(() => expect((runBtn as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(runBtn)
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
    const body = mockedCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.rebalance_frequency).toBe('1d')
  })
})

describe('BacktestRunModal safe defaults + tiered estimates (P4)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
    } as never)
  })

  it('new user defaults to idx_hs300 universe and sends it in the payload', async () => {
    await openAndRun()
    const body = mockedCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.universe_id).toBe('idx_hs300')
    expect(body.rebalance_frequency).toBe('1w')
  })

  it('restores remembered explicit choices from localStorage (returning user)', async () => {
    localStorage.setItem(
      'cquant_run_modal_defaults',
      JSON.stringify({ universe_id: 'idx_zz500', rebalance_frequency: '1mo' }),
    )
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const freqSelect = await screen.findByTestId('rebalance-frequency-select') as HTMLSelectElement
    expect(freqSelect.value).toBe('1mo')
    const poolSelect = await screen.findByTestId('universe-select') as HTMLSelectElement
    expect(poolSelect.value).toBe('idx_zz500')
  })

  it('strategy config beats localStorage memory', async () => {
    localStorage.setItem(
      'cquant_run_modal_defaults',
      JSON.stringify({ universe_id: 'idx_zz500', rebalance_frequency: '1mo' }),
    )
    renderWithProviders(
      <BacktestRunModal
        strategyId="strat_1"
        configText='{"factors":["ret_20d"],"top_n":10,"universe_id":"all","rebalance_frequency":"1d"}'
        onClose={() => {}}
      />,
    )
    const freqSelect = await screen.findByTestId('rebalance-frequency-select') as HTMLSelectElement
    expect(freqSelect.value).toBe('1d')
    const poolSelect = await screen.findByTestId('universe-select') as HTMLSelectElement
    expect(poolSelect.value).toBe('all')
  })

  it('persists explicit user changes to localStorage', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const freqSelect = await screen.findByTestId('rebalance-frequency-select')
    fireEvent.change(freqSelect, { target: { value: '1mo' } })
    const stored = JSON.parse(localStorage.getItem('cquant_run_modal_defaults') ?? '{}')
    expect(stored.rebalance_frequency).toBe('1mo')
  })

  it('shows the index-pool estimate for the safe default and switches with selection', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const est = await screen.findByTestId('time-estimate')
    // idx_hs300 × 1w
    expect(est.textContent).toContain('约 2-5 秒')
    expect(est.textContent).toContain('约 1 分钟')

    const poolSelect = (await screen.findByTestId('universe-select')) as HTMLSelectElement
    expect(poolSelect.value).toBe('idx_hs300')
    fireEvent.change(poolSelect, { target: { value: 'all' } })
    // all × 1w（频率仍是安全默认 1w）
    await waitFor(() => expect(screen.getByTestId('time-estimate').textContent).toContain('约 10-20 秒'))

    const freqSelect = screen.getByTestId('rebalance-frequency-select')
    fireEvent.change(freqSelect, { target: { value: '1d' } })
    // all × 1d → 最慢档
    await waitFor(() => expect(screen.getByTestId('time-estimate').textContent).toContain('约 30 秒-1 分钟'))

    fireEvent.change(freqSelect, { target: { value: '1mo' } })
    await waitFor(() => expect(screen.getByTestId('time-estimate').textContent).toContain('约 5-10 秒'))
  })

  it('slow-combo warning appears for full market × daily and disappears otherwise', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    await screen.findByTestId('time-estimate')
    expect(screen.queryByTestId('slow-combo-warning')).not.toBeInTheDocument()

    const poolSelect = (await screen.findByTestId('universe-select')) as HTMLSelectElement
    expect(poolSelect.value).toBe('idx_hs300')
    fireEvent.change(poolSelect, { target: { value: 'all' } })
    // all × 1w（默认频率）→ 仍不是最慢组合，无警示
    expect(screen.queryByTestId('slow-combo-warning')).not.toBeInTheDocument()

    // 切到日频 → all × 1d 最慢组合警示出现
    fireEvent.change(screen.getByTestId('rebalance-frequency-select'), { target: { value: '1d' } })
    expect(await screen.findByTestId('slow-combo-warning')).toBeInTheDocument()

    // 切回周频 → 警告消失
    fireEvent.change(screen.getByTestId('rebalance-frequency-select'), { target: { value: '1w' } })
    await waitFor(() => expect(screen.queryByTestId('slow-combo-warning')).not.toBeInTheDocument())
  })
})

describe('BacktestRunModal precheck warnings (P3-7)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('renders each warning when create response contains warnings', async () => {
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
      warnings: ['regime 指标 ma_20 在 2025-03-01 起缺失 5 天', 'regime 指标 vol_60 覆盖不足'],
    } as never)
    await openAndRun()
    const box = await screen.findByTestId('precheck-warnings')
    expect(box).toBeInTheDocument()
    expect(screen.getByText('指标覆盖预检提示')).toBeInTheDocument()
    expect(screen.getByText(/ma_20 在 2025-03-01 起缺失 5 天/)).toBeInTheDocument()
    expect(screen.getByText(/vol_60 覆盖不足/)).toBeInTheDocument()
    expect(box.querySelectorAll('li').length).toBe(2)
  })

  it('does not render the warning box when warnings is empty or omitted', async () => {
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
    } as never)
    await openAndRun()
    // Give the mutation a tick to settle, then assert no warning box
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('precheck-warnings')).not.toBeInTheDocument()
  })

  it('still shows the running status after submit (flow not blocked)', async () => {
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
      warnings: ['一条警告'],
    } as never)
    await openAndRun()
    expect(await screen.findByTestId('precheck-warnings')).toBeInTheDocument()
    // Modal stays open polling the job — running spinner visible alongside warnings
    expect(await screen.findByText('运行中...')).toBeInTheDocument()
  })
})
