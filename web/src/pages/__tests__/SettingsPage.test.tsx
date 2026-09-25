/**
 * SettingsPage — API key 配置流程：状态徽标渲染、保存写入 localStorage、
 * 保存后出现"测试连接/清除"按钮。
 */

import { renderWithProviders } from '../../test-utils'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { authApi } from '@/lib/api/auth'
import { getApiKey } from '@/lib/api/apiKey'
import { SettingsPage } from '../SettingsPage'

vi.mock('@/lib/api/auth', () => ({
  authApi: {
    status: vi.fn().mockResolvedValue({ key_configured: true, mode: 'strict' }),
    verify: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const mockedStatus = vi.mocked(authApi.status)

function renderPage() {
  return renderWithProviders(<SettingsPage />)
}

describe('SettingsPage — API key configuration', () => {
  beforeEach(() => {
    localStorage.clear()
    mockedStatus.mockResolvedValue({ key_configured: true, mode: 'strict' })
  })

  it('renders server auth status badges', async () => {
    renderPage()
    expect(await screen.findByText('服务端已配置 Key')).toBeInTheDocument()
    expect(screen.getByText('模式：strict')).toBeInTheDocument()
    expect(screen.getByText('本机未保存 Key')).toBeInTheDocument()
  })

  it('save writes the key to localStorage and reveals test/clear buttons', async () => {
    renderPage()
    const input = await screen.findByPlaceholderText('粘贴 API Key（32 位）…')
    fireEvent.change(input, { target: { value: '  test-key-1  ' } })
    fireEvent.click(screen.getByText('保存'))

    expect(getApiKey()).toBe('test-key-1')
    expect(screen.getByText('本机已保存 Key')).toBeInTheDocument()
    expect(screen.getByText('测试连接')).toBeInTheDocument()
    expect(screen.getByText('清除')).toBeInTheDocument()
  })

  it('clear removes the stored key', async () => {
    localStorage.setItem('cquant.apiKey', 'old-key')
    renderPage()
    fireEvent.click(await screen.findByText('清除'))
    expect(getApiKey()).toBe('')
    expect(screen.getByText('本机未保存 Key')).toBeInTheDocument()
  })

  it('dev mode badge shown when server reports dev', async () => {
    mockedStatus.mockResolvedValue({ key_configured: false, mode: 'dev' })
    renderPage()
    // 注意：加载态默认值恰好是"未配置 Key + strict"，必须等 dev 徽标本身出现
    expect(await screen.findByText('模式：dev（非交易端点免认证）')).toBeInTheDocument()
    expect(screen.getByText('服务端未配置 Key（当前无需认证）')).toBeInTheDocument()
  })
})
