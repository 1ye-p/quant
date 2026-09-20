import { useState, useEffect, Suspense } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { mlApi, backtestsApi, scoringApi, alertsApi, jobsApi } from '@/lib/api'
import { elapsedStr } from '@/lib/utils'
import { Breadcrumb } from '@/components/ui/Breadcrumb'
import { LanguageSwitcher } from '@/components/common/LanguageSwitcher'
import { CommandPalette } from '@/components/common/CommandPalette'
import { useThemeStore } from '@/stores/themeStore'
import { useSidebarStore } from '@/stores/sidebarStore'
import { useWorkflowStore } from '@/stores/workflowStore'
import { WorkflowBar } from '@/components/workflow/WorkflowBar'
import { isOnboarded, markOnboarded } from '@/pages/WelcomePage'

const NAV_ICONS: Record<string, string> = {
  '/factors':    '🔬',
  '/strategies': '⚙️',
  '/ml':         '🧠',
  '/backtests':  '📈',
  '/optimize':   '⚖️',
  '/risk':       '🛡',
  '/scoring':    '🎯',
  '/live':       '📡',
  '/trading':    '💹',
  '/news':       '📰',
  '/datasets':   '🗄️',
  '/knowledge':  '📚',
  '/advisor':    '🤖',
  '/':           '🏠',
  '/alerts':    '🔔',
  '/tasks':     '📋',
  '/pipeline':  '🔄',
}

function useNavGroups() {
  const { t } = useTranslation()
  return [
    {
      label: t('common.nav.research_tools', '研究工具'),
      items: [
        { to: '/factors',    label: t('common.nav.factors') },
        { to: '/strategies', label: t('common.nav.strategies') },
        { to: '/ml',         label: t('common.nav.ml') },
        { to: '/backtests',  label: t('common.nav.backtests') },
        { to: '/optimize',   label: t('common.nav.optimize', '组合优化') },
        { to: '/risk',       label: t('common.nav.risk') },
        { to: '/scoring',    label: t('common.nav.scoring', '截面打分') },
      ],
    },
    {
      label: t('common.nav.data_monitor', '数据 & 监控'),
      items: [
        { to: '/live',     label: t('common.nav.live', '实盘监控') },
        { to: '/trading',  label: t('common.nav.trading') },
        { to: '/news',     label: t('common.nav.news', '消息面') },
        { to: '/datasets', label: t('common.nav.datasets', '数据集') },
        { to: '/data-browser', label: t('common.nav.data_browser', '数据浏览器') },
      ],
    },
    {
      label: t('common.nav.knowledge_ai', '知识 & AI'),
      items: [
        { to: '/knowledge', label: t('common.nav.knowledge', '知识库') },
        { to: '/advisor',   label: t('common.nav.advisor', 'AI 分析助手') },
      ],
    },
    {
      label: t('common.nav.system', '系统'),
      items: [
        { to: '/',         label: t('common.nav.dashboard') },
        { to: '/tasks',    label: t('common.nav.tasks', '任务管理') },
        { to: '/alerts',   label: t('common.nav.alerts', '告警中心') },
        { to: '/pipeline', label: t('common.nav.pipeline', '自动化管道') },
      ],
    },
  ]
}

export function AppLayout() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const navGroups = useNavGroups()
  // One-time onboarding banner: only shown until the user completes or skips
  // the /welcome demo guide (state persisted via localStorage).
  const [showOnboarding, setShowOnboarding] = useState(() => !isOnboarded())
  // Global command palette (Cmd+K / Ctrl+K)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const queryClient = useQueryClient()
  const location = useLocation()
  const { mode, toggle: toggleTheme } = useThemeStore()
  const { collapsed, toggle: toggleCollapsed, mobileOpen, openMobile, closeMobile } = useSidebarStore()
  const { currentWorkflow, currentStep, steps, nextStep, prevStep, reset: resetWorkflow } = useWorkflowStore()
  const isRelevantPage = ['/ml', '/backtests', '/scoring', '/tasks'].includes(location.pathname)
  const pollInterval = isRelevantPage ? 10_000 : 60_000

  const stopTaskMutation = useMutation({
    mutationFn: (jobId: string) => jobsApi.cancel(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['layout'] })
      queryClient.invalidateQueries({ queryKey: ['ml'] })
      queryClient.invalidateQueries({ queryKey: ['backtests'] })
      queryClient.invalidateQueries({ queryKey: ['scoring'] })
    },
  })

  const deleteTaskMutation = useMutation({
    mutationFn: (jobId: string) => jobsApi.delete(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['layout'] })
      queryClient.invalidateQueries({ queryKey: ['ml'] })
      queryClient.invalidateQueries({ queryKey: ['backtests'] })
      queryClient.invalidateQueries({ queryKey: ['scoring'] })
    },
    onError: (e: Error) => toast.error(`删除失败: ${e.message}`),
  })

  // 轮询各模块运行中任务数量
  const { data: mlJobs } = useQuery({
    queryKey: ['layout', 'ml-running'],
    queryFn: () => mlApi.experiments(100),
    refetchInterval: pollInterval,
    select: (d) => d.items?.filter(
      (e: { status: string }) => e.status === 'running' || e.status === 'pending'
    ).length ?? 0,
  })

  const { data: btJobs } = useQuery({
    queryKey: ['layout', 'bt-running'],
    queryFn: () => backtestsApi.list({ limit: 50 }),
    refetchInterval: pollInterval,
    select: (d) => d.items?.filter(
      (r: { status: string }) => r.status === 'running' || r.status === 'pending'
    ).length ?? 0,
  })

  const { data: scoringJobs } = useQuery({
    queryKey: ['layout', 'scoring-running'],
    queryFn: () => scoringApi.listSnapshots(20),
    refetchInterval: pollInterval,
    select: (d) => d.items?.filter(
      (s: { status: string }) => s.status === 'running' || s.status === 'pending'
    ).length ?? 0,
  })

  const runningBadges: Record<string, number> = {}
  if ((mlJobs ?? 0) > 0) runningBadges['/ml'] = mlJobs as number
  if ((btJobs ?? 0) > 0) runningBadges['/backtests'] = btJobs as number
  if ((scoringJobs ?? 0) > 0) runningBadges['/scoring'] = scoringJobs as number

  const [taskDropdownOpen, setTaskDropdownOpen] = useState(false)

  // Detailed running task queries for topbar
  const { data: mlRunning } = useQuery({
    queryKey: ['layout', 'ml-running-details'],
    queryFn: () => mlApi.experiments(50),
    refetchInterval: pollInterval,
    select: (d) => (d.items ?? [])
      .filter((e: { status: string }) => e.status === 'running' || e.status === 'pending')
      .map((e: { run_id: string; status: string; started_at?: number | string; trainer_name?: string }) => ({
        type: 'ML训练',
        id: e.run_id.slice(0, 10),
        fullId: e.run_id,
        status: e.status,
        startedAt: e.started_at,
        detail: e.trainer_name ?? '',
      })),
  })

  const { data: btRunning } = useQuery({
    queryKey: ['layout', 'bt-running-details'],
    queryFn: () => backtestsApi.list({ limit: 50 }),
    refetchInterval: pollInterval,
    select: (d) => (d.items ?? [])
      .filter((r: { status: string }) => r.status === 'running' || r.status === 'pending')
      .map((r: { run_id: string; status: string; started_at?: string; strategy_id?: string }) => ({
        type: '回测',
        id: r.run_id.slice(0, 10),
        fullId: r.run_id,
        status: r.status,
        startedAt: r.started_at,
        detail: r.strategy_id ?? '',
      })),
  })

  const allRunningTasks = [...(mlRunning ?? []), ...(btRunning ?? [])]
  const runningCount = allRunningTasks.length

  useEffect(() => {
    if (!taskDropdownOpen) return
    const handler = () => setTaskDropdownOpen(false)
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [taskDropdownOpen])

  // Global Cmd+K / Ctrl+K shortcut for the command palette
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(o => !o)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const { data: alertUnread } = useQuery({
    queryKey: ['alerts', 'unread-count'],
    queryFn: () => alertsApi.history(true, 1),
    refetchInterval: 60_000,
    select: (d) => d.unread_count,
  })


  return (
    <div className="flex h-screen overflow-hidden">
      {currentWorkflow && currentStep && steps.length > 0 && (
        <WorkflowBar
          steps={steps}
          currentStep={currentStep}
          onPrev={prevStep}
          onNext={nextStep}
          onReset={resetWorkflow}
        />
      )}
      <nav className={`${
        collapsed ? 'w-12' : 'w-56'
      } bg-brand-600 text-gray-100 flex flex-col flex-shrink-0 sticky ${currentWorkflow ? 'top-10' : 'top-0'} h-screen overflow-y-auto transition-[width] duration-200`}>

        {collapsed ? (
          <button
            onClick={toggleCollapsed}
            className="p-3 hover:bg-white/10 text-center text-lg w-full"
            title={t('common.layout.expand_sidebar')}
          >
            ☰
          </button>
        ) : (
          <div className="flex items-center justify-between px-5 py-5">
            <Link to="/" className="text-white font-bold text-lg hover:text-blue-100 transition-colors">
              cQuant
            </Link>
            <button
              onClick={toggleCollapsed}
              className="text-blue-200 hover:text-white text-sm"
              title={t('common.layout.collapse_sidebar')}
            >
              ◀
            </button>
          </div>
        )}

        <div className="flex-1 px-2 pb-4 space-y-4">
          {navGroups.map(group => (
            <div key={group.label}>
              {!collapsed && (
                <div className="px-2 py-1 text-xs font-semibold text-blue-200 uppercase tracking-wider">
                  {group.label}
                </div>
              )}
              <ul className="space-y-0.5">
                {group.items.map(({ to, label }) => (
                  <li key={to}>
                    <NavLink
                      to={to}
                      end={to === '/'}
                      title={collapsed ? label : undefined}
                      className={({ isActive }) =>
                        `flex items-center ${collapsed ? 'justify-center px-2' : 'px-3'} py-2 rounded-lg text-sm transition-colors ${
                          isActive
                            ? 'bg-white/20 text-white font-semibold'
                            : 'text-blue-100 hover:bg-white/10 hover:text-white'
                        }`
                      }
                    >
                      {collapsed ? (
                        <span className="relative text-base">
                          {NAV_ICONS[to] ?? label[0]}
                          {runningBadges[to] ? (
                            <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-blue-300 animate-pulse" />
                          ) : to === '/alerts' && (alertUnread ?? 0) > 0 ? (
                            <span className="absolute -top-1 -right-1 w-3.5 h-3.5 text-[7px] bg-red-500 text-white rounded-full flex items-center justify-center">
                              {(alertUnread ?? 0) > 9 ? '9+' : alertUnread}
                            </span>
                          ) : null}
                        </span>
                      ) : (
                        <>
                          <span className="flex-1">{label}</span>
                          {to === '/alerts' && (alertUnread ?? 0) > 0 ? (
                            <span className="px-1.5 py-0.5 text-xs bg-red-500 text-white rounded-full min-w-[18px] text-center flex-shrink-0">
                              {alertUnread}
                            </span>
                          ) : runningBadges[to] ? (
                            <span className="w-1.5 h-1.5 rounded-full bg-blue-300 animate-pulse flex-shrink-0" />
                          ) : null}
                        </>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </nav>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div
            className="fixed inset-0 bg-black/30"
            onClick={closeMobile}
          />
          <div className="fixed left-0 top-0 bottom-0 w-64 bg-brand-600 shadow-xl p-4 overflow-y-auto">
            <div className="flex justify-between items-center mb-4">
              <Link to="/" className="text-white font-bold text-lg" onClick={closeMobile}>
                cQuant
              </Link>
              <button onClick={closeMobile} className="text-blue-200 hover:text-white">
                ✕
              </button>
            </div>
            {navGroups.map(group => (
              <div key={group.label} className="mb-4">
                <div className="px-2 py-1 text-xs font-semibold text-blue-200 uppercase tracking-wider">
                  {group.label}
                </div>
                <ul className="space-y-0.5">
                  {group.items.map(({ to, label }) => (
                    <li key={to}>
                      <NavLink
                        to={to}
                        end={to === '/'}
                        onClick={closeMobile}
                        className={({ isActive }) =>
                          `flex items-center px-3 py-2 rounded-lg text-sm transition-colors ${
                            isActive
                              ? 'bg-white/20 text-white font-semibold'
                              : 'text-blue-100 hover:bg-white/10 hover:text-white'
                          }`
                        }
                      >
                        {NAV_ICONS[to] ?? label[0]} {label}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Topbar */}
          <header className="h-10 bg-white border-b border-gray-200 flex items-center justify-end px-4 flex-shrink-0 relative z-10">
            <button
              className="md:hidden p-2 mr-1 hover:bg-gray-100 rounded-lg"
              onClick={openMobile}
            >
              ☰
            </button>
            <button onClick={toggleTheme} className="p-2 rounded-lg hover:bg-gray-100 mr-2" title={t('common.layout.toggle_theme')}>
              {mode === 'light' ? '\u{1F319}' : '\u{2600}\u{FE0F}'}
            </button>
            <LanguageSwitcher />
            {runningCount > 0 ? (
              <div className="relative">
                <button
                  onClick={(e) => { e.stopPropagation(); setTaskDropdownOpen(o => !o) }}
                  className="flex items-center gap-1.5 text-sm text-gray-600 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100"
                >
                  <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
                  <span className="font-medium">⚙ {runningCount} {t('common.layout.running_tasks', '个任务运行中')}</span>
                  <span className="text-xs text-gray-400">▾</span>
                </button>
                {taskDropdownOpen && (
                  <div
                    className="absolute right-0 top-full mt-1 w-72 bg-white border border-gray-200 rounded-xl shadow-lg z-50"
                    onClick={e => e.stopPropagation()}
                  >
                    <div className="px-3 py-2 border-b text-xs font-semibold text-gray-500 uppercase">
                      {t('common.layout.running_tasks_title', '进行中的任务')}
                    </div>
                    <ul className="divide-y divide-gray-100 max-h-64 overflow-y-auto">
                      {allRunningTasks.map((task, i) => (
                        <li key={i} className="px-3 py-2 flex items-center justify-between">
                          <div className="flex-1 min-w-0">
                            <span className="text-xs font-medium text-gray-700">{task.type}</span>
                            <span className="ml-2 font-mono text-xs text-gray-400">{task.id}…</span>
                            {task.detail && (
                              <div className="text-xs text-gray-500 truncate max-w-[140px]">{task.detail}</div>
                            )}
                          </div>
                          <div className="text-right flex-shrink-0 ml-2 flex items-center gap-2">
                            <div>
                              <span className="text-xs text-gray-400">
                                {elapsedStr(task.startedAt)}
                              </span>
                              <div className="text-xs text-blue-500 mt-0.5">
                                {task.status}
                              </div>
                            </div>
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                if (confirm('确定要停止此任务吗？')) {
                                  stopTaskMutation.mutate(task.fullId)
                                }
                              }}
                              disabled={stopTaskMutation.isPending}
                              className="p-1 text-red-400 hover:text-red-600 hover:bg-red-50 rounded transition-colors disabled:opacity-50"
                              title="停止任务"
                            >
                              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                              </svg>
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                if (confirm('确定删除此任务记录吗？')) {
                                  deleteTaskMutation.mutate(task.fullId)
                                }
                              }}
                              disabled={deleteTaskMutation.isPending}
                              className="p-1 text-gray-400 hover:text-gray-600 hover:bg-gray-50 rounded transition-colors disabled:opacity-50"
                              title="删除任务"
                            >
                              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                              </svg>
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                    <div className="px-3 py-2 border-t text-xs text-gray-400 text-center">
                      {t('common.layout.auto_refresh', '每10秒自动刷新')}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <span className="text-xs text-gray-400">{t('common.layout.no_running_tasks', '无运行中任务')}</span>
            )}
          </header>

          {/* Main content */}
          <main className={`flex-1 overflow-y-auto p-8 bg-gray-50 ${currentWorkflow ? 'pt-18' : ''}`}>
            {showOnboarding && location.pathname !== '/welcome' && (
              <div
                data-testid="onboarding-banner"
                className="mb-4 flex flex-col sm:flex-row sm:items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3"
              >
                <div className="flex-1">
                  <p className="text-sm font-medium text-gray-900">
                    {t('common.layout.onboarding_banner_title')}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {t('common.layout.onboarding_banner_desc')}
                  </p>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  <button
                    className="btn-primary text-sm"
                    onClick={() => {
                      // Do not persist yet — WelcomePage marks onboarded when
                      // the demo flow completes, so abandoning the guide
                      // re-shows the banner on the next visit.
                      setShowOnboarding(false)
                      navigate('/welcome')
                    }}
                  >
                    {t('common.layout.onboarding_start')}
                  </button>
                  <button
                    className="btn-secondary text-sm"
                    onClick={() => {
                      markOnboarded()
                      setShowOnboarding(false)
                    }}
                  >
                    {t('common.layout.onboarding_skip')}
                  </button>
                </div>
              </div>
            )}
            <Breadcrumb />
            <Suspense fallback={
              <div className="flex items-center justify-center h-64">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand-600" />
              </div>
            }>
              <Outlet />
            </Suspense>
          </main>
        </div>

        <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  )
}
