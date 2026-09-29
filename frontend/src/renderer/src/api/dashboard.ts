import { request } from './client'

/** 工作台聚合（#500 数据中心移除后去掉 today 速览） */
export interface DashboardSummary {
  cards: {
    account_count: number
    account_invalid: number
    shop_total: number
    shop_new_today: number
    material_total: number
    material_new_today: number
    video_idle: number
    video_occupied: number
  }
  todo: {
    invalid_accounts: { id: string; remark: string | null; nickname: string | null }[]
    running_publish_tasks: { id: string; task_name: string; start_time: string }[]
  }
}

export const dashboardApi = {
  /** 工作台聚合 */
  dashboard: () => request<DashboardSummary>('/dashboard'),
}
