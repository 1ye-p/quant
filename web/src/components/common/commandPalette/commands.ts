/**
 * Command palette — command sources (data layer).
 *
 * Kept separate from CommandPalette.tsx (UI layer) so filtering,
 * recency tracking and command construction can be unit-tested
 * without rendering.
 */

import type { TFunction } from 'i18next'
import type { NavigateFunction } from 'react-router-dom'

export type CommandGroup =
  | 'recent'
  | 'pages'
  | 'factors'
  | 'strategies'
  | 'backtests'
  | 'actions'

export interface CommandItem {
  /** Stable unique id, also used as the recent-usage localStorage key. */
  id: string
  group: CommandGroup
  label: string
  /** Extra searchable text (path, category, identifier). */
  keywords?: string
  icon?: string
  run: () => void
}

// ── Fuzzy matching (subsequence, case-insensitive) ──────────────────────────

/** True when every char of `query` appears in `text` in order. */
export function fuzzyMatch(text: string, query: string): boolean {
  const t = text.toLowerCase()
  const q = query.toLowerCase().trim()
  if (!q) return true
  let i = 0
  for (const ch of q) {
    i = t.indexOf(ch, i)
    if (i === -1) return false
    i += 1
  }
  return true
}

export function commandMatches(cmd: CommandItem, query: string): boolean {
  if (!query.trim()) return true
  return (
    fuzzyMatch(cmd.label, query) ||
    (cmd.keywords ? fuzzyMatch(cmd.keywords, query) : false)
  )
}

// ── Recent usage (localStorage, degrade silently) ───────────────────────────

export const RECENT_KEY = 'cquant_cmdk_recent'
const RECENT_LIMIT = 5

export function readRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function pushRecent(id: string): void {
  try {
    const next = [id, ...readRecent().filter(x => x !== id)].slice(0, RECENT_LIMIT)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // localStorage unavailable — recency simply not persisted
  }
}

// ── Static commands: 21 top-level pages + actions ───────────────────────────

interface PageDef {
  path: string
  /** i18n key (existing nav key when available). */
  labelKey: string
  fallback: string
  icon?: string
}

// 21 top-level pages from src/router.tsx (no :id tabs, no share/:shareId, no 404)
const PAGES: PageDef[] = [
  { path: '/',                            labelKey: 'common.nav.dashboard',           fallback: '总览',         icon: '🏠' },
  { path: '/welcome',                     labelKey: 'common.command_palette.page.welcome', fallback: '新手引导' },
  { path: '/factors',                     labelKey: 'common.nav.factors',             fallback: '因子研究',     icon: '🔬' },
  { path: '/strategies',                  labelKey: 'common.nav.strategies',          fallback: '策略配置',     icon: '⚙️' },
  { path: '/ml',                          labelKey: 'common.nav.ml',                  fallback: '机器学习',     icon: '🧠' },
  { path: '/backtests',                   labelKey: 'common.nav.backtests',           fallback: '回测评估',     icon: '📈' },
  { path: '/backtests/compare',           labelKey: 'common.command_palette.page.backtests_compare', fallback: '回测对比' },
  { path: '/live',                        labelKey: 'common.nav.live',                fallback: '实盘监控',     icon: '📡' },
  { path: '/trading',                     labelKey: 'common.nav.trading',             fallback: '交易中心',     icon: '💹' },
  { path: '/news',                        labelKey: 'common.nav.news',                fallback: '消息面',       icon: '📰' },
  { path: '/optimize',                    labelKey: 'common.nav.optimize',            fallback: '组合优化',     icon: '⚖️' },
  { path: '/risk',                        labelKey: 'common.nav.risk',                fallback: '风控管理',     icon: '🛡' },
  { path: '/scoring',                     labelKey: 'common.nav.scoring',             fallback: '截面打分',     icon: '🎯' },
  { path: '/datasets/external-indicators', labelKey: 'common.command_palette.page.external_indicators', fallback: '外部指标导入' },
  { path: '/datasets',                    labelKey: 'common.nav.datasets',            fallback: '数据集',       icon: '🗄️' },
  { path: '/data-browser',                labelKey: 'common.nav.data_browser',        fallback: '数据浏览器' },
  { path: '/knowledge',                   labelKey: 'common.nav.knowledge',           fallback: '知识库',       icon: '📚' },
  { path: '/advisor',                     labelKey: 'common.nav.advisor',             fallback: 'AI 分析助手',  icon: '🤖' },
  { path: '/alerts',                      labelKey: 'common.nav.alerts',              fallback: '告警中心',     icon: '🔔' },
  { path: '/tasks',                       labelKey: 'common.nav.tasks',               fallback: '任务管理',     icon: '📋' },
  { path: '/pipeline',                    labelKey: 'common.nav.pipeline',            fallback: '自动化管道',   icon: '🔄' },
]

export interface PaletteActions {
  toggleTheme: () => void
  toggleLanguage: () => void
}

/** Build the always-available commands: page navigation + quick actions. */
export function buildStaticCommands(
  t: TFunction,
  navigate: NavigateFunction,
  actions: PaletteActions,
): CommandItem[] {
  const pageCommands: CommandItem[] = PAGES.map(({ path, labelKey, fallback, icon }) => ({
    id: `page:${path}`,
    group: 'pages' as const,
    label: t(labelKey, fallback),
    keywords: path,
    icon,
    run: () => navigate(path),
  }))

  const actionCommands: CommandItem[] = [
    {
      id: 'action:new-backtest',
      group: 'actions',
      label: t('common.command_palette.action.new_backtest', '新建回测'),
      keywords: '/backtests new create',
      icon: '➕',
      run: () => navigate('/backtests'),
    },
    {
      id: 'action:import-indicators',
      group: 'actions',
      label: t('common.command_palette.action.import_indicators', '导入外部指标'),
      keywords: '/datasets/external-indicators import',
      icon: '📥',
      run: () => navigate('/datasets/external-indicators'),
    },
    {
      id: 'action:toggle-language',
      group: 'actions',
      label: t('common.command_palette.action.toggle_language', '切换语言 / Switch Language'),
      keywords: 'language i18n zh en 中英 switch',
      icon: '🌐',
      run: actions.toggleLanguage,
    },
    {
      id: 'action:toggle-theme',
      group: 'actions',
      label: t('common.command_palette.action.toggle_theme', '切换主题'),
      keywords: 'theme dark light 主题 深色 浅色 toggle',
      icon: '🌓',
      run: actions.toggleTheme,
    },
  ]

  return [...pageCommands, ...actionCommands]
}
