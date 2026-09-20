import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { AppLayout } from '../layout/AppLayout'
import { CommandPalette } from '../common/CommandPalette'
import { useThemeStore } from '@/stores/themeStore'
import i18n from 'i18next'
import { RECENT_KEY } from '../common/commandPalette/commands'

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }))

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateMock }
})

// Real themeStore uses zustand persist against jsdom's non-functional
// localStorage — replace it with a plain in-memory store of the same shape.
vi.mock('@/stores/themeStore', async () => {
  const { create } = await import('zustand')
  interface ThemeState {
    mode: 'light' | 'dark'
    toggle: () => void
    setMode: (mode: 'light' | 'dark') => void
  }
  const useThemeStore = create<ThemeState>()((set, get) => ({
    mode: 'light',
    toggle: () => set({ mode: get().mode === 'light' ? 'dark' : 'light' }),
    setMode: (mode) => set({ mode }),
  }))
  return { useThemeStore }
})

vi.mock('@/lib/api', () => ({
  mlApi: { experiments: vi.fn().mockResolvedValue({ items: [] }) },
  backtestsApi: { list: vi.fn().mockResolvedValue({ items: [
    { run_id: 'run_abc12345', strategy_id: 'mom10', status: 'done' },
  ] }) },
  scoringApi: { listSnapshots: vi.fn().mockResolvedValue({ items: [] }) },
  alertsApi: { history: vi.fn().mockResolvedValue({ unread_count: 0 }) },
  jobsApi: { cancel: vi.fn(), delete: vi.fn() },
  factorsApi: { getAvailable: vi.fn().mockResolvedValue({ factors: [
    { name: 'mom_20d', label_zh: '20日动量', label_en: '20d Momentum', category: 'momentum' },
  ] }) },
  strategiesApi: { list: vi.fn().mockResolvedValue({ items: [
    { strategy_id: 'top10' },
  ] }) },
}))

function stubLocalStorage() {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
  })
}

describe('CommandPalette (Cmd+K)', () => {
  beforeEach(() => {
    stubLocalStorage()
    navigateMock.mockClear()
    useThemeStore.setState({ mode: 'light' })
    void i18n.changeLanguage('zh-CN')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('AppLayout opens the palette on Ctrl+K and closes on Esc', async () => {
    renderWithProviders(<AppLayout />)
    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(screen.getByTestId('command-palette')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument())
  })

  it('opens on Cmd+K (metaKey) too', () => {
    renderWithProviders(<AppLayout />)
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(screen.getByTestId('command-palette')).toBeInTheDocument()
  })

  it('fuzzy-filters commands by substring and subsequence', async () => {
    renderWithProviders(<CommandPalette open onClose={() => {}} />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '因子' } })
    // zh-CN labels from the shared i18n instance; debounce (150ms) applies first
    await waitFor(() => expect(screen.queryByText('策略配置')).not.toBeInTheDocument())
    expect(screen.getByText('因子研究')).toBeInTheDocument()
    // subsequence match against the path keyword
    fireEvent.change(input, { target: { value: 'bts' } })
    await waitFor(() => expect(screen.queryByText('因子研究')).not.toBeInTheDocument())
    expect(screen.getByText('回测评估')).toBeInTheDocument()
    // ArrowDown moves the selection to the next option (回测对比)
    const options = screen.getAllByRole('option')
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(options[1]).toHaveAttribute('aria-selected', 'true')
  })

  it('navigates on Enter and records recent usage', async () => {
    const onClose = vi.fn()
    renderWithProviders(<CommandPalette open onClose={onClose} />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'backtests' } })
    // Wait for the debounced filter to drop non-matching commands (e.g. 总览)
    await waitFor(() => expect(screen.queryByText('总览')).not.toBeInTheDocument())
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigateMock).toHaveBeenCalledWith('/backtests')
    expect(onClose).toHaveBeenCalled()
    expect(JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')).toContain('page:/backtests')
  })

  it('shows recent commands first when query is empty', () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify(['page:/pipeline']))
    renderWithProviders(<CommandPalette open onClose={() => {}} />)
    const listbox = screen.getByRole('listbox')
    const first = listbox.querySelector('[role="option"]')
    expect(first).toHaveTextContent('自动化管道')
    expect(screen.getByText('最近使用')).toBeInTheDocument()
  })

  it('theme toggle action flips the theme store', () => {
    renderWithProviders(<CommandPalette open onClose={() => {}} />)
    fireEvent.click(screen.getByText('切换主题'))
    expect(useThemeStore.getState().mode).toBe('dark')
  })

  it('language toggle action calls i18n.changeLanguage', () => {
    const spy = vi.spyOn(i18n, 'changeLanguage')
    renderWithProviders(<CommandPalette open onClose={() => {}} />)
    fireEvent.click(screen.getByText('切换语言 / Switch Language'))
    expect(spy).toHaveBeenCalledWith('en-US')
    spy.mockRestore()
  })

  it('renders loaded factor/strategy/backtest search sources', async () => {
    renderWithProviders(<CommandPalette open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('20日动量')).toBeInTheDocument())
    expect(screen.getByText('top10')).toBeInTheDocument()
    expect(screen.getByText(/run_abc123/)).toBeInTheDocument()
  })
})
