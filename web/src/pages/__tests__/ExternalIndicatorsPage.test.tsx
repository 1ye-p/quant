import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { ExternalIndicatorsPage } from '../ExternalIndicatorsPage'
import { ApiError } from '@/lib/api'

const {
  listExtIndBuiltinsMock,
  enableExtIndBuiltinMock,
  refreshExtIndicatorsMock,
  listExtIndRunsMock,
  createExtIndCustomMock,
  testExtIndCustomMock,
  getExtIndCatalogMock,
  patchExtIndCatalogMock,
  deleteExtIndCatalogMock,
} = vi.hoisted(() => ({
  listExtIndBuiltinsMock: vi.fn(),
  enableExtIndBuiltinMock: vi.fn(),
  refreshExtIndicatorsMock: vi.fn(),
  listExtIndRunsMock: vi.fn(),
  createExtIndCustomMock: vi.fn(),
  testExtIndCustomMock: vi.fn(),
  getExtIndCatalogMock: vi.fn(),
  patchExtIndCatalogMock: vi.fn(),
  deleteExtIndCatalogMock: vi.fn(),
}))

// Spread the actual module so ApiError / extractExtIndStageError stay real;
// only datasetsApi methods are stubbed.
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
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
    createExtIndCustom: createExtIndCustomMock,
    testExtIndCustom: testExtIndCustomMock,
    getExtIndCatalog: getExtIndCatalogMock,
    patchExtIndCatalog: patchExtIndCatalogMock,
    deleteExtIndCatalog: deleteExtIndCatalogMock,
  },
  }
})

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

  // ── Custom sources tab (P3-5) ──────────────────────────────────────────────

  it('custom tab lists custom_http rows only and opens the create form', async () => {
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('自定义源'))

    expect(await screen.findByText('共 0 个自定义源')).toBeInTheDocument()
    fireEvent.click(screen.getByText('新建自定义源'))
    expect(screen.getByText('新建自定义源（custom_http）')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('my_indicator')).toBeInTheDocument()
  })

  it('test button posts the form config and renders the sample table', async () => {
    testExtIndCustomMock.mockResolvedValue({
      sample: [
        { trade_date: '2026-09-28', value: 123.45 },
        { trade_date: '2026-09-29', value: null },
      ],
      diagnostics: {
        resolved_url: 'https://api.example.com/v1/data?d=20260929',
        status: 'ok',
        rows_parsed: 2,
        field_map_hit: true,
      },
    })
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('自定义源'))
    fireEvent.click(screen.getByText('新建自定义源'))

    fireEvent.change(screen.getByPlaceholderText('my_indicator'), { target: { value: 'my_ind' } })
    fireEvent.change(screen.getByPlaceholderText('https://api.example.com/v1/data?d={date}'), {
      target: { value: 'https://api.example.com/v1/data?d={date}' },
    })
    fireEvent.click(screen.getByText('测试连接'))

    await waitFor(() => expect(testExtIndCustomMock).toHaveBeenCalledTimes(1))
    const cfg = testExtIndCustomMock.mock.calls[0][0]
    expect(cfg.url_template).toBe('https://api.example.com/v1/data?d={date}')
    expect(cfg.method).toBe('GET')
    expect(cfg.date_param_style).toBe('yyyymmdd')
    expect(cfg.extraction.records_path).toBe('$.data.list')
    expect(cfg.extraction.field_map).toEqual({ trade_date: 'trade_date', value: 'value' })

    expect(await screen.findByText('测试成功（未入库）')).toBeInTheDocument()
    expect(screen.getByText('123.45')).toBeInTheDocument()
    expect(screen.getByText(/api.example.com\/v1\/data\?d=20260929/)).toBeInTheDocument()
  })

  it('test failure with a stage tag maps to the bilingual stage message', async () => {
    testExtIndCustomMock.mockRejectedValue(
      new ApiError('HTTP 400', {
        status: 400,
        details: { detail: { stage: 'ssrf_blocked', message: 'URL has no hostname' } },
      }),
    )
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('自定义源'))
    fireEvent.click(screen.getByText('新建自定义源'))
    fireEvent.change(screen.getByPlaceholderText('my_indicator'), { target: { value: 'my_ind' } })
    fireEvent.change(screen.getByPlaceholderText('https://api.example.com/v1/data?d={date}'), {
      target: { value: 'https://api.example.com/v1/data?d={date}' },
    })
    fireEvent.click(screen.getByText('测试连接'))

    expect(await screen.findByText(/请求被 SSRF 防护拦截/)).toBeInTheDocument()
    expect(screen.getByText('URL has no hostname')).toBeInTheDocument()
  })

  it('save posts the create body and returns to the row list on success', async () => {
    createExtIndCustomMock.mockResolvedValue({ indicator_key: 'my_ind' })
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('自定义源'))
    fireEvent.click(screen.getByText('新建自定义源'))
    fireEvent.change(screen.getByPlaceholderText('my_indicator'), { target: { value: 'my_ind' } })
    fireEvent.change(screen.getByPlaceholderText('https://api.example.com/v1/data?d={date}'), {
      target: { value: 'https://api.example.com/v1/data?d={date}' },
    })
    fireEvent.click(screen.getByText('保存'))

    await waitFor(() => expect(createExtIndCustomMock).toHaveBeenCalledTimes(1))
    const body = createExtIndCustomMock.mock.calls[0][0]
    expect(body.indicator_key).toBe('my_ind')
    expect(body.frequency).toBe('daily')
    expect(body.available_date_rule).toBe('B')
    expect(body.source_config.extraction.type).toBe('jsonpath')
    // back on the row list
    expect(await screen.findByText('新建自定义源')).toBeInTheDocument()
    expect(screen.queryByText('保存')).not.toBeInTheDocument()
  })

  it('409 duplicate key surfaces the duplicate message without leaving the form', async () => {
    createExtIndCustomMock.mockRejectedValue(
      new ApiError("indicator_key 已存在：'my_ind'", { status: 409 }),
    )
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click(screen.getByText('自定义源'))
    fireEvent.click(screen.getByText('新建自定义源'))
    fireEvent.change(screen.getByPlaceholderText('my_indicator'), { target: { value: 'my_ind' } })
    fireEvent.change(screen.getByPlaceholderText('https://api.example.com/v1/data?d={date}'), {
      target: { value: 'https://api.example.com/v1/data?d={date}' },
    })
    fireEvent.click(screen.getByText('保存'))

    await waitFor(() => expect(createExtIndCustomMock).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/指标键已存在（409）/)).toBeInTheDocument()
    // form is still open
    expect(screen.getByText('新建自定义源（custom_http）')).toBeInTheDocument()
  })

  // ── Delete dialog resurrect notice (backlog #10) ───────────────────────────

  it('delete dialog shows the resurrect notice only when purge is unchecked', async () => {
    deleteExtIndCatalogMock.mockResolvedValue({ deleted: 'margin_balance', purged_data: false })
    renderWithProviders(<ExternalIndicatorsPage />)
    fireEvent.click((await screen.findAllByText('删除'))[0])

    // purge unchecked → catalog row will be recreated by the startup migration
    expect(await screen.findByText(/下次服务启动迁移时自动重建/)).toBeInTheDocument()
    expect(screen.getByText('仅删目录')).toBeInTheDocument()

    // checking purge hides the notice and switches to the destructive wording
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.queryByText(/下次服务启动迁移时自动重建/)).not.toBeInTheDocument()
    expect(screen.getByText('连数据一起删除')).toBeInTheDocument()
  })
})
