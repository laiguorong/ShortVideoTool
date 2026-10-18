import { request, type PageResult, type ApiResp } from './client'

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

  // #publish-count：累计发布成功次数（视频发布成功后 +1）
  publish_count: number

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
  createTask: async (body: {
    task_name: string
    keyword: string
    cities: string[]
    account_id: string
    interval: IntervalConfig
    shop_name_pattern?: string
    city_pattern?: string
  }): Promise<ShopPullTask> => {
    const r = await request<ApiResp<ShopPullTask>>('/selection/tasks', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return r.data!
  },

  listTasks: async (page = 1, pageSize = 20): Promise<PageResult<ShopPullTask>> => {
    const r = await request<ApiResp<PageResult<ShopPullTask>>>(
      `/selection/tasks?page=${page}&page_size=${pageSize}`,
    )
    return r.data!
  },

  getTask: async (id: string): Promise<ShopPullTask> => {
    const r = await request<ApiResp<ShopPullTask>>(`/selection/tasks/${id}`)
    return r.data!
  },

  updateTask: async (id: string, body: Partial<{
    task_name: string
    keyword: string
    cities: string[]
    account_id: string
    interval: IntervalConfig
    shop_name_pattern: string
    city_pattern: string
  }>): Promise<ShopPullTask> => {
    const r = await request<ApiResp<ShopPullTask>>(`/selection/tasks/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
    return r.data!
  },

  deleteTask: async (id: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/selection/tasks/${id}`, { method: 'DELETE' })
    return r.data!
  },

  toggleTask: async (id: string, enabled: boolean): Promise<ShopPullTask> => {
    const r = await request<ApiResp<ShopPullTask>>(`/selection/tasks/${id}/toggle`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    })
    return r.data!
  },

  runTask: async (id: string): Promise<{ ok: boolean; bg_task_id: string }> => {
    const r = await request<ApiResp<{ ok: boolean; bg_task_id: string }>>(
      `/selection/tasks/${id}/run`, { method: 'POST' },
    )
    return r.data!
  },

  taskLogs: async (id: string, page = 1, pageSize = 50): Promise<PageResult<ShopPullLog>> => {
    const r = await request<ApiResp<PageResult<ShopPullLog>>>(
      `/selection/tasks/${id}/logs?page=${page}&page_size=${pageSize}`,
    )
    return r.data!
  },

  /** 全部门店拉取执行记录（任务 #407：任务执行记录页签使用） */
  allLogs: async (page = 1, pageSize = 50): Promise<PageResult<ShopPullLog & { task_name: string | null }>> => {
    const r = await request<ApiResp<PageResult<ShopPullLog & { task_name: string | null }>>>(
      `/selection/logs?page=${page}&page_size=${pageSize}`,
    )
    return r.data!
  },

  /** 批量删除门店拉取记录（任务 #408） */
  batchDeleteLogs: async (ids: string[]): Promise<{ deleted: number; missing: string[] }> => {
    const r = await request<ApiResp<{ deleted: number; missing: string[] }>>(
      '/selection/logs/batch-delete',
      { method: 'POST', body: JSON.stringify({ ids }) },
    )
    return r.data!
  },

  /** 门店查询 */
  listShops: async (params: ShopQueryParams = {}): Promise<PageResult<Shop>> => {
    // 仅过滤 null/undefined/空串；false 是三态筛选的"仅否"语义，必须保留
    // （之前一并过滤掉 false，导致"仅无佣金/仅未加库"形同虚设）
    const filtered = Object.entries(params).filter(
      ([, v]) => v !== '' && v !== undefined && v !== null,
    )
    const q = new URLSearchParams(
      filtered.map(([k, v]) => `${k}=${v}`).join('&'),
    )
    const r = await request<ApiResp<PageResult<Shop>>>(`/selection/shops?${q}`)
    return r.data!
  },

  /** 维度聚合（下拉数据源） */
  fetchCities: async (): Promise<{ cities: string[] }> => {
    const r = await request<ApiResp<{ cities: string[] }>>('/selection/cities')
    return r.data!
  },
  fetchProvinces: async (): Promise<{ provinces: string[] }> => {
    const r = await request<ApiResp<{ provinces: string[] }>>('/selection/provinces')
    return r.data!
  },
  fetchCategories: async (): Promise<{ categories: string[] }> => {
    const r = await request<ApiResp<{ categories: string[] }>>('/selection/categories')
    return r.data!
  },

  /** 切换门店加库标记（enabled=true 加库，false 取消） */
  toggleAdded: async (shopId: string, enabled: boolean): Promise<{
    shop_id: string
    is_added_to_library: number
    added_time: string | null
  }> => {
    const r = await request<ApiResp<{
      shop_id: string
      is_added_to_library: number
      added_time: string | null
    }>>(`/selection/shops/${shopId}/toggle-added?enabled=${enabled}`, { method: 'POST' })
    return r.data!
  },
}
