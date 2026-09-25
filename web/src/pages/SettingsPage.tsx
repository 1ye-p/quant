import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { authApi } from '@/lib/api/auth'
import { getApiKey, setApiKey, clearApiKey } from '@/lib/api/apiKey'

/**
 * 设置页 — API Key 配置。
 *
 * 服务端配置 CQUANT_API_KEY 后，所有 /api/v1/* 请求需携带
 * Authorization: Bearer <key>。key 保存在浏览器 localStorage，
 * client 层每个请求自动附加，无需刷新页面即生效。
 */
export function SettingsPage() {
  const { t } = useTranslation()
  const [input, setInput] = useState('')
  const [hasLocalKey, setHasLocalKey] = useState(() => !!getApiKey())
  const [testing, setTesting] = useState(false)

  const { data: status } = useQuery({
    queryKey: ['settings', 'auth-status'],
    queryFn: () => authApi.status(),
  })

  const handleSave = () => {
    if (!input.trim()) {
      toast.error(t('page.settings.error_empty'))
      return
    }
    setApiKey(input)
    setHasLocalKey(true)
    setInput('')
    toast.success(t('page.settings.toast_saved'))
  }

  const handleClear = () => {
    clearApiKey()
    setHasLocalKey(false)
    toast.success(t('page.settings.toast_cleared'))
  }

  const handleTest = async () => {
    setTesting(true)
    try {
      await authApi.verify()
      toast.success(t('page.settings.toast_valid'))
    } catch (e) {
      toast.error(t('page.settings.toast_invalid', { message: e instanceof Error ? e.message : '' }))
    } finally {
      setTesting(false)
    }
  }

  const keyConfigured = status?.key_configured ?? false
  const mode = status?.mode ?? 'strict'

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">{t('page.settings.title')}</h1>
        <p className="text-sm text-gray-500 mt-1">{t('page.settings.subtitle')}</p>
      </div>

      <div className="card p-5 space-y-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">
            {t('page.settings.api_key_card.title')}
          </h2>
          <p className="text-sm text-gray-500 mt-1">{t('page.settings.api_key_card.desc')}</p>
        </div>

        {/* 服务端状态 */}
        <div className="flex flex-wrap gap-2 text-sm">
          <span
            className={`px-2 py-1 rounded ${
              keyConfigured ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'
            }`}
          >
            {keyConfigured
              ? t('page.settings.server.key_configured')
              : t('page.settings.server.key_not_configured')}
          </span>
          <span className="px-2 py-1 rounded bg-gray-100 text-gray-600">
            {mode === 'dev'
              ? t('page.settings.server.mode_dev')
              : t('page.settings.server.mode_strict')}
          </span>
          <span
            className={`px-2 py-1 rounded ${
              hasLocalKey ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-500'
            }`}
          >
            {hasLocalKey
              ? t('page.settings.local.saved')
              : t('page.settings.local.not_saved')}
          </span>
        </div>

        {/* 输入 + 操作 */}
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t('page.settings.placeholder_key')}
            className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
            autoComplete="off"
          />
          <button
            onClick={handleSave}
            className="px-4 py-2 rounded-md bg-brand-600 text-white text-sm hover:bg-brand-700 disabled:opacity-50"
            disabled={!input.trim()}
          >
            {t('page.settings.btn_save')}
          </button>
          {hasLocalKey && (
            <>
              <button
                onClick={handleTest}
                disabled={testing}
                className="px-4 py-2 rounded-md border border-gray-300 text-sm hover:bg-gray-50 disabled:opacity-50"
              >
                {testing ? t('page.settings.btn_testing') : t('page.settings.btn_test')}
              </button>
              <button
                onClick={handleClear}
                className="px-4 py-2 rounded-md border border-red-200 text-red-600 text-sm hover:bg-red-50"
              >
                {t('page.settings.btn_clear')}
              </button>
            </>
          )}
        </div>

        {/* 说明 */}
        <div className="text-xs text-gray-500 space-y-1 border-t pt-3">
          <p>{t('page.settings.hint_generate')}</p>
          <code className="block bg-gray-100 rounded px-2 py-1 font-mono">
            python -m cquant.cli.main auth generate-key
          </code>
          <p>{t('page.settings.note_trading')}</p>
        </div>
      </div>
    </div>
  )
}
