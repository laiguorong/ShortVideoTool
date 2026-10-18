import { request, type PageResult, type ApiResp } from './client'

/** 发布任务（#413 含 end_time / 各种时间窗配置；#publish-contract 出参 *_json 已解析为结构化对象） */
export interface PublishTask {
  id: string
  task_name: string
  start_time: string
  end_time?: string | null
  interval_minutes: number
  // #calc_mode：计算模式字段
  schedule_mode?: 'fixed' | 'balanced'
  fixed_interval_min?: number
  balanced_step_min?: number
  daily_limit_mode?: 'global' | 'per_account'
  daily_limit_global?: number | null
  declaration?: string | null
  allow_download?: 0 | 1
  status: 'draft' | 'running' | 'paused' | 'finished' | 'cancelled'
  // 派生显示状态：finished 含失败 / cancelled 含成功 → 'partial'，徽标变黄色
  display_status?: 'draft' | 'running' | 'paused' | 'finished' | 'cancelled' | 'partial'
  success_count: number
  fail_count: number
  success_count_real: number
  fail_count_real: number
  item_total: number
  create_time: string
  // 首次进入 running 的时刻；进度列用时 = now - started_at
  started_at?: string | null
  // #publish-contract 出参：原 *_json 字符串列已解析为结构化对象（前端不再 JSON.parse）
  // 注：旧 account_ids / project_ids / projects_payload 已删（前端改用 account_snapshots / project_snapshots）
  // #418：视频目录模式字段（getTaskDetail 返回；编辑草稿时回填用）
  project_source?: 'project' | 'video_dir'
  video_dirs?: VideoDirEntry[]
  manual_shops?: Record<string, ShopDraftEntry[]>
  manual_intros?: Record<string, string[]>
  daily_limit_per_account?: DailyLimitEntry[]
  // 新形态快照（详情/编辑回填用，与首建 buildDraftPayload 一致）
  account_snapshots?: AccountDraftSnapshot[]
  project_snapshots?: ProjectDraftSnapshot[]
}

/** 发布明细（#413 含 intro/topics/declaration） */
export interface PublishItem {
  id: string
  task_id: string
  plan_time: string
  account_id: string
  account_nickname: string | null
  account_remark: string | null
  project_id: string
  video_id: string
  shop_id: string
  shop_name: string | null
  intro_id?: string | null
  intro_snapshot?: string | null
  topics_snapshot?: string | null
  declaration?: string | null
  allow_download?: number | null
  status: 'waiting' | 'publishing' | 'suspended' | 'success' | 'failed' | 'cancelled'
  fail_reason: string | null
  retry_count: number
  video_path?: string | null
  video_title?: string | null
  video_combination_key?: string | null
}

/** 发布记录（#413 含 intro/topics/declaration 快照；plan_time 从 publish_task_item JOIN） */
export interface PublishRecord {
  id: string
  publish_title: string
  publish_topics: string
  publish_time: string
  video_title: string
  account_id: string
  account_snapshot: string | null
  shop_snapshot: string | null
  project_id: string
  video_id: string
  video_local_path: string
  online_video_id: string | null
  create_time: string
  intro_snapshot?: string | null
  topics_snapshot?: string | null
  declaration?: string | null
  allow_download?: number | null
  project_title?: string | null
  plan_time?: string | null
  item_status?: 'waiting' | 'publishing' | 'suspended' | 'success' | 'failed' | 'cancelled' | null
}

// ============ #450 新形态：对象数组（统一权威形态） ============

/** 门店快照：包含 id + name + city + poi_id。 */
export interface ShopDraftEntry {
  id: string
  name: string
  city: string
  poi_id: string
}

/** 简介条目（临时文本模式）：每条前端生成 uuid 作 id。 */
export interface IntroDraftEntry {
  id: string
  content: string
  topics?: string[]
}

/** 项目快照：包含项目元数据 + 门店列表 + 简介列表。 */
export interface ProjectDraftSnapshot {
  id: string
  title: string
  shops: ShopDraftEntry[]
  intros: IntroDraftEntry[]
  introMode?: 'list' | 'manual'
}

/** 账号快照。 */
export interface AccountDraftSnapshot {
  id: string
  nickname?: string | null
  remark?: string | null
}

export interface DailyLimitEntry {
  account_id: string
  account_label?: string
  limit: number
  schedule_mode?: 'fixed' | 'balanced'
  fixed_interval_min?: number
  balanced_step_min?: number
  // 老数据兼容（旧版本只存 interval_min 单字段）：duplicate 回填时按当前 mode 落到对应字段
  interval_min?: number
}

// #418：成品视频目录项（一个目录 = 一个项目）
export interface VideoDirEntry {
  id: string
  abs_path: string
  dir_name: string
  video_count: number
}

// #418：扫描目录返回结果
export interface VideoDirScanResult {
  abs_path: string
  dir_name: string
  video_count: number
  video_files: string[]
}

/**
 * 向导统一请求载荷（对应后端 DraftPayload / SaveDraftRequest / PreviewRequest / ConfirmRequest）。
 * 统一只发对象数组形态（project_snapshots / account_snapshots）；旧 projects / account_ids /
 * account_labels 为过渡期可选字段（后端已忽略），待 duplicate_task 回填迁移完成后移除。
 */
export interface DraftRequest {
  task_name: string
  // #450 新形态（唯一权威）
  project_snapshots?: ProjectDraftSnapshot[]
  account_snapshots?: AccountDraftSnapshot[]
  daily_limit_mode: 'global' | 'per_account'
  daily_limit_global?: number
  daily_limit_per_account: DailyLimitEntry[]
  start_time?: string | null
  end_time?: string | null
  schedule_mode?: 'fixed' | 'balanced'
  fixed_interval_min?: number
  balanced_step_min?: number
  declaration?: string
  allow_download?: boolean
  task_id?: string
  project_source?: 'project' | 'video_dir'
  video_dirs?: VideoDirEntry[]
  manual_shops?: Record<string, ShopDraftEntry[]>
  manual_intros?: Record<string, string[]>
}

export interface PreviewItem {
  account_id: string
  account_nickname?: string
  account_remark?: string
  project_id: string
  project_title?: string
  shop_id: string
  shop_name?: string
  intro_id: string
  intro_content: string
  intro_topics: string[]
  plan_time: string
  video_path?: string
  schedule_mode?: 'fixed' | 'balanced'
  interval_min?: number
}

export interface PreviewStats {
  total: number
  by_account: Record<string, number>
  by_project: Record<string, number>
}

export interface PreviewResult {
  // 直传预览无 task_id（后端返回 null）；草稿预览返回真实 id
  task_id?: string | null
  overflow: boolean
  message?: string
  items: PreviewItem[]
  stats: PreviewStats
  validation?: {
    warnings: ValidationWarning[]
    has_warnings: boolean
  }
}

export interface ValidationWarning {
  project_id: string
  stale_in_draft: string[]
  extra_in_draft: string[]
}

export const publishApi = {
  /** #413 向导：保存草稿（任意步可调） */
  saveDraft: async (body: DraftRequest) => {
    const r = await request<ApiResp<{ task_id: string; draft: boolean }>>(
      '/publish/tasks/draft', { method: 'POST', body: JSON.stringify(body) })
    return r.data!
  },

  /** #413 向导：确认草稿 → running */
  confirmTask: async (taskId: string) => {
    const r = await request<ApiResp<{ task_id: string; item_count: number; running: boolean }>>(
      `/publish/tasks/${taskId}/confirm`, { method: 'POST' })
    return r.data!
  },

  /** #441 直传 payload 预览（不入库；wizard 临时化版） */
  previewDirect: async (body: Omit<DraftRequest, 'task_id'>) => {
    const r = await request<ApiResp<PreviewResult>>('/publish/tasks/preview-direct', {
      method: 'POST', body: JSON.stringify(body) })
    return r.data!
  },

  /** #441 直传 payload 确认（一次性入库 → running；不需要先 saveDraft） */
  confirmDirect: async (body: Omit<DraftRequest, 'task_id'>) => {
    const r = await request<ApiResp<{ task_id: string; item_count: number; running: boolean }>>(
      '/publish/tasks/confirm-direct', { method: 'POST', body: JSON.stringify(body) })
    return r.data!
  },

  /** #453：复制任务为 wizard payload（不写库），前端开 wizard 回显 */
  duplicateTask: async (taskId: string) => {
    const r = await request<ApiResp<{ payload: Omit<DraftRequest, 'task_id'> }>>(
      `/publish/tasks/${taskId}/duplicate`, { method: 'POST' })
    return r.data!
  },

  /** #413：任务只读详情 */
  getTaskDetail: async (taskId: string) => {
    const r = await request<ApiResp<PublishTask & { items: PublishItem[] }>>(
      `/publish/tasks/${taskId}/detail`)
    return r.data!
  },

  /** #418：扫描成品视频目录 */
  scanVideoDir: async (absPath: string) => {
    const r = await request<ApiResp<VideoDirScanResult>>('/publish/video-dirs/scan', {
      method: 'POST', body: JSON.stringify({ abs_path: absPath }) })
    return r.data!
  },

  /** 任务列表（支持按状态过滤） */
  listTasks: async (page = 1, pageSize = 20, status?: string) => {
    const q = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (status) q.set('status', status)
    const r = await request<ApiResp<PageResult<PublishTask>>>(`/publish/tasks?${q}`)
    return r.data!
  },

  /** 暂停/恢复 */
  toggleTask: async (id: string, paused: boolean) => {
    const r = await request<ApiResp<Record<string, never>>>(
      `/publish/tasks/${id}/toggle`, { method: 'POST', body: JSON.stringify({ paused }) })
    return r.data!
  },

  /** 取消 */
  cancelTask: async (id: string) => {
    const r = await request<ApiResp<{ cancelled_items: number }>>(
      `/publish/tasks/${id}/cancel`, { method: 'POST' })
    return r.data!
  },

  /** 删除终态任务 */
  deleteTask: async (id: string) => {
    const r = await request<ApiResp<Record<string, never>>>(
      `/publish/tasks/${id}`, { method: 'DELETE' })
    return r.data!
  },

  /** 任务级重试：plan_time-now ≥2h 复活 waiting，<2h 永久关闭 */
  retryTask: async (taskId: string) => {
    const r = await request<ApiResp<{ retried: number; closed: number }>>(
      `/publish/tasks/${taskId}/retry`, { method: 'POST' })
    return r.data!
  },

  /** 重启已取消任务：cancelled → running，明细重置回 waiting 重新派发 */
  restartTask: async (taskId: string) => {
    const r = await request<ApiResp<{ reset: number; dispatched: number }>>(
      `/publish/tasks/${taskId}/restart`, { method: 'POST' })
    return r.data!
  },

  /** 发布记录 */
  listRecords: async (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params)
    const r = await request<ApiResp<PageResult<PublishRecord>>>(`/publish/records?${q}`)
    return r.data!
  },

  /** 导出 Excel */
  exportRecords: async (outPath: string, filters: Record<string, string> = {}) => {
    const r = await request<ApiResp<{ file: string }>>('/publish/records/export', {
      method: 'POST', body: JSON.stringify({ out_path: outPath, ...filters }) })
    return r.data!
  },
}

// ============ #413/#415/#416 视频简介 + 项目-门店绑定 + 项目状态 ============
// 原 frontend/src/renderer/src/api/publish_intro.ts 已并入本文件。
// #publish-contract：所有 intro 路由后端已统一包 ApiResp 信封（与 publishApi 对齐），
// 方法内部 await 后 .data! 解包，调用方拿到业务数据本身（与 publishApi 风格一致）。
// 错误统一走 HTTPException(4xx/5xx)，由 client.ts 分类 → ApiError。

/** 简介 */
export interface VideoIntro {
  id: string
  project_id: string
  content: string
  topics_json: string           // 后端原始（#415 后固定 '[]'，保留字段兼容）
  topics?: string[]             // #415：从 content 正则提取的 #话题 列表
  enabled: number
  create_time: string
  update_time: string
}

/** 项目-门店绑定项 */
export interface ProjectShopItem {
  project_id: string
  shop_id: string
  sort_order: number
  shop_name?: string | null
  city?: string | null
  category?: string | null
  poi_id?: string | null
  /** #publish-count：累计发布成功次数（视频发布成功后 +1）。
   *  后端 list_project_shops v47 起必返回（0 表示从没发过）；后端已按 publish_count ASC 排序。 */
  publish_count: number
}

export const publishIntroApi = {
  /** 项目简介列表 */
  listIntros: async (projectId: string): Promise<VideoIntro[]> => {
    const r = await request<ApiResp<VideoIntro[]>>(`/publish/intro/projects/${projectId}/intros`)
    return r.data!
  },

  /** 新增简介 */
  createIntro: async (body: { project_id: string; content: string; topics?: string[] }): Promise<VideoIntro> => {
    const r = await request<ApiResp<VideoIntro>>('/publish/intro/intros', {
      method: 'POST', body: JSON.stringify(body),
    })
    return r.data!
  },

  /** 更新简介 */
  updateIntro: async (introId: string, body: { content?: string; topics?: string[] }): Promise<{ updated: number }> => {
    const r = await request<ApiResp<{ updated: number }>>(`/publish/intro/intros/${introId}`, {
      method: 'PATCH', body: JSON.stringify(body),
    })
    return r.data!
  },

  /** 删除简介 */
  deleteIntro: async (introId: string): Promise<{ deleted: number }> => {
    const r = await request<ApiResp<{ deleted: number }>>(`/publish/intro/intros/${introId}`, {
      method: 'DELETE',
    })
    return r.data!
  },

  /** 项目已绑门店列表（按 sort_order） */
  listProjectShops: async (projectId: string): Promise<ProjectShopItem[]> => {
    const r = await request<ApiResp<ProjectShopItem[]>>(`/publish/intro/projects/${projectId}/shops`)
    return r.data!
  },

  /** 全量覆盖项目-门店绑定（按传入顺序） */
  setProjectShops: async (projectId: string, shopIds: string[]): Promise<{ bound: number }> => {
    const r = await request<ApiResp<{ bound: number }>>(`/publish/intro/projects/${projectId}/shops`, {
      method: 'PUT', body: JSON.stringify({ shop_ids: shopIds }),
    })
    return r.data!
  },

  /** #415：富文本编辑器一次性提交所有标题/话题（全量覆盖）。
   *  每条就是一行字符串（content 已含 #话题，前端按行解析）。 */
  setProjectIntros: async (projectId: string, items: string[]): Promise<{ saved: number }> => {
    const r = await request<ApiResp<{ saved: number }>>(`/publish/intro/projects/${projectId}/intros`, {
      method: 'PUT', body: JSON.stringify({ items }),
    })
    return r.data!
  },

  /** 项目列表 + 已绑门店数（向导用） */
  listProjectsWithShops: async (): Promise<Array<{ id: string; title: string; shop_count: number }>> => {
    const r = await request<ApiResp<Array<{ id: string; title: string; shop_count: number }>>>(
      '/publish/intro/projects-with-shops')
    return r.data!
  },

  /** #416：可发布项目列表（status='normal'），用于新建发布任务向导 */
  listPublishableProjects: async (): Promise<Array<{
    id: string
    title: string
    status: string
    shop_count: number
    /** #448：可用成品数（status='idle' 的 generated_video 数），用于 step2 显示。 */
    generated_idle?: number
  }>> => {
    const r = await request<ApiResp<Array<{
      id: string
      title: string
      status: string
      shop_count: number
      generated_idle?: number
    }>>>('/publish/intro/projects-publishable')
    return r.data!
  },

  /** #416：停用/启用项目。status='disabled' 停用、'normal' 启用。
   *  停用后新建发布任务不能再选；不影响已有任务/成品。 */
  setProjectStatus: async (projectId: string, status: 'normal' | 'disabled'): Promise<{ status: string }> => {
    const r = await request<ApiResp<{ status: string }>>(
      `/publish/intro/projects/${projectId}/status`,
      { method: 'PUT', body: JSON.stringify({ status }) },
    )
    return r.data!
  },
}
