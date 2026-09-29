import { useEffect, useState } from 'react'
import { useAppStore, type Page } from '@/store/useAppStore'
import { settingApi, taskApi } from '@/api/setting'
import { ToastContainer, toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { DashboardPage } from '@/pages/DashboardPage'
import { AccountsPage } from '@/pages/AccountsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { SelectionPage } from '@/pages/SelectionPage'
import { MaterialsPage } from '@/pages/MaterialsPage'
import { CreationPage } from '@/pages/CreationPage'
import { PublishPage } from '@/pages/PublishPage'
import { RecordsPage } from '@/pages/RecordsPage'
import { TasksPage } from '@/pages/TasksPage'
import { RiskNoticeDialog } from '@/components/shared/RiskNotice'

/** 导航配置（界面交互设计 9.1.1） */
const NAV_ITEMS: { page: Page; label: string; icon: string }[] = [
  { page: 'dashboard', label: '工作台', icon: '🏠' },
  { page: 'accounts', label: '账号', icon: '👤' },
  { page: 'selection', label: '选品', icon: '🛍' },
  { page: 'materials', label: '素材', icon: '🎬' },
  { page: 'creation', label: '创作', icon: '✂️' },
  { page: 'publish', label: '发布', icon: '🚀' },
  { page: 'records', label: '记录', icon: '📋' },
  { page: 'tasks', label: '任务', icon: '⏳' },
  { page: 'settings', label: '设置', icon: '⚙️' },
]

/** 页面组件映射（未生成页面渲染占位） */
function PageContent({ page }: { page: Page }) {
  switch (page) {
    case 'dashboard':
      return <DashboardPage />
    case 'accounts':
      return <AccountsPage />
    case 'settings':
      return <SettingsPage />
    case 'selection':
      return <SelectionPage />
    case 'materials':
      return <MaterialsPage />
    case 'creation':
      return <CreationPage />
    case 'publish':
      return <PublishPage />
    case 'tasks':
      return <TasksPage />
    case 'records':
      return <RecordsPage />
  }
}

/** 占位页面（M3+ 逐里程碑替换） */
export function PlaceholderPage({ title }: { title: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
      <div className="text-4xl">🚧</div>
      <div className="text-sm">{title} · 开发中（后续里程碑交付）</div>
    </div>
  )
}

export default function App() {
  const { page, setPage, sidebarCollapsed, toggleSidebar, backendReady, setBackendReady,
          unreadCount, taskSummary, setTaskSummary, setNotifications, riskConfirmed, setRiskConfirmed } =
    useAppStore()
  const [healthLoading, setHealthLoading] = useState(true)

  // 后端就绪轮询（1 秒重试，参考 VideoMatrix App 模式）
  useEffect(() => {
    let timer: number | undefined
    let failCount = 0
    const poll = async () => {
      try {
        await settingApi.riskConfirmed() // 任意接口即可探活
        setBackendReady(true)
        setHealthLoading(false)
      } catch (e) {
        // #112：原 catch {} 静默，后端真挂时无限重试无任何线索；
        // dev 输出错误便于排查；连续 10 次失败 toast 提示用户
        if (import.meta.env.DEV) console.warn('[App] 后端就绪轮询失败:', e)
        failCount += 1
        if (failCount === 10) {
          toast('后端响应异常，请重启程序', 'error')
        }
        timer = window.setTimeout(poll, 1000)
      }
    }
    poll()
    return () => window.clearTimeout(timer)
  }, [setBackendReady])

  // 就绪后：轮询任务概览（5s）+ 通知未读数（30s）
  useEffect(() => {
    if (!backendReady) return
    const loadTasks = () => taskApi.summary().then(setTaskSummary).catch((e) => console.warn('[App] 请求失败:', e))
    const loadNotices = () =>
      settingApi
        .notifications()
        .then((r) => setNotifications(r.list, r.unread_count))
        .catch((e) => console.warn('[App] 请求失败:', e))
    loadTasks()
    loadNotices()
    const t1 = window.setInterval(loadTasks, 5000)
    const t2 = window.setInterval(loadNotices, 30000)
    return () => {
      window.clearInterval(t1)
      window.clearInterval(t2)
    }
  }, [backendReady, setTaskSummary, setNotifications])

  // 首次启动风险告知
  useEffect(() => {
    if (!backendReady) return
    settingApi
      .riskConfirmed()
      .then((r) => setRiskConfirmed(r.confirmed))
      .catch((e) => console.warn('[App] 请求失败:', e))
  }, [backendReady, setRiskConfirmed])

  if (healthLoading) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-border border-t-accent" />
        <div className="text-sm text-muted-foreground">正在启动后端服务…</div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏（9.1.1）：产品名 + 通知铃铛 */}
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background-elev px-4">
        <div className="flex items-center gap-2 font-semibold">短视频工具</div>
        <div className="flex items-center gap-3">
          <button
            className="relative rounded p-1.5 hover:bg-muted"
            title="通知中心"
            onClick={() => setPage('settings')}
          >
            🔔
            {unreadCount > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] text-white">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 侧边栏（可折叠） */}
        <nav
          className={cn(
            'flex shrink-0 flex-col border-r border-border bg-background-elev transition-all',
            sidebarCollapsed ? 'w-16' : 'w-48',
          )}
        >
          {NAV_ITEMS.map((item) => (
            <button
              key={item.page}
              onClick={() => setPage(item.page)}
              className={cn(
                'flex items-center gap-2.5 px-4 py-2.5 text-sm transition-colors',
                page === item.page
                  ? 'border-r-2 border-accent bg-blue-50 font-medium text-accent'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
              title={item.label}
            >
              <span className="text-base">{item.icon}</span>
              {!sidebarCollapsed && <span>{item.label}</span>}
            </button>
          ))}
          <div className="mt-auto p-2">
            <button
              onClick={toggleSidebar}
              className="w-full rounded p-2 text-center text-muted-foreground hover:bg-muted"
              title="折叠侧边栏"
            >
              {sidebarCollapsed ? '»' : '«'}
            </button>
          </div>
        </nav>

        {/* 内容区 */}
        <main className="min-w-0 flex-1 overflow-auto bg-background">
          <PageContent page={page} />
        </main>
      </div>

      {/* 底部状态栏（任务队列展示优化 PR4 #57）：任务运行中：[类型] {name} - {progress}
          类型标签放最前，便于多任务并行时一眼区分（素材拉取 vs 门店拉取 vs 视频发布…） */}
      <footer className="flex h-7 shrink-0 items-center justify-between border-t border-border bg-background-elev px-3 text-xs text-muted-foreground">
        <div className="truncate">
          {taskSummary && taskSummary.running_total > 0
            ? `● 任务运行中：${
                taskSummary.running_items
                  .map((i) => `[${i.type_label}] ${i.name} - ${i.progress || '初始化中'}`)
                  .join(' · ') || ''
              }`
            : '● 空闲'}
        </div>
        <div>v1.0.0</div>
      </footer>

      <ToastContainer />
      <RiskNoticeDialog open={!riskConfirmed} onConfirm={() => setRiskConfirmed(true)} />
    </div>
  )
}
