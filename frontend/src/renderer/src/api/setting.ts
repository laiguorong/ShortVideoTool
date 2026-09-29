import { request, type PageResult } from './client'
import type { NotificationItem, TaskItem, TaskSummaryItem } from '@/store/useAppStore'

/** 全局配置（后端 settings.json） */
export interface AppSettings {
  account_check_hours: number
  log_retention_days: number
  temp_retention_days: number
  /** v22 阶梯式翻页：第 1 轮最大页数（线性递减，10 兜底；系统设置可调） */
  pull_base_pages?: number
  /** #410 调试开关：是否显示浏览器窗口（默认 false 无头；登录窗固定显示不受此开关影响） */
  browser_show_window?: boolean
  [key: string]: unknown
}

/** 目录健康检查结果 */
export interface HealthCheckResult {
  healthy: boolean
  items: { dir: string; exists: boolean; writable: boolean }[]
  free_gb: number
  warnings: string[]
}

export const settingApi = {
  /** 读取配置 */
  get: () => request<AppSettings>('/settings'),

  /** 修改配置（热加载） */
  update: (updates: Partial<AppSettings>) =>
    request<AppSettings>('/settings', { method: 'PUT', body: JSON.stringify(updates) }),

  /** 目录健康检查 */
  healthCheck: () => request<HealthCheckResult>('/settings/health-check'),

  /** 清理 temp/cache/log */
  cleanup: () => request<{
    deleted_files: number
    /** #511：被占用/权限失败跳过数（如 loguru 正在写今天的日志） */
    failed_files: number
    freed_mb: number
  }>('/settings/cleanup-temp-cache', {
    method: 'POST',
  }),

  /** 数据目录信息 */
  dataDir: () => request<{ data_dir: string; sub_dirs: string[] }>('/settings/data-dir'),

  /** 人脸证据目录（任务 #129 复盘修复：人脸检测命中帧存此目录；temp/cache 子目录） */
  faceEvidenceDir: () => request<{ face_evidence_dir: string }>('/settings/face-evidence-dir'),

  /** 备份 db+config */
  backup: (body: { target_path: string; include_material?: boolean; include_finished?: boolean }) =>
    request<{ file: string; size_mb: number }>('/settings/backup', { method: 'POST', body: JSON.stringify(body) }),

  /** 风险告知确认状态 */
  riskConfirmed: () => request<{ confirmed: boolean }>('/settings/risk-confirmed'),

  /** 确认风险告知 */
  riskConfirm: (confirmed: boolean) =>
    request<{ ok: boolean }>('/settings/risk-confirm', { method: 'POST', body: JSON.stringify({ confirmed }) }),

  /** 通知列表 */
  notifications: (onlyUnread = false) =>
    request<PageResult<NotificationItem> & { unread_count: number }>(
      `/settings/notifications?only_unread=${onlyUnread}`,
    ),

  /** 标记已读 */
  markRead: (notificationId?: string, allRead = false) =>
    request<{ ok: boolean }>(
      `/settings/notifications/read?notification_id=${notificationId ?? ''}&all_read=${allRead}`,
      { method: 'POST' },
    ),
}

export const taskApi = {
  /** 任务列表 */
  list: (runningOnly = false) => request<TaskItem[]>(`/tasks?running_only=${runningOnly}`),

  /** 任务概览（状态栏 + TasksPage 顶部 RunningTasksBanner 数据源） */
  summary: () =>
    request<{
      running_total: number
      by_type: Record<string, number>
      running_items: Array<TaskSummaryItem>
    }>('/tasks/summary'),

  /** 取消任务 */
  cancel: (taskId: string) =>
    request<{ ok: boolean }>(`/tasks/${taskId}/cancel`, { method: 'POST' }),
}
