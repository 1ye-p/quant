import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import i18n from 'i18next'
import '../../test-utils'  // Initialize i18n singleton (zh-CN)
import { StrategyDSLEditor, configToForm } from './StrategyDSLEditor'

// Assertions go through the initialized i18n instance so the tests are
// decoupled from any hardcoded locale strings.
const t = (key: string) => i18n.t(key)

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
    expect(screen.getByText(t('component.strategy_dsl.section_info'))).toBeInTheDocument()
    expect(screen.getByText(t('component.strategy_dsl.section_score'))).toBeInTheDocument()
    expect(screen.getByText(t('component.strategy_dsl.section_position'))).toBeInTheDocument()
    expect(screen.getByText(t('component.strategy_dsl.section_risk'))).toBeInTheDocument()
    expect(screen.getByText(t('component.strategy_dsl.section_regime'))).toBeInTheDocument()
  })

  it('shows Chinese zod errors mapped to fields on save attempt', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getByText(t('common.save')))
    await waitFor(() =>
      expect(screen.getByText('name: 策略名不能为空')).toBeInTheDocument(),
    )
  })

  it('syncs form to YAML when switching to YAML mode', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    const nameInput = screen.getAllByRole('textbox')[0] as HTMLInputElement
    fireEvent.change(nameInput, { target: { value: 'demo_dsl' } })
    fireEvent.click(screen.getByText(t('component.strategy_dsl.mode_yaml')))
    await waitFor(() => expect(screen.getByTestId('monaco-mock')).toBeInTheDocument())
    expect((screen.getByTestId('monaco-mock') as HTMLTextAreaElement).value).toContain('name: demo_dsl')
  })

  it('blocks switch back to form on YAML syntax error', async () => {
    render(<StrategyDSLEditor strategyId="new" onClose={vi.fn()} onSaved={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getByText(t('component.strategy_dsl.mode_yaml')))
    await waitFor(() => expect(screen.getByTestId('monaco-mock')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('monaco-mock'), { target: { value: 'name: [unclosed' } })
    fireEvent.click(screen.getByText(t('component.strategy_dsl.mode_form')))
    // stays in YAML mode; syntax error surfaced
    expect(screen.getByTestId('monaco-mock')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/YAML 解析失败/)).toBeInTheDocument())
  })
})

describe('configToForm', () => {
  const baseDsl = {
    name: 'demo_dsl',
    universe: 'all',
    frequency: 'daily',
    score: [{ factor: 'ret_20d', weight: 1 }],
    position: { method: 'equal_weight', params: {}, constraints: {} },
    risk: [],
  }

  it('normalizes empty-string when rules to null (default)', () => {
    const configText = JSON.stringify({
      strategy_type: 'DSL',
      strategy_id: 'demo_dsl',
      dsl_spec: {
        ...baseDsl,
        regime: {
          mode: 'threshold',
          indicators: {},
          initial: null,
          reevaluate: null,
          states: null,
          rules: [
            { when: '', position_scale: 0.5 },
            { when: 'ma_20 > ma_60', position_scale: 1 },
          ],
          scale_expr: null,
        },
      },
    })
    const form = configToForm(configText)
    expect(form).not.toBeNull()
    expect(form?.regime?.rules?.[0].when).toBeNull()
    expect(form?.regime?.rules?.[1].when).toBe('ma_20 > ma_60')
  })

  it('roundtrips non-DSL config to null', () => {
    expect(configToForm(JSON.stringify({ strategy_type: 'StaticTopN' }))).toBeNull()
    expect(configToForm('not json')).toBeNull()
  })
})
