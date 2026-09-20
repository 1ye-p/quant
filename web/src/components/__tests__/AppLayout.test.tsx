import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { AppLayout } from '../layout/AppLayout'

vi.mock('@/lib/api', () => ({
  mlApi: { experiments: vi.fn().mockResolvedValue({ items: [] }) },
  backtestsApi: { list: vi.fn().mockResolvedValue({ items: [] }) },
  scoringApi: { listSnapshots: vi.fn().mockResolvedValue({ items: [] }) },
  alertsApi: { history: vi.fn().mockResolvedValue({ unread_count: 0 }) },
  jobsApi: {
    cancel: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))

describe('AppLayout onboarding banner', () => {
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

  it('shows the first-run banner for a new user', () => {
    renderWithProviders(<AppLayout />)
    expect(screen.getByTestId('onboarding-banner')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始引导' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '跳过' })).toBeInTheDocument()
  })

  it('skipping hides the banner and marks onboarding done', () => {
    renderWithProviders(<AppLayout />)
    fireEvent.click(screen.getByRole('button', { name: '跳过' }))
    expect(screen.queryByTestId('onboarding-banner')).not.toBeInTheDocument()
    expect(localStorage.getItem('cquant_onboarded')).toBe('1')
  })

  it('renders nothing for an already-onboarded user', () => {
    localStorage.setItem('cquant_onboarded', '1')
    renderWithProviders(<AppLayout />)
    expect(screen.queryByTestId('onboarding-banner')).not.toBeInTheDocument()
  })
})
