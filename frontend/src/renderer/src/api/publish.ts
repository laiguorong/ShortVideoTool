import { request, type PageResult } from './client'

/** 发布任务（#413 含 end_time / 各种时间窗配置） */
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
  // 旧字段兼容
  same_project_interval_min?: number
  diff_project_interval_min?: number
  daily_limit_mode?: 'global' | 'per_account'
  daily_limit_global?: number | null
  declaration?: string | null
  allow_download?: 0 | 1
  status: 'draft' | 'running' | 'paused' | 'finished' | 'cancelled'
  // 派生显示状态：finished 含失败 / cancelled 含成功 → 'partial'，徽标变黄色
  // 其他场景等于 status
  display_status?: 'draft' | 'running' | 'paused' | 'finished' | 'cancelled' | 'partial'
  success_count: number
  fail_count: number
  success_count_real: number
  fail_count_real: number
  item_total: number
  create_time: string
  // #v42：首次进入 running 的时刻；进度列用时 = now - started_at
  // NULL 表示从未进入 running（草稿/未确认），进度列不显示用时
  started_at?: string | null
  // 账号/项目/门店 ID 列表（JSON 字符串）
  account_ids_json?: string | null
  project_ids_json?: string | null
  shop_ids_json?: string | null
  // #418：视频目录模式字段（getTaskDetail 返回；编辑草稿时回填用）
  project_source?: 'project' | 'video_dir'
  video_dirs_json?: string | null       // JSON 字符串
  manual_shops_json?: string | null      // JSON 字符串
  manual_intros_json?: string | null     // JSON 字符串（视频简介-按目录）
  projects_payload_json?: string | null  // JSON 字符串（项目模式门店）
  daily_limit_per_account_json?: string | null
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
  topics_snapshot?: string | null   // JSON 数组
  declaration?: string | null
  allow_download?: number | null
  status: 'waiting' | 'publishing' | 'suspended' | 'success' | 'failed' | 'cancelled'
  fail_reason: string | null
  retry_count: number
  /** JOIN generated_video：成品视频相对路径 */
  video_path?: string | null
  /** JOIN project：项目标题作为视频名（成品共享同项目标题；视频目录模式为空） */
  video_title?: string | null
  /** JOIN generated_video：组合键（仅供排查） */
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
  // 计划发布时间（来自 publish_task_item.plan_time；可能为 null 表示明细已无关联）
  plan_time?: string | null
  // 明细状态（用于判断发布失败时是否隐藏预览按钮）
  item_status?: 'waiting' | 'publishing' | 'suspended' | 'success' | 'failed' | 'cancelled' | null
}

// ============ #413 向导模型 ============

export interface ProjectDraftEntry {
  project_id: string
  // #449：项目名称快照（日志用，不参与校验）
  project_title?: string
  shop_ids?: string[]           // 空 = 回查 project_shop
  // #449：与 shop_ids 等长，按序；供后端日志 / 排查
  shop_names?: string[]
  shop_cities?: string[]
  shop_poi_ids?: string[]
  intro_ids?: string[]          // 空 = 回查全部
  // #449：与 intro_ids 等长，按序；供后端日志 / 排查
  intro_contents?: string[]
  intro_topics?: string[]
}

// ============ #450 新形态：对象数组 ============

/** 门店快照：包含 id + name + city + poi_id，不再分离 id 数组。 */
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
}

/** 账号快照。 */
export interface AccountDraftSnapshot {
  id: string
  nickname?: string | null
  remark?: string | null
}

export interface DailyLimitEntry {
  account_id: string
  // #449：账号显示名快照（日志用）
  account_label?: string
  limit: number
  // #596：分账号下每账号独立算模式 + 两个独立 interval 字段（切 mode 不影响值）
  schedule_mode?: 'fixed' | 'balanced'
  fixed_interval_min?: number
  balanced_step_min?: number
  // 老字段兼容（被 fixed_interval_min/balanced_step_min 取代；老 payload 透传时按 mode 落到对应字段）
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

export interface DraftRequest {
  task_name: string
  // #450 新形态（优先）
  project_snapshots?: ProjectDraftSnapshot[]
  account_snapshots?: AccountDraftSnapshot[]
  // 旧形态保留兼容
  projects?: ProjectDraftEntry[]
  account_ids?: string[]
  // #449：与 account_ids 等长，按序；供后端日志 / 排查
  account_labels?: string[]
  daily_limit_mode: 'global' | 'per_account'
  daily_limit_global?: number
  daily_limit_per_account: DailyLimitEntry[]
  start_time?: string | null
  end_time?: string | null
  // #calc_mode：计算模式
  schedule_mode?: 'fixed' | 'balanced'
  fixed_interval_min?: number              // schedule_mode='fixed' 时使用，默认 10
  balanced_step_min?: number               // schedule_mode='balanced' 时使用（步长），默认 60
  // 旧字段保留兼容（旧 wizard 仍可能带，后端 fallback 链处理）
  same_project_interval_min?: number
  diff_project_interval_min?: number
  // #417/#419：存文本内容（不是 enum），默认「无需添加自主声明」
  declaration?: string
  allow_download?: boolean
  /** #413 R12：传 task_id 时改为更新已有 draft（不改 = 新建） */
  task_id?: string

  // #418：项目来源
  project_source?: 'project' | 'video_dir'
  // #418：成品视频目录列表（仅 video_dir 模式）
  video_dirs?: VideoDirEntry[]
  // #418：手动输入门店（按 video_dir.id 索引；仅 video_dir 模式）
  manual_shops?: Record<string, ShopDraftEntry[]>
  // 视频简介-按项目一一对应：每个视频目录独立一份简介数组
  manual_intros?: Record<string, string[]>
}

export interface PreviewItem {
  account_id: string
  project_id: string
  /** #418 video_dir 模式：目录名（dir_name），与 wizard step2 显示一致 */
  project_title?: string
  shop_id: string
  shop_name?: string
  intro_id: string
  intro_content: string
  intro_topics: string[]
  plan_time: string
  video_path?: string
  // #596：preview 行带算模式 + 当前 mode 对应间隔（per_account 模式按账号不同；global 与顶层一致）
  schedule_mode?: 'fixed' | 'balanced'
  interval_min?: number
}

export interface PreviewStats {
  total: number
  by_account: Record<string, number>
  by_project: Record<string, number>
}

export interface PreviewResult {
  task_id: string
  overflow: boolean
  message?: string
  items: PreviewItem[]
  stats: PreviewStats
  // #439：draft.shop_ids 与 DB.project_shop 一致性校验结果；
  // 前端 wizard 拦截 stale_in_db / extra_in_draft，避免带病提交
  validation?: {
    warnings: ValidationWarning[]
    has_warnings: boolean
  }
}

export interface ValidationWarning {
  project_id: string
  /** #445：draft 勾的但当前不可用（被取消加库 / 无佣金 / 解除绑定） */
  stale_in_draft: string[]
  /** draft 勾的店曾绑定项目但已被彻底移除 */
  extra_in_draft: string[]
}

export const publishApi = {
  /** [旧] 创建发布任务（向导请走 draft/preview/confirm） */
  createTask: (body: {
    task_name: string
    project_ids: string[]
    account_ids: string[]
    shop_ids: string[]
    per_account_count: number
    start_time: string
    interval_minutes: number
    shop_assign_mode?: string
  }) =>
    request<{ task_id: string; item_count: number }>('/publish/tasks', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #413 向导：保存草稿（任意步可调） */
  saveDraft: (body: DraftRequest) =>
    request<{ task_id: string; draft: boolean }>('/publish/tasks/draft', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #413 向导：预览排期（不入库） */
  previewTask: (taskId: string) =>
    request<PreviewResult>(`/publish/tasks/${taskId}/preview`),

  /** #413 向导：确认草稿 → running */
  confirmTask: (taskId: string) =>
    request<{ task_id: string; item_count: number; running: boolean }>(
      `/publish/tasks/${taskId}/confirm`, { method: 'POST' }),

  /** #441 直传 payload 预览（不入库；wizard 临时化版） */
  previewDirect: (body: Omit<DraftRequest, 'task_id'>) =>
    request<PreviewResult>('/publish/tasks/preview-direct', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #441 直传 payload 确认（一次性入库 → running；不需要先 saveDraft） */
  confirmDirect: (body: Omit<DraftRequest, 'task_id'>) =>
    request<{ task_id: string; item_count: number; running: boolean }>(
      '/publish/tasks/confirm-direct', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** #453 改：复制任务为 wizard payload（不写库），前端开 wizard 回显 */
  duplicateTask: (taskId: string) =>
    request<{ payload: Omit<DraftRequest, 'task_id'> }>(
      `/publish/tasks/${taskId}/duplicate`, { method: 'POST' }),

  /** #413：任务只读详情 */
  getTaskDetail: (taskId: string) =>
    request<PublishTask & { items: PublishItem[] }>(`/publish/tasks/${taskId}/detail`),

  // #418：扫描成品视频目录
  scanVideoDir: (absPath: string) =>
    request<VideoDirScanResult>('/publish/video-dirs/scan', {
      method: 'POST',
      body: JSON.stringify({ abs_path: absPath }),
    }),

  /** 任务列表（支持按状态过滤） */
  listTasks: (page = 1, pageSize = 20, status?: string) => {
    const q = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (status) q.set('status', status)
    return request<PageResult<PublishTask>>(`/publish/tasks?${q}`)
  },

  /** 任务详情（含明细；旧接口保留） */
  getTask: (id: string) =>
    request<PublishTask & { items: PublishItem[] }>(`/publish/tasks/${id}`),

  /** 暂停/恢复 */
  toggleTask: (id: string, paused: boolean) =>
    request<{ ok: boolean }>(`/publish/tasks/${id}/toggle`, {
      method: 'POST',
      body: JSON.stringify({ paused }),
    }),

  /** 取消 */
  cancelTask: (id: string) =>
    request<{ ok: boolean; cancelled_items: number }>(`/publish/tasks/${id}/cancel`, { method: 'POST' }),

  /** 删除终态任务 */
  deleteTask: (id: string) =>
    request<{ ok: boolean }>(`/publish/tasks/${id}`, { method: 'DELETE' }),

  /** 任务级重试：plan_time-now ≥2h 复活 waiting，<2h 永久关闭 */
  retryTask: (taskId: string) =>
    request<{ ok: boolean; retried: number; closed: number }>(
      `/publish/tasks/${taskId}/retry`, { method: 'POST' }),

  /** 重启已取消任务：cancelled → running，明细重置回 waiting 重新派发 */
  restartTask: (taskId: string) =>
    request<{ ok: boolean; reset: number; dispatched: number }>(
      `/publish/tasks/${taskId}/restart`, { method: 'POST' }),

  /** 发布记录 */
  listRecords: (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params)
    return request<PageResult<PublishRecord>>(`/publish/records?${q}`)
  },

  /** 导出 Excel */
  exportRecords: (outPath: string, filters: Record<string, string> = {}) =>
    request<{ ok: boolean; file: string }>('/publish/records/export', {
      method: 'POST',
      body: JSON.stringify({ out_path: outPath, ...filters }),
    }),
}
