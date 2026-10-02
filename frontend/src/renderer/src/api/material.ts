import { request, type PageResult } from './client'
import type { IntervalConfig } from './selection'

/** 分类节点（平铺，前端组树） */
export interface Category {
  id: string
  parent_id: string
  name: string
  type: 'video' | 'music'
  sort_order: number
  direct_count: number
  count: number
}

/** 素材记录 */
export interface Material {
  id: string
  title: string
  /** 抓取出处原标题（与素材标题分离，用户编辑 title 不影响此字段） */
  author_title?: string | null
  type: 'video' | 'music'
  category_id: string
  category_name?: string | null
  duration_ms: number | null
  file_size: number
  source_type: 'pull' | 'share' | 'upload'
  source_ref: string | null
  file_md5: string
  resolution: string | null
  orientation: 'vertical' | 'horizontal' | null
  file_path: string
  file_status: 'normal' | 'missing'
  ref_count: number
  create_time: string
  /** 出处信息（拉取/分享渠道，详情展示用） */
  author_nickname?: string | null
  author_douyin_id?: string | null
  share_url?: string | null
  download_url?: string | null
  /** 本地封面缓存相对路径（cache/cover/xxx.webp） */
  cover_url?: string | null
  /** v6 出处补充：作者头像缓存 + 互动快照 + 发布时间 */
  author_avatar?: string | null
  /** v10 作者增强：主页 sec_uid + 简介 + 粉丝/获赞 */
  author_sec_uid?: string | null
  author_signature?: string | null
  author_follower_count?: number | null
  author_total_favorited?: number | null
  digg_count?: number | null
  comment_count?: number | null
  collect_count?: number | null
  share_count?: number | null
  publish_time?: string | null
}

/** 视频拉取任务 */
export interface VideoPullTask {
  id: string
  task_name: string
  conditions_json: string
  account_id: string
  category_id: string
  interval_config: string
  status: 'enabled' | 'disabled'
  last_run_time: string | null
  next_run_time: string | null
  account_nickname?: string | null
  category_name?: string | null
  /** v22 阶梯式翻页：已完成的轮数（0 = 尚未开始；用户手动点"重新启用"才清零） */
  pull_round?: number
  /** v22 阶梯式翻页：本任务跨轮累计入库数（重置时清零） */
  total_pulled?: number
  /** v23 停用原因：null 启用中；'manual' 用户手动停用；'auto_max' 达 max_count 自动停用 */
  stop_reason?: 'manual' | 'auto_max' | null
  /** 任务队列实时进度（task_service 内存态；list 接口不返回，仅前端轮询补） */
  current_run?: {
    status: 'running' | 'waiting' | 'idle'
    progress?: string | null
    message?: string | null
  } | null
}

/** 拉取条件（VideoPullConditions，与后端模型对应） */
export interface PullConditions {
  /** 平台搜索主关键词 */
  keyword?: string
  /** 标题正则：对搜索结果标题二次过滤，如：探店|测评 */
  title_regex?: string
  /** 门店名称关键词 */
  shop_name?: string
  /** 画面方向 vertical / horizontal / 空=不限 */
  orientation?: string
  /** 排序依据 0=综合 / 1=最多点赞 / 2=最新发布（默认 2） */
  sort_type?: 0 | 1 | 2
  /** 发布时间档位 any/1d/7d/180d */
  publish_range?: 'any' | '1d' | '7d' | '180d'
  /** 视频时长档位 any/lt1m/1to5m/gt5m */
  duration_range?: 'any' | 'lt1m' | '1to5m' | 'gt5m'
  /** 是否拉取背景音乐（原声）入音乐库，默认 true */
  fetch_bgm?: boolean
  /** 是否过滤带字幕的视频（按 OCR 检测画面文字判断） */
  filter_subtitle?: boolean
  /** 是否过滤含主播人脸的视频（按 OpenCV 人脸检测判断） */
  filter_face?: boolean
  /** 单轮最大入库数量（1-1000，默认 100，达到上限任务自动停用） */
  max_count?: number
  /** [已废弃] 旧版发布时间起，仅为兼容老 JSON */
  publish_after?: string | null
  /** [已废弃] 旧版发布时间止，仅为兼容老 JSON */
  publish_before?: string | null
  /** [已废弃] 话题，v120 移除 */
  topics?: string
  /** [已废弃] 描述关键词，v120 移除 */
  desc_keywords?: string
  /** [已废弃] 发布位置，v120 移除 */
  location?: string
}

export const materialApi = {
  /** 分类树（平铺） */
  categories: (type: 'video' | 'music', orientation?: 'vertical' | 'horizontal' | null) => {
    // #441：分类计数按项目画幅过滤（与 list 列表一致；方形项目 orientation=null 透传不过滤）
    const o = orientation ? `&orientation=${orientation}` : ''
    return request<Category[]>(`/materials/categories?type=${type}${o}`)
  },

  /** 新增分类 */
  createCategory: (name: string, type: 'video' | 'music', parentId = '') =>
    request<Category>('/materials/categories', {
      method: 'POST',
      body: JSON.stringify({ name, type, parent_id: parentId }),
    }),

  /** 重命名分类 */
  renameCategory: (id: string, name: string) =>
    request<{ ok: boolean }>(`/materials/categories/${id}/rename`, {
      method: 'PUT',
      body: JSON.stringify({ name }),
    }),

  /** 移动分类（换父级/排序） */
  moveCategory: (id: string, newParentId: string, sortOrder = 0) =>
    request<{ ok: boolean }>(`/materials/categories/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ new_parent_id: newParentId, sort_order: sortOrder }),
    }),

  /** 删除分类 */
  deleteCategory: (id: string, strategy = 'to_parent') =>
    request<{ ok: boolean }>(`/materials/categories/${id}?strategy=${strategy}`, { method: 'DELETE' }),

  /** 素材列表 */
  list: (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params)
    return request<PageResult<Material>>(`/materials?${q}`)
  },

  /** 编辑素材 */
  update: (id: string, body: { title?: string; category_id?: string }) =>
    request<{ ok: boolean }>(`/materials/${id}`, { method: 'PUT', body: JSON.stringify(body) }),

  /** 删除素材 */
  remove: (id: string, keepFile = false) =>
    request<{ ok: boolean }>(`/materials/${id}?keep_file=${keepFile}`, { method: 'DELETE' }),

  /** 分享链接批量导入（异步） */
  importShare: (shareTexts: string[], categoryId: string) =>
    request<{ task_id: string }>('/materials/import-share', {
      method: 'POST',
      body: JSON.stringify({ share_texts: shareTexts, category_id: categoryId }),
    }),

  /** 分享导入任务列表 */
  listShareImportTasks: (page = 1, pageSize = 20) =>
    request<PageResult<{
      id: string
      category_id: string
      category_name?: string | null
      status: string
      total: number
      success_count: number
      failed_count: number
      message: string
      create_time: string
    }>>(`/materials/import-tasks?page=${page}&page_size=${pageSize}`),

  /** 查询分享导入任务进度 */
  getShareImportTask: (taskId: string) => request<{
    task: { id: string; category_id: string; status: string; total: number; success_count: number; failed_count: number; message: string }
    items: { id: string; share_text: string; status: string; message: string; retry_count: number; material_id: string | null }[]
  }>(`/materials/import-tasks/${taskId}`),

  /** 重试分享导入任务中的失败项 */
  retryShareImportTask: (taskId: string) => request<{ ok: boolean; retry_count: number }>(`/materials/import-tasks/${taskId}/retry`, { method: 'POST' }),

  /** 重试单条分享导入失败项 */
  retryShareImportItem: (taskId: string, itemId: string) => request<{ ok: boolean }>(`/materials/import-tasks/${taskId}/items/${itemId}/retry`, { method: 'POST' }),

  /** 删除分享导入任务 */
  deleteShareImportTask: (taskId: string) => request<{ ok: boolean }>(`/materials/import-tasks/${taskId}`, { method: 'DELETE' }),

  /** 创建本地文件/文件夹上传任务（异步） */
  upload: (filePaths: string[], categoryId: string) =>
    request<{ task_id: string }>('/materials/upload', {
      method: 'POST',
      body: JSON.stringify({ file_paths: filePaths, category_id: categoryId }),
    }),

  /** 上传任务列表 */
  listUploadTasks: (page = 1, pageSize = 20) =>
    request<PageResult<{
      id: string
      category_id: string
      category_name?: string | null
      status: string
      total: number
      success_count: number
      failed_count: number
      message: string
      create_time: string
    }>>(`/materials/upload-tasks?page=${page}&page_size=${pageSize}`),

  /** 查询上传任务进度 */
  getUploadTask: (taskId: string) => request<{
    task: { id: string; category_id: string; status: string; total: number; success_count: number; failed_count: number; message: string }
    items: { id: string; file_path: string; file_name: string; status: string; message: string; retry_count: number; material_id: string | null }[]
  }>(`/materials/upload-tasks/${taskId}`),

  /** 重试上传任务中的失败项 */
  retryUploadTask: (taskId: string) => request<{ ok: boolean; retry_count: number }>(`/materials/upload-tasks/${taskId}/retry`, { method: 'POST' }),

  /** 重试单条上传失败项 */
  retryUploadItem: (taskId: string, itemId: string) => request<{ ok: boolean }>(`/materials/upload-tasks/${taskId}/items/${itemId}/retry`, { method: 'POST' }),

  /** 删除上传任务 */
  deleteUploadTask: (taskId: string) => request<{ ok: boolean }>(`/materials/upload-tasks/${taskId}`, { method: 'DELETE' }),

  /** 创建视频拉取任务 */
  createPullTask: (body: {
    task_name: string
    conditions: PullConditions
    account_id: string
    category_id: string
    interval: IntervalConfig
  }) => request<VideoPullTask>('/materials/pull-tasks', { method: 'POST', body: JSON.stringify(body) }),

  /** 视频拉取任务列表 */
  pullTasks: (page = 1, pageSize = 20) =>
    request<PageResult<VideoPullTask>>(`/materials/pull-tasks?page=${page}&page_size=${pageSize}`),

  /** 视频拉取任务执行日志 */
  listPullLogs: (taskId: string, page = 1, pageSize = 20) => request<PageResult<{
    id: string
    run_time: string
    new_count: number
    skip_count: number
    fail_reason: string | null
  }>>(`/materials/pull-tasks/${taskId}/logs?page=${page}&page_size=${pageSize}`),

  /** 拉取执行记录聚合列表（所有任务，按时间倒序，含任务名） */
  listAllPullLogs: (page = 1, pageSize = 20) => request<PageResult<{
    id: string
    task_id: string
    task_name: string | null
    run_time: string
    new_count: number
    skip_count: number
    fail_reason: string | null
  }>>(`/materials/pull-logs?page=${page}&page_size=${pageSize}`),

  /** #351：删除单条拉取执行日志（聚合列表批量删除，前端并发） */
  deletePullLog: (logId: string) => request<{ ok: boolean }>(`/materials/pull-logs/${logId}`, { method: 'DELETE' }),

  /** 编辑视频拉取任务（全字段，下一轮生效） */
  updatePullTask: (id: string, body: {
    task_name: string
    conditions: PullConditions
    account_id: string
    category_id: string
    interval: IntervalConfig
  }) => request<VideoPullTask>(`/materials/pull-tasks/${id}`, { method: 'PUT', body: JSON.stringify(body) }),

  /** 任务启停 */
  togglePullTask: (id: string, enabled: boolean) =>
    request<VideoPullTask>(`/materials/pull-tasks/${id}/toggle`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  /** v22 阶梯重置并重新启用：清零 pull_round + total_pulled，重新启用任务。
   *  适用：达 max_count 自动停用后的"重新启用"按钮。仅对 status='disabled' 生效。 */
  restartPullTask: (id: string) =>
    request<VideoPullTask>(`/materials/pull-tasks/${id}/restart`, { method: 'POST' }),

  /** 手动触发 */
  runPullTask: (id: string) =>
    request<{ ok: boolean; bg_task_id: string }>(`/materials/pull-tasks/${id}/run`, { method: 'POST' }),

  /** 删除任务 */
  deletePullTask: (id: string) =>
    request<{ ok: boolean }>(`/materials/pull-tasks/${id}`, { method: 'DELETE' }),
}
