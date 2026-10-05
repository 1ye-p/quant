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
    universes: vi.fn().mockResolvedValue({ predefined: [] }),
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
    mockedCreate.mockResolvedValue({
      job_id: 'job_1',
      strategy_id: 'strat_1',
      status: 'running',
    } as never)
  })

  it('renders the selector with three options and default 1d', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const select = await screen.findByTestId('rebalance-frequency-select') as HTMLSelectElement
    expect(select.value).toBe('1d')
    const options = Array.from(select.querySelectorAll('option')).map(o => o.value)
    expect(options).toEqual(['1d', '1w', '1mo'])
  })

  it('sends rebalance_frequency: "1d" in the payload by default', async () => {
    await openAndRun()
    expect(mockedCreate).toHaveBeenCalledTimes(1)
    const body = mockedCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.rebalance_frequency).toBe('1d')
  })

  it('sends the selected weekly frequency in the payload', async () => {
    renderWithProviders(
      <BacktestRunModal strategyId="strat_1" configText='{"factors":["ret_20d"],"top_n":10}' onClose={() => {}} />,
    )
    const select = await screen.findByTestId('rebalance-frequency-select')
    fireEvent.change(select, { target: { value: '1w' } })
    const runBtn = await screen.findByRole('button', { name: '执行回测' })
    await waitFor(() => expect((runBtn as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(runBtn)
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
    const body = mockedCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.rebalance_frequency).toBe('1w')
  })
})

describe('BacktestRunModal precheck warnings (P3-7)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
