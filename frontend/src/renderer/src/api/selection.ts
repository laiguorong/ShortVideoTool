import { request, type PageResult } from './client'

/** 间隔配置（与后端 IntervalConfig 对齐）。 */
export interface IntervalConfig {
  type: 'minute' | 'hour' | 'daily'
  value?: number
  time?: string
}

/** 选品拉取任务（响应模型）。 */
export interface ShopPullTask {
  id: string
  task_name: string
  keyword: string
  cities: string[]
  account_id: string
  interval: IntervalConfig
  status: 'enabled' | 'disabled'
  last_run_time: string | null
  next_run_time: string | null
  create_time: string
  update_time: string
  /** v37：正则过滤（任务 #407） */
  shop_name_pattern?: string
  city_pattern?: string
}

/** 任务执行日志。 */
export interface ShopPullLog {
  id: string
  task_id: string
  run_time: string
  pages_done: number
  new_count: number
  update_count: number
  fail_reason: string | null
  create_time: string
}

/** 门店记录（响应模型）。 */
export interface Shop {
  id: string
  poi_id: string
  name: string

  // 地理
  province: string | null
  city: string | null
  district: string | null
  ad_code: string | null
  address: string | null
  lat_gcj02: number | null
  lng_gcj02: number | null

  // 分类
  category: string | null
  category_full: string | null

  // CPS
  is_cps: number

  // 佣金 / 比率
  commission_rate: number | null
  take_rate_min: number | null
  take_rate_max: number | null
  take_rate_avg: number | null

  // 销量 / GMV
  total_sold: number | null
  total_gmv: number | null
  total_commission: number | null

  // 商品
  spu_count: number
  cps_spu_count: number
  delivery_spu_count: number
  spu_type_groupon: number
  spu_type_delivery: number

  // 平台
  platform_name: string | null
  platform_source: number | null

  // TOP SPU
  top_spu_name: string | null
  top_spu_sold: number | null

  // 详情
  detail_fetched: number
  detail_updated_time: string | null

  // 加库标记
  is_added_to_library: number
  added_time: string | null

  // 通用
  create_time: string
  update_time: string
}

/** 门店查询参数。 */
export interface ShopQueryParams {
  keyword?: string
  category?: string
  city?: string
  province?: string
  is_cps_only?: boolean
  is_added_only?: boolean
  spu_count_min?: number
  spu_count_max?: number
  total_gmv_min?: number
  total_gmv_max?: number
  take_rate_min?: number
  take_rate_max?: number
  commission_min?: number
  commission_max?: number
  sort?: string
  page?: number
  page_size?: number
}

export const selectionApi = {
  /** 任务列表（创建/查询/编辑/删除/启停/触发/日志） */
  createTask: (body: {
    task_name: string
    keyword: string
    cities: string[]
    account_id: string
    interval: IntervalConfig
    shop_name_pattern?: string
    city_pattern?: string
  }) => request<ShopPullTask>('/selection/tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  }),

  listTasks: (page = 1, pageSize = 20) =>
    request<PageResult<ShopPullTask>>(`/selection/tasks?page=${page}&page_size=${pageSize}`),

  getTask: (id: string) => request<ShopPullTask>(`/selection/tasks/${id}`),

  updateTask: (id: string, body: Partial<{
    task_name: string
    keyword: string
    cities: string[]
    account_id: string
    interval: IntervalConfig
    shop_name_pattern: string
    city_pattern: string
  }>) => request<ShopPullTask>(`/selection/tasks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  }),

  deleteTask: (id: string) =>
    request<{ ok: boolean }>(`/selection/tasks/${id}`, { method: 'DELETE' }),

  toggleTask: (id: string, enabled: boolean) =>
    request<ShopPullTask>(`/selection/tasks/${id}/toggle`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  runTask: (id: string) =>
    request<{ ok: boolean; bg_task_id: string }>(`/selection/tasks/${id}/run`, {
      method: 'POST',
    }),

  taskLogs: (id: string, page = 1, pageSize = 50) =>
    request<PageResult<ShopPullLog>>(
      `/selection/tasks/${id}/logs?page=${page}&page_size=${pageSize}`,
    ),

  /** 全部门店拉取执行记录（任务 #407：任务执行记录页签使用） */
  allLogs: (page = 1, pageSize = 50) =>
    request<PageResult<ShopPullLog & { task_name: string | null }>>(
      `/selection/logs?page=${page}&page_size=${pageSize}`,
    ),

  /** 批量删除门店拉取记录（任务 #408） */
  batchDeleteLogs: (ids: string[]) =>
    request<{ deleted: number; missing: string[] }>(
      '/selection/logs/batch-delete',
      { method: 'POST', body: JSON.stringify({ ids }) },
    ),

  /** 门店查询 */
  listShops: (params: ShopQueryParams = {}) => {
    // 仅过滤 null/undefined/空串；false 是三态筛选的"仅否"语义，必须保留
    // （之前一并过滤掉 false，导致"仅无佣金/仅未加库"形同虚设）
    const filtered = Object.entries(params).filter(
      ([, v]) => v !== '' && v !== undefined && v !== null,
    )
    const q = new URLSearchParams(
      filtered.map(([k, v]) => `${k}=${v}`).join('&'),
    )
    return request<PageResult<Shop>>(`/selection/shops?${q}`)
  },

  /** 维度聚合（下拉数据源） */
  fetchCities: () => request<{ cities: string[] }>('/selection/cities'),
  fetchProvinces: () => request<{ provinces: string[] }>('/selection/provinces'),
  fetchCategories: () => request<{ categories: string[] }>('/selection/categories'),

  /** 切换门店加库标记（enabled=true 加库，false 取消） */
  toggleAdded: (shopId: string, enabled: boolean) =>
    request<{ shop_id: string; is_added_to_library: number; added_time: string | null }>(
      `/selection/shops/${shopId}/toggle-added?enabled=${enabled}`,
      { method: 'POST' },
    ),
}