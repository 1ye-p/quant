import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../../test-utils'  // Initialize i18n singleton (zh-CN)
import { StrategyDSLEditor } from './StrategyDSLEditor'

vi.mock('@monaco-editor/react', () => {
  const Editor = ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <textarea data-testid="monaco-mock" value={value} onChange={e => onChange(e.target.value)} />
  )
  return { default: Editor }
})

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('StrategyDSLEditor', () => {
  it('renders segmented form sections in form mode', () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    expect(screen.getByText('基本信息')).toBeInTheDocument()
    expect(screen.getByText('打分因子 (score)')).toBeInTheDocument()
    expect(screen.getByText('仓位 (position)')).toBeInTheDocument()
    expect(screen.getByText('风控 (risk)')).toBeInTheDocument()
    expect(screen.getByText('市场状态 (regime)')).toBeInTheDocument()
  })

  it('shows Chinese zod errors mapped to fields on save attempt', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() =>
      expect(screen.getByText('name: 策略名不能为空')).toBeInTheDocument(),
    )
  })

  it('syncs form to YAML when switching to YAML mode', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    const nameInput = screen.getAllByRole('textbox')[0] as HTMLInputElement
    fireEvent.change(nameInput, { target: { value: 'demo_dsl' } })
    fireEvent.click(screen.getByText('YAML 源码'))
    await waitFor(() => expect(screen.getByTestId('monaco-mock')).toBeInTheDocument())
    expect((screen.getByTestId('monaco-mock') as HTMLTextAreaElement).value).toContain('name: demo_dsl')
  })

  it('blocks switch back to form on YAML syntax error', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getByText('YAML 源码'))
    await waitFor(() => expect(screen.getByTestId('monaco-mock')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('monaco-mock'), { target: { value: 'name: [unclosed' } })
    fireEvent.click(screen.getByText('表单模式'))
    // stays in YAML mode; syntax error surfaced
    expect(screen.getByTestId('monaco-mock')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/YAML 解析失败/)).toBeInTheDocument())
  })
})
