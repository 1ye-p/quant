/**
 * Global command palette (Cmd+K / Ctrl+K).
 *
 * UI layer only — command construction, fuzzy matching and recency
 * persistence live in ./commandPalette/commands.ts.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { factorsApi, strategiesApi, backtestsApi } from '@/lib/api'
import type { AvailableFactor } from '@/lib/api/factors'
import { useThemeStore } from '@/stores/themeStore'
import {
  buildStaticCommands,
  commandMatches,
  pushRecent,
  readRecent,
  type CommandGroup,
  type CommandItem,
} from './commandPalette/commands'

const SEARCH_DEBOUNCE_MS = 150
const SEARCH_LIMIT_PER_GROUP = 20

interface CommandPaletteProps {
  open: boolean
  onClose: () => void
}

function groupLabelKey(group: CommandGroup): string {
  return `common.command_palette.group.${group}`
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const toggleTheme = useThemeStore(s => s.toggle)

  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [searchCommands, setSearchCommands] = useState<CommandItem[]>([])
  const [recentIds, setRecentIds] = useState<string[]>([])
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  const isZh = i18n.language.startsWith('zh')

  const toggleLanguage = useCallback(() => {
    void i18n.changeLanguage(isZh ? 'en-US' : 'zh-CN')
  }, [i18n, isZh])

  // ── Reset / focus / scroll-lock on open ───────────────────────────────────
  useEffect(() => {
    if (!open) return
    setQuery('')
    setDebouncedQuery('')
    setSelectedIndex(0)
    setRecentIds(readRecent())
    // Focus after mount so the input exists in the DOM.
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.clearTimeout(timer)
      document.body.style.overflow = prevOverflow
    }
  }, [open])

  // ── Debounce the query used for filtering ─────────────────────────────────
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query])

  // ── Lazy-load search sources once per open ────────────────────────────────
  useEffect(() => {
    if (!open) return
    let cancelled = false

    const isZhNow = i18n.language.startsWith('zh')
    const factorLabel = (f: AvailableFactor) => (isZhNow ? f.label_zh || f.name : f.label_en || f.name)

    const factorsPromise = factorsApi.getAvailable()
      .then(res =>
        (res.factors ?? []).map<CommandItem>(f => ({
          id: `factor:${f.name}`,
          group: 'factors',
          label: factorLabel(f),
          keywords: `${f.name} ${f.category}`,
          icon: '🔬',
          run: () => navigate('/factors'),
        })),
      )
      .catch(() => [] as CommandItem[])

    const strategiesPromise = strategiesApi.list()
      .then(res =>
        (res.items ?? []).slice(0, SEARCH_LIMIT_PER_GROUP).map<CommandItem>(s => ({
          id: `strategy:${s.strategy_id}`,
          group: 'strategies',
          label: s.strategy_id,
          keywords: 'strategy 策略',
          icon: '⚙️',
          run: () => navigate('/strategies'),
        })),
      )
      .catch(() => [] as CommandItem[])

    const backtestsPromise = backtestsApi.list({ limit: SEARCH_LIMIT_PER_GROUP })
      .then(res =>
        (res.items ?? []).map<CommandItem>(r => ({
          id: `backtest:${r.run_id}`,
          group: 'backtests',
          label: `${r.strategy_id} · ${r.run_id.slice(0, 10)}`,
          keywords: `${r.run_id} ${r.strategy_id} backtest 回测`,
          icon: '📈',
          run: () => navigate(`/backtests/${r.run_id}`),
        })),
      )
      .catch(() => [] as CommandItem[])

    void Promise.all([factorsPromise, strategiesPromise, backtestsPromise]).then(([f, s, b]) => {
      if (!cancelled) setSearchCommands([...f, ...s, ...b])
    })

    return () => { cancelled = true }
    // i18n.language intentionally read once per open — reopening refreshes it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, navigate])

  // ── Command list (static + search) ────────────────────────────────────────
  const staticCommands = useMemo(
    () => buildStaticCommands(t, navigate, { toggleTheme, toggleLanguage }),
    [t, navigate, toggleTheme, toggleLanguage],
  )
  const allCommands = useMemo(
    () => [...staticCommands, ...searchCommands],
    [staticCommands, searchCommands],
  )

  // ── Filter + recent-first ordering ────────────────────────────────────────
  const filtered = useMemo(() => {
    if (!debouncedQuery.trim()) {
      const byId = new Map(allCommands.map(c => [c.id, c]))
      const recent = recentIds
        .map(id => byId.get(id))
        .filter((c): c is CommandItem => Boolean(c))
        .map(c => ({ ...c, group: 'recent' as const }))
      const recentIdSet = new Set(recentIds)
      const rest = allCommands.filter(c => !recentIdSet.has(c.id))
      return [...recent, ...rest]
    }
    return allCommands.filter(c => commandMatches(c, debouncedQuery))
  }, [allCommands, debouncedQuery, recentIds])

  // Clamp selection when the filtered list shrinks.
  useEffect(() => {
    setSelectedIndex(idx => Math.min(idx, Math.max(filtered.length - 1, 0)))
  }, [filtered.length])

  // Keep the selected row in view.
  useEffect(() => {
    const el = listRef.current?.children[selectedIndex] as HTMLElement | undefined
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedIndex])

  const execute = useCallback((cmd: CommandItem) => {
    pushRecent(cmd.id)
    cmd.run()
    onClose()
  }, [onClose])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSelectedIndex(idx => Math.min(idx + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSelectedIndex(idx => Math.max(idx - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const cmd = filtered[selectedIndex]
      if (cmd) execute(cmd)
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center pt-[12vh] px-4" data-testid="command-palette">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t('common.command_palette.title', '命令面板')}
        className="relative w-full max-w-xl bg-white dark:bg-gray-800 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 overflow-hidden"
      >
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={e => { setQuery(e.target.value); setSelectedIndex(0) }}
          onKeyDown={handleKeyDown}
          placeholder={t('common.command_palette.placeholder', '搜索页面、因子、策略或执行动作…')}
          aria-label={t('common.command_palette.placeholder', '搜索页面、因子、策略或执行动作…')}
          className="w-full px-4 py-3 text-sm bg-transparent border-b border-gray-200 dark:border-gray-700 focus:outline-none text-gray-900 dark:text-gray-100 placeholder:text-gray-400"
        />
        <ul ref={listRef} className="max-h-[50vh] overflow-y-auto py-1" role="listbox">
          {filtered.length === 0 && (
            <li className="px-4 py-6 text-sm text-gray-400 text-center">
              {t('common.command_palette.no_results', '无匹配结果')}
            </li>
          )}
          {filtered.map((cmd, idx) => {
            const prevGroup = idx > 0 ? filtered[idx - 1].group : null
            const showHeader = cmd.group !== prevGroup
            return (
              <li key={cmd.id} role="option" aria-selected={idx === selectedIndex} className="list-none">
                {showHeader && (
                  <div className="px-4 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-gray-400">
                    {t(groupLabelKey(cmd.group))}
                  </div>
                )}
                <div
                  onClick={() => execute(cmd)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                  className={`flex items-center gap-2 px-4 py-2 text-sm cursor-pointer ${
                    idx === selectedIndex
                      ? 'bg-brand-50 dark:bg-brand-900/40 text-brand-700 dark:text-brand-200'
                      : 'text-gray-700 dark:text-gray-200'
                  }`}
                >
                  <span className="w-5 text-center flex-shrink-0">{cmd.icon ?? '·'}</span>
                  <span className="flex-1 truncate">{cmd.label}</span>
                  {cmd.group === 'pages' && cmd.keywords && (
                    <span className="text-xs text-gray-400 font-mono flex-shrink-0">{cmd.keywords}</span>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
        <div className="px-4 py-2 border-t border-gray-200 dark:border-gray-700 text-[11px] text-gray-400 flex gap-3">
          <span>↑↓ {t('common.command_palette.hint_navigate', '选择')}</span>
          <span>⏎ {t('common.command_palette.hint_execute', '执行')}</span>
          <span>Esc {t('common.command_palette.hint_close', '关闭')}</span>
        </div>
      </div>
    </div>
  )
}
