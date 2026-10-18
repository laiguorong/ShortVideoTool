import { request, type PageResult, type ApiResp } from './client'
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
  get: async (): Promise<AppSettings> => {
    const r = await request<ApiResp<AppSettings>>('/settings')
    return r.data!
  },

  /** 修改配置（热加载） */
  update: async (updates: Partial<AppSettings>): Promise<AppSettings> => {
    const r = await request<ApiResp<AppSettings>>(
      '/settings', { method: 'PUT', body: JSON.stringify(updates) },
    )
    return r.data!
  },

  /** 目录健康检查 */
  healthCheck: async (): Promise<HealthCheckResult> => {
    const r = await request<ApiResp<HealthCheckResult>>('/settings/health-check')
    return r.data!
  },

  /** 清理 temp/cache/log */
  cleanup: async (): Promise<{
    deleted_files: number
    /** #511：被占用/权限失败跳过数（如 loguru 正在写今天的日志） */
    failed_files: number
    freed_mb: number
  }> => {
    const r = await request<ApiResp<{
      deleted_files: number
      failed_files: number
      freed_mb: number
    }>>('/settings/cleanup-temp-cache', { method: 'POST' })
    return r.data!
  },

  /** 数据目录信息 */
  dataDir: async (): Promise<{ data_dir: string; sub_dirs: string[] }> => {
    const r = await request<ApiResp<{ data_dir: string; sub_dirs: string[] }>>('/settings/data-dir')
    return r.data!
  },

  /** 人脸证据目录（任务 #129 复盘修复：人脸检测命中帧存此目录；temp/cache 子目录） */
  faceEvidenceDir: async (): Promise<{ face_evidence_dir: string }> => {
    const r = await request<ApiResp<{ face_evidence_dir: string }>>('/settings/face-evidence-dir')
    return r.data!
  },

  /** 备份 db+config */
  backup: async (body: { target_path: string; include_material?: boolean; include_finished?: boolean }): Promise<{ file: string; size_mb: number }> => {
    const r = await request<ApiResp<{ file: string; size_mb: number }>>(
      '/settings/backup', { method: 'POST', body: JSON.stringify(body) },
    )
    return r.data!
  },

  /** 风险告知确认状态 */
  riskConfirmed: async (): Promise<{ confirmed: boolean }> => {
    const r = await request<ApiResp<{ confirmed: boolean }>>('/settings/risk-confirmed')
    return r.data!
  },

  /** 确认风险告知 */
  riskConfirm: async (confirmed: boolean): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      '/settings/risk-confirm', { method: 'POST', body: JSON.stringify({ confirmed }) },
    )
    return r.data!
  },

  /** 通知列表 */
  notifications: async (onlyUnread = false): Promise<PageResult<NotificationItem> & { unread_count: number }> => {
    const r = await request<ApiResp<PageResult<NotificationItem> & { unread_count: number }>>(
      `/settings/notifications?only_unread=${onlyUnread}`,
    )
    return r.data!
  },

  /** 标记已读 */
  markRead: async (notificationId?: string, allRead = false): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/settings/notifications/read?notification_id=${notificationId ?? ''}&all_read=${allRead}`,
      { method: 'POST' },
    )
    return r.data!
  },
}

export const taskApi = {
  /** 任务列表 */
  list: async (runningOnly = false): Promise<TaskItem[]> => {
    const r = await request<ApiResp<TaskItem[]>>(`/tasks?running_only=${runningOnly}`)
    return r.data!
  },

  /** 任务概览（状态栏 + TasksPage 顶部 RunningTasksBanner 数据源） */
  summary: async (): Promise<{
    running_total: number
    by_type: Record<string, number>
    running_items: Array<TaskSummaryItem>
  }> => {
    const r = await request<ApiResp<{
      running_total: number
      by_type: Record<string, number>
      running_items: Array<TaskSummaryItem>
    }>>('/tasks/summary')
    return r.data!
  },

  /** 取消任务 */
  cancel: async (taskId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/tasks/${taskId}/cancel`, { method: 'POST' },
    )
    return r.data!
  },
}
