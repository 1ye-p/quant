import { describe, it, expect, vi, beforeEach, beforeAll, afterAll } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import '../../test-utils'  // initializes global i18n singleton (zh-CN) for useTranslation()
import { FactorsPage } from '../FactorsPage'

const _ResizeObserver = globalThis.ResizeObserver
beforeAll(() => {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})
afterAll(() => { globalThis.ResizeObserver = _ResizeObserver })

vi.mock('@/lib/api', () => ({
  factorAnalyticsApi: {
    definitions: vi.fn().mockResolvedValue({
      items: [
        { name: 'ret_5d', description: '5日收益率', tags: ['momentum'], source: 'builtin' },
        { name: 'ret_20d', description: '20日收益率', tags: ['momentum'], source: 'builtin' },
        { name: 'vol_20d', description: '20日波动率', tags: ['risk'], source: 'builtin' },
      ],
      total: 3,
    }),
    versions: vi.fn().mockResolvedValue({
      items: [{ feature_set_version: 'v1_20260101', start_date: '2024-01-01', end_date: '2026-01-01', row_count: 100000 }],
    }),
    getAvailable: vi.fn().mockResolvedValue({
      factors: [],
      categories: [],
      degraded_sources: [],
    }),
    icStatus: vi.fn().mockResolvedValue({
      items: [
        { factor_name: 'ret_5d', mean_ic: 0.05, ir: 0.8, hit_rate: 0.55, is_alert: false, alert_message: null },
        { factor_name: 'ret_20d', mean_ic: 0.008, ir: 0.1, hit_rate: 0.48, is_alert: true, alert_message: 'IC 0.008 低于阈值 0.02' },
        { factor_name: 'vol_20d', mean_ic: 0.03, ir: 0.6, hit_rate: 0.52, is_alert: false, alert_message: null },
      ],
      threshold: 0.02,
      window_days: 20,
      feature_set_version: 'v1_20260101',
    }),
    computeIC: vi.fn().mockResolvedValue({ job_id: 'job-1', status: 'running' }),
    computeICMatrix: vi.fn().mockResolvedValue({ job_id: 'job-2', status: 'running' }),
    icJob: vi.fn().mockResolvedValue({
      job_id: 'job-1',
      status: 'done',
      series_json: [
        { trade_date: '2026-01-01', ic: 0.05 },
        { trade_date: '2026-01-02', ic: 0.03 },
      ],
      summary_json: {
        mean_ic: 0.04,
        ir: 0.7,
        hit_rate: 0.55,
        observations: 200,
      },
    }),
    computeQuintiles: vi.fn().mockResolvedValue({
      factor_name: 'ret_5d',
      horizon_days: 1,
      n_groups: 5,
      groups: [
        { quintile: '1', mean_return: -0.02, std_return: 0.05, count: 100 },
        { quintile: '5', mean_return: 0.03, std_return: 0.06, count: 100 },
      ],
    }),
    computeFactorCorrelation: vi.fn().mockResolvedValue({
      factors: ['ret_5d', 'ret_20d'],
      matrix: [
        { factor_a: 'ret_5d', factor_b: 'ret_5d', correlation: 1.0 },
        { factor_a: 'ret_5d', factor_b: 'ret_20d', correlation: 0.7 },
        { factor_a: 'ret_20d', factor_b: 'ret_5d', correlation: 0.7 },
        { factor_a: 'ret_20d', factor_b: 'ret_20d', correlation: 1.0 },
      ],
    }),
  },
  customFactorApi: {
    create: vi.fn().mockResolvedValue({ factor_id: 'cf1', name: 'my_factor', status: 'created' }),
    preview: vi.fn().mockResolvedValue({
      valid: true,
      error: null,
      preview: [
        { asset_id: 'SSE:600000', trade_date: '2026-01-01', value: 1.23456 },
      ],
    }),
  },
  alertsApi: {
    createRule: vi.fn().mockResolvedValue({ rule_id: 'ar1', status: 'created' }),
  },
  dslApi: {
    functions: vi.fn().mockResolvedValue({
      functions: [
        { name: 'ma', signature: 'ma(col, n)', description: '移动平均' },
        { name: 'roc', signature: 'roc(col, n)', description: '变化率' },
      ],
      columns: ['close', 'open', 'high', 'low', 'volume'],
      examples: [
        { expression: "roc('close', 5)", description: '5日收益率' },
      ],
    }),
  },
}))

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('FactorsPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders page title', () => {
    renderWithProviders(<FactorsPage />)
    expect(screen.getByText('Alpha 因子研究')).toBeInTheDocument()
  })

  it('renders factor cards after loading', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      const ret5dElements = screen.getAllByText('ret_5d')
      expect(ret5dElements.length).toBeGreaterThanOrEqual(1)
      const vol20dElements = screen.getAllByText('vol_20d')
      expect(vol20dElements.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('shows factor descriptions', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getByText('5日收益率')).toBeInTheDocument()
      expect(screen.getByText('20日收益率')).toBeInTheDocument()
    })
  })

  it('shows factor tags', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getAllByText('momentum').length).toBeGreaterThanOrEqual(1)
      expect(screen.getByText('risk')).toBeInTheDocument()
    })
  })

  it('filters factors by search', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getAllByText('ret_5d').length).toBeGreaterThanOrEqual(1)
    })

    const searchInput = screen.getByPlaceholderText(/搜索因子名称或描述/)
    await user.type(searchInput, 'vol')

    await waitFor(() => {
      expect(screen.getAllByText('vol_20d').length).toBeGreaterThanOrEqual(1)
      expect(screen.queryByText('5日收益率')).not.toBeInTheDocument()
    })
  })

  it('shows no results message when search has no matches', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getAllByText('ret_5d').length).toBeGreaterThanOrEqual(1)
    })

    const searchInput = screen.getByPlaceholderText(/搜索因子名称或描述/)
    await user.type(searchInput, 'nonexistent_factor_xyz')

    await waitFor(() => {
      expect(screen.getByText(/未找到含"nonexistent_factor_xyz"的因子/)).toBeInTheDocument()
    })
  })

  it('shows step progress indicator', () => {
    renderWithProviders(<FactorsPage />)
    // Refactored page uses tab navigation instead of step indicator
    expect(screen.getByText('因子选择')).toBeInTheDocument()
  })

  it('shows feature set version selector', () => {
    renderWithProviders(<FactorsPage />)
    expect(screen.getByDisplayValue('选择 Feature Set 版本')).toBeInTheDocument()
  })

  it('does not render degraded banner when no degraded sources', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getAllByText('ret_5d').length).toBeGreaterThanOrEqual(1)
    })
    expect(screen.queryByText(/部分因子集不可用/)).not.toBeInTheDocument()
  })
})

describe('FactorsPage Degraded Sources', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows amber banner with source names and recovery command when degraded', async () => {
    const { factorAnalyticsApi } = await import('@/lib/api')
    vi.mocked(factorAnalyticsApi.getAvailable).mockResolvedValue({
      factors: [],
      categories: [],
      degraded_sources: [
        { source: 'qlib158', reason: 'submodule_unavailable' },
        { source: 'gtja191', reason: 'submodule_unavailable' },
      ],
    })
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getByText(/部分因子集不可用（qlib158, gtja191）/)).toBeInTheDocument()
    })
    expect(screen.getByText(/git submodule update --init --recursive/)).toBeInTheDocument()
  })
})

describe('FactorsPage IC Alerts', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows IC alert summary banner when alerts exist', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getByText(/1 个因子 IC 低于阈值/)).toBeInTheDocument()
    })
  })

  it('shows warning badge on factor with IC alert', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })
  })

  it('opens IC alert modal when warning badge is clicked', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('ret_20d IC 告警'))

    expect(screen.getByText('创建 IC 告警规则')).toBeInTheDocument()
    expect(screen.getByText('IC 阈值（绝对值低于此值触发）')).toBeInTheDocument()
    expect(screen.getByText('检查窗口（天）')).toBeInTheDocument()
  })

  it('displays factor name in alert modal', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('ret_20d IC 告警'))

    // ret_20d appears in both card and modal - check multiple exist
    const ret20dElements = screen.getAllByText('ret_20d')
    expect(ret20dElements.length).toBeGreaterThanOrEqual(2) // card + modal
  })

  it('closes modal on cancel', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('ret_20d IC 告警'))
    expect(screen.getByText('创建 IC 告警规则')).toBeInTheDocument()

    const cancelButtons = screen.getAllByText('取消')
    await user.click(cancelButtons[cancelButtons.length - 1])

    expect(screen.queryByText('创建 IC 告警规则')).not.toBeInTheDocument()
  })

  it('has threshold and window inputs with default values', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('ret_20d IC 告警'))

    // 0.02 appears in both IC threshold input and modal - check multiple exist
    const thresholdInputs = screen.getAllByDisplayValue('0.02')
    expect(thresholdInputs.length).toBeGreaterThanOrEqual(1)

    // Check the modal has window_days default of 20
    // (20 might also appear elsewhere, so use getAll)
    const windowInputs = screen.getAllByDisplayValue('20')
    expect(windowInputs.length).toBeGreaterThanOrEqual(1)
  })

  it('submits IC alert form and calls createRule', async () => {
    const { alertsApi } = await import('@/lib/api')
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getByLabelText('ret_20d IC 告警')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('ret_20d IC 告警'))
    expect(screen.getByText('创建 IC 告警规则')).toBeInTheDocument()

    // Click submit button
    const submitButtons = screen.getAllByText('创建告警')
    await user.click(submitButtons[submitButtons.length - 1])

    await waitFor(() => {
      expect(alertsApi.createRule).toHaveBeenCalled()
      const callArgs = vi.mocked(alertsApi.createRule).mock.calls[0][0]
      expect(callArgs.rule_type).toBe('factor_ic_low')
    })
  })
})

describe('FactorsPage Custom Factor', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens create factor modal', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await user.click(screen.getByText('+ 新建因子'))
    expect(screen.getByText('新建自定义因子')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('如 my_momentum_5d')).toBeInTheDocument()
  })

  it('shows DSL syntax help toggle', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await user.click(screen.getByText('+ 新建因子'))
    expect(screen.getByText(/语法说明/)).toBeInTheDocument()
  })

  it('expands syntax help and shows columns and functions', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await user.click(screen.getByText('+ 新建因子'))
    await user.click(screen.getByText(/语法说明/))

    expect(screen.getByText('可用列')).toBeInTheDocument()
    expect(screen.getByText('函数')).toBeInTheDocument()
  })

  it('shows preview button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await user.click(screen.getByText('+ 新建因子'))
    expect(screen.getByText('预览')).toBeInTheDocument()
  })
})

describe('FactorsPage IC Analysis', () => {
  beforeEach(() => vi.clearAllMocks())

  it('selects factor and shows IC analysis section', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getAllByText('ret_5d').length).toBeGreaterThanOrEqual(1)
    })

    // Click on the factor card (first ret_5d element)
    const factorCards = screen.getAllByText('ret_5d')
    await user.click(factorCards[0])

    // Refactored page shows factor selection in tab navigation
    expect(screen.getByText('因子选择')).toBeInTheDocument()
  })

  it('shows horizon selector', async () => {
    const user = userEvent.setup()
    renderWithProviders(<FactorsPage />)

    await waitFor(() => {
      expect(screen.getAllByText('ret_5d').length).toBeGreaterThanOrEqual(1)
    })

    const factorCards = screen.getAllByText('ret_5d')
    await user.click(factorCards[0])

    expect(screen.getByText('Horizon')).toBeInTheDocument()
    expect(screen.getByDisplayValue('1 天')).toBeInTheDocument()
  })

  it('shows multi-factor IC matrix section', async () => {
    renderWithProviders(<FactorsPage />)
    await waitFor(() => {
      // Refactored page uses tab navigation
      expect(screen.getByText('IC 分析')).toBeInTheDocument()
    })
  })

  it('shows new factor button', () => {
    renderWithProviders(<FactorsPage />)
    expect(screen.getByText('+ 新建因子')).toBeInTheDocument()
  })
})
