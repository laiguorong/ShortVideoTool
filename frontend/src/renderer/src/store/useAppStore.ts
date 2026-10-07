import { create } from 'zustand'

/** 页面标识（无 react-router，字面量联合切页，界面交互设计 9.1.1） */
export type Page =
  | 'dashboard'
  | 'accounts'
  | 'selection'
  | 'materials'
  | 'creation'
  | 'publish'
  | 'tasks'
  | 'records'
  | 'settings'

/** 通知条目 */
export interface NotificationItem {
  id: string
  level: 'info' | 'warn' | 'error'
  category: string
  title: string
  content: string | null
  is_read: number
  handled: number
  action: string | null
  create_time: string
}

/** 后台任务条目（底部状态栏/任务面板） */
export interface TaskItem {
  task_id: string
  task_type: string
  type_label: string
  name: string
  status: string
  progress: string
  message: string
}

/** 任务概览中的运行中条目（summary.running_items 元素）。 */
export interface TaskSummaryItem {
  task_id: string
  type: string
  type_label: string
  name: string
  status: string
  progress: string
  message: string
}

/** #P1-6：任务概览整体结构（统一给 setTaskSummary 与 state 使用，避免字段增改两处同步） */
export interface TaskSummary {
  running_total: number
  by_type: Record<string, number>
  running_items: TaskSummaryItem[]
}

/** 全局应用状态（单 store，模块前缀分字段） */
interface AppState {
  /** 当前页面 */
  page: Page
  setPage: (page: Page) => void

  /** 侧边栏折叠 */
  sidebarCollapsed: boolean
  toggleSidebar: () => void

  /** 后端就绪（health 轮询） */
  backendReady: boolean
  setBackendReady: (ready: boolean) => void

  /** 通知中心 */
  notifications: NotificationItem[]
  unreadCount: number
  setNotifications: (items: NotificationItem[], unread: number) => void

  /** 后台任务概览（底部状态栏 + TasksPage 顶部 RunningTasksBanner） */
  taskSummary: TaskSummary | null
  setTaskSummary: (s: TaskSummary) => void

  /** 任务面板抽屉开关 */
  taskDrawerOpen: boolean
  setTaskDrawerOpen: (open: boolean) => void

  /** 首次启动风险告知 */
  riskConfirmed: boolean
  setRiskConfirmed: (v: boolean) => void

  /** 启动检查是否完成（7 步全 ok 后切 true，进入主界面） */
  startupComplete: boolean
  setStartupComplete: (v: boolean) => void
}

export const useAppStore = create<AppState>()((set) => ({
  page: 'dashboard',
  setPage: (page) => set({ page }),

  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  backendReady: false,
  setBackendReady: (backendReady) => set({ backendReady }),

  notifications: [],
  unreadCount: 0,
  setNotifications: (notifications, unreadCount) => set({ notifications, unreadCount }),

  taskSummary: null,
  setTaskSummary: (taskSummary) => set({ taskSummary }),

  taskDrawerOpen: false,
  setTaskDrawerOpen: (taskDrawerOpen) => set({ taskDrawerOpen }),

  riskConfirmed: true,
  setRiskConfirmed: (riskConfirmed) => set({ riskConfirmed }),

  startupComplete: false,
  setStartupComplete: (startupComplete) => set({ startupComplete }),
}))
