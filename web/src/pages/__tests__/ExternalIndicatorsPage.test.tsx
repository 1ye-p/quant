import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { ExternalIndicatorsPage } from '../ExternalIndicatorsPage'

const { listExtIndBuiltinsMock, enableExtIndBuiltinMock, refreshExtIndicatorsMock, listExtIndRunsMock } =
  vi.hoisted(() => ({
    listExtIndBuiltinsMock: vi.fn(),
    enableExtIndBuiltinMock: vi.fn(),
    refreshExtIndicatorsMock: vi.fn(),
    listExtIndRunsMock: vi.fn(),
  }))

vi.mock('@/lib/api', () => ({
  datasetsApi: {
    listExtIndCatalog: vi.fn().mockResolvedValue({
      total: 2,
      items: [
        {
          indicator_key: 'margin_balance',
          display_name: '两融余额',
          unit: null,
          description: null,
          source_type: 'csv',
          source_name: 'manual',
          pinned_source: null,
          source_config: null,
          available_date_rule: 'B' as const,
          frequency: 'daily' as const,
          backfill_start: null,
          enabled: true,
          last_refresh_at: '2026-09-28T10:00:00',
          last_status: 'ok',
          last_error: null,
          updated_at: null,
          latest_trade_date: '2026-09-25',
          stale: false,
        },
        {
          indicator_key: 'north_flow',
          display_name: '北向资金',
          unit: null,
          description: null,
          source_type: 'csv',
          source_name: 'manual',
          pinned_source: null,
          source_config: null,
          available_date_rule: 'B' as const,
          frequency: 'weekly' as const,
          backfill_start: null,
          enabled: true,
          last_refresh_at: null,
          last_status: 'never_run',
          last_error: null,
          updated_at: null,
          latest_trade_date: null,
          stale: true,
        },
      ],
    }),
    listExtIndBuiltins: listExtIndBuiltinsMock,
    enableExtIndBuiltin: enableExtIndBuiltinMock,
    refreshExtIndicators: refreshExtIndicatorsMock,
    listExtIndRuns: listExtIndRunsMock,
  },
}))

const BUILTIN_ITEMS = [
  {
    indicator_key: 'margin_balance',
    display_name: '两融余额',
    unit: '亿元',
    description: null,
    available_date_rule: 'B' as const,
    frequency: 'daily' as const,
    default_backfill_years: 2,
    candidates: [
      { name: 'tushare', ready: false },
      { name: 'akshare', ready: true },
    ],
    enabled: false,
  },
  {
    indicator_key: 'shibor_3m',
    display_name: 'SHIBOR 3M',
    unit: '%',
    description: null,
    available_date_rule: 'A' as const,
    frequency: 'daily' as const,
    default_backfill_years: 3,
    candidates: [{ name: 'akshare', ready: true }],
    enabled: true,
  },
]

function setupBuiltinMocks() {
  listExtIndBuiltinsMock.mockResolvedValue({ items: BUILTIN_ITEMS, total: 2 })
  enableExtIndBuiltinMock.mockResolvedValue({ backfill: 'pending' })
  refreshExtIndicatorsMock.mockResolvedValue({
    trigger: 'manual',
    started_at: '2026-10-01T00:00:00+00:00',
    finished_at: '2026-10-01T00:01:00+00:00',
    results: [],
    ok: 1,
    error: 0,
  })
  listExtIndRunsMock.mockResolvedValue({
    items: [
      {
        run_id: 3,
        indicator_key: 'shibor_3m',
        source_name: 'akshare',
        started_at: '2026-09-30T02:00:00+00:00',
        finished_at: null,
        status: 'running',
        trigger: 'manual',
        range_start: '2026-09-25',
        range_end: '2026-09-30',
        rows_fetched: 4,
        rows_upserted: 4,
        error: null,
        interrupted: true,
      },
      {
        run_id: 2,
        indicator_key: 'shibor_3m',
        source_name: 'akshare',
        started_at: '2026-09-29T02:00:00+00:00',
        finished_at: '2026-09-29T02:01:00+00:00',
        status: 'ok',
        trigger: 'schedule',
        range_start: '2026-09-24',
        range_end: '2026-09-29',
        rows_fetched: 5,
        rows_upserted: 4,
        error: null,
        interrupted: false,
      },
    ],
    total: 2,
  })
}

describe('ExternalIndicatorsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders page title and catalog rows with stale badge', async () => {
    renderWithProviders(<ExternalIndicatorsPage />)
    expect(screen.getByText('外部指标管理')).toBeInTheDocument()
    expect(await screen.findByText('margin_balance')).toBeInTheDocument()
    expect(screen.getByText('north_flow')).toBeInTheDocument()
    // stale badge only on the stale row
    expect(screen.getByText('过期')).toBeInTheDocument()
    // four tabs
    expect(screen.getByText('指标列表')).toBeInTheDocument()
    expect(screen.getByText('CSV 导入')).toBeInTheDocument()
    expect(screen.getByText('内置目录')).toBeInTheDocument()
    expect(screen.getByText('自定义源')).toBeInTheDocument()
  })

  it('builtin tab lists registry rows with candidate readiness and enable/refresh actions', async () => {
    setupBuiltinMocks()
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('内置目录'))

    expect(await screen.findByText('SHIBOR 3M')).toBeInTheDocument()
    expect(screen.getByText('两融余额')).toBeInTheDocument()
    // candidate readiness badges (akshare appears on both rows)
    expect(screen.getByText(/tushare ❌/)).toBeInTheDocument()
    expect(screen.getAllByText(/akshare ✅/).length).toBeGreaterThan(0)
    // disabled row → enable; enabled row → single-key refresh
    expect(screen.getByText('启用并回填')).toBeInTheDocument()
    expect(screen.getByText('立即刷新')).toBeInTheDocument()
    // page-level full refresh entry
    expect(screen.getByText('全部刷新（到期）')).toBeInTheDocument()
  })

  it('enable flow opens dialog, confirms with default backfill, calls enable endpoint', async () => {
    setupBuiltinMocks()
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('内置目录'))
    fireEvent.click(await screen.findByText('启用并回填'))

    expect(await screen.findByText(/启用内置指标：margin_balance/)).toBeInTheDocument()
    // row button + dialog confirm share the label; the dialog one is last
    const enableButtons = screen.getAllByRole('button', { name: '启用并回填' })
    fireEvent.click(enableButtons[enableButtons.length - 1])

    await waitFor(() =>
      expect(enableExtIndBuiltinMock).toHaveBeenCalledWith('margin_balance', undefined),
    )
  })

  it('single-row refresh posts an explicit keys array (never a full refresh)', async () => {
    setupBuiltinMocks()
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('内置目录'))
    fireEvent.click(await screen.findByText('立即刷新'))

    await waitFor(() => expect(refreshExtIndicatorsMock).toHaveBeenCalledWith({ keys: ['shibor_3m'] }))
  })

  it('full refresh requires confirmation modal before posting empty body', async () => {
    setupBuiltinMocks()
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('内置目录'))
    fireEvent.click(await screen.findByText('全部刷新（到期）'))

    // confirm dialog appears; nothing called yet
    expect(await screen.findByText('全部刷新（到期指标）')).toBeInTheDocument()
    expect(refreshExtIndicatorsMock).not.toHaveBeenCalled()
    // confirm button inside dialog (second occurrence of the label)
    fireEvent.click(screen.getAllByText('全部刷新（到期）')[1])
    await waitFor(() => expect(refreshExtIndicatorsMock).toHaveBeenCalledWith({}))
  })

  it('run history shows interrupted annotation and rows', async () => {
    setupBuiltinMocks()
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('内置目录'))
    fireEvent.click(await screen.findByText('展开'))

    expect(await screen.findByText('已中断')).toBeInTheDocument()
    expect(screen.getByText('4/4')).toBeInTheDocument()
    expect(listExtIndRunsMock).toHaveBeenCalledWith('', 20)
  })
})
