import { request, type PageResult, type ApiResp } from './client'
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
  /** 单轮最大入库数量（1-9999，默认 100，达到上限任务自动停用） */
  max_count?: number
  /** 作品来源：search=关键词搜索（缺省） / user_homepage=抖音用户主页 */
  source_mode?: 'search' | 'user_homepage'
  /** 抖音用户主页链接（一行一个，可含分享文本）。仅 user_homepage 生效，回填权威 */
  homepage_urls?: string[]
  /** homepage_urls 解析出的 sec_uid 缓存（后端派生，只读，勿用于回填） */
  homepage_sec_uids?: string[]
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
  categories: async (type: 'video' | 'music', orientation?: 'vertical' | 'horizontal' | null): Promise<Category[]> => {
    // #441：分类计数按项目画幅过滤（与 list 列表一致；方形项目 orientation=null 透传不过滤）
    const o = orientation ? `&orientation=${orientation}` : ''
    const r = await request<ApiResp<Category[]>>(`/materials/categories?type=${type}${o}`)
    return r.data!
  },

  /** 新增分类 */
  createCategory: async (name: string, type: 'video' | 'music', parentId = ''): Promise<Category> => {
    const r = await request<ApiResp<Category>>('/materials/categories', {
      method: 'POST',
      body: JSON.stringify({ name, type, parent_id: parentId }),
    })
    return r.data!
  },

  /** 重命名分类 */
  renameCategory: async (id: string, name: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/categories/${id}/rename`, {
      method: 'PUT',
      body: JSON.stringify({ name }),
    })
    return r.data!
  },

  /** 移动分类（换父级/排序） */
  moveCategory: async (id: string, newParentId: string, sortOrder = 0): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/categories/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ new_parent_id: newParentId, sort_order: sortOrder }),
    })
    return r.data!
  },

  /** 删除分类 */
  deleteCategory: async (id: string, strategy = 'to_parent'): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/materials/categories/${id}?strategy=${strategy}`, { method: 'DELETE' },
    )
    return r.data!
  },

  /** 素材列表 */
  list: async (params: Record<string, string> = {}): Promise<PageResult<Material>> => {
    const q = new URLSearchParams(params)
    const r = await request<ApiResp<PageResult<Material>>>(`/materials?${q}`)
    return r.data!
  },

  /** 编辑素材 */
  update: async (id: string, body: { title?: string; category_id?: string }): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/${id}`, {
      method: 'PUT', body: JSON.stringify(body),
    })
    return r.data!
  },

  /** 删除素材 */
  remove: async (id: string, keepFile = false): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/materials/${id}?keep_file=${keepFile}`, { method: 'DELETE' },
    )
    return r.data!
  },

  /** 分享链接批量导入（异步） */
  importShare: async (shareTexts: string[], categoryId: string, type: 'video' | 'music'): Promise<{ task_id: string }> => {
    const r = await request<ApiResp<{ task_id: string }>>('/materials/import-share', {
      method: 'POST',
      body: JSON.stringify({ share_texts: shareTexts, category_id: categoryId, type }),
    })
    return r.data!
  },

  /** 分享导入任务列表 */
  listShareImportTasks: async (page = 1, pageSize = 20): Promise<PageResult<{
    id: string
    category_id: string
    type: 'video' | 'music'
    category_name?: string | null
    status: string
    total: number
    success_count: number
    failed_count: number
    message: string
    create_time: string
  }>> => {
    const r = await request<ApiResp<PageResult<{
      id: string
      category_id: string
      type: 'video' | 'music'
      category_name?: string | null
      status: string
      total: number
      success_count: number
      failed_count: number
      message: string
      create_time: string
    }>>>(`/materials/import-tasks?page=${page}&page_size=${pageSize}`)
    return r.data!
  },

  /** 查询分享导入任务进度 */
  getShareImportTask: async (taskId: string): Promise<{
    task: { id: string; category_id: string; type: 'video' | 'music'; status: string; total: number; success_count: number; failed_count: number; message: string; create_time: string }
    items: { id: string; share_text: string; status: string; message: string; retry_count: number; material_id: string | null }[]
  }> => {
    const r = await request<ApiResp<{
      task: { id: string; category_id: string; type: 'video' | 'music'; status: string; total: number; success_count: number; failed_count: number; message: string; create_time: string }
      items: { id: string; share_text: string; status: string; message: string; retry_count: number; material_id: string | null }[]
    }>>(`/materials/import-tasks/${taskId}`)
    return r.data!
  },

  /** 重试分享导入任务中的失败项 */
  retryShareImportTask: async (taskId: string): Promise<{ ok: boolean; retry_count: number }> => {
    const r = await request<ApiResp<{ ok: boolean; retry_count: number }>>(
      `/materials/import-tasks/${taskId}/retry`, { method: 'POST' },
    )
    return r.data!
  },

  /** 重试单条分享导入失败项 */
  retryShareImportItem: async (taskId: string, itemId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/materials/import-tasks/${taskId}/items/${itemId}/retry`, { method: 'POST' },
    )
    return r.data!
  },

  /** 删除分享导入任务 */
  deleteShareImportTask: async (taskId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/import-tasks/${taskId}`, { method: 'DELETE' })
    return r.data!
  },

  /** 创建本地文件/文件夹上传任务（异步） */
  upload: async (filePaths: string[], categoryId: string): Promise<{ task_id: string }> => {
    const r = await request<ApiResp<{ task_id: string }>>('/materials/upload', {
      method: 'POST',
      body: JSON.stringify({ file_paths: filePaths, category_id: categoryId }),
    })
    return r.data!
  },

  /** 上传任务列表 */
  listUploadTasks: async (page = 1, pageSize = 20): Promise<PageResult<{
    id: string
    category_id: string
    category_name?: string | null
    status: string
    total: number
    success_count: number
    failed_count: number
    message: string
    create_time: string
  }>> => {
    const r = await request<ApiResp<PageResult<{
      id: string
      category_id: string
      category_name?: string | null
      status: string
      total: number
      success_count: number
      failed_count: number
      message: string
      create_time: string
    }>>>(`/materials/upload-tasks?page=${page}&page_size=${pageSize}`)
    return r.data!
  },

  /** 查询上传任务进度 */
  getUploadTask: async (taskId: string): Promise<{
    task: { id: string; category_id: string; status: string; total: number; success_count: number; failed_count: number; message: string }
    items: { id: string; file_path: string; file_name: string; status: string; message: string; retry_count: number; material_id: string | null }[]
  }> => {
    const r = await request<ApiResp<{
      task: { id: string; category_id: string; status: string; total: number; success_count: number; failed_count: number; message: string }
      items: { id: string; file_path: string; file_name: string; status: string; message: string; retry_count: number; material_id: string | null }[]
    }>>(`/materials/upload-tasks/${taskId}`)
    return r.data!
  },

  /** 重试上传任务中的失败项 */
  retryUploadTask: async (taskId: string): Promise<{ ok: boolean; retry_count: number }> => {
    const r = await request<ApiResp<{ ok: boolean; retry_count: number }>>(
      `/materials/upload-tasks/${taskId}/retry`, { method: 'POST' },
    )
    return r.data!
  },

  /** 重试单条上传失败项 */
  retryUploadItem: async (taskId: string, itemId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/materials/upload-tasks/${taskId}/items/${itemId}/retry`, { method: 'POST' },
    )
    return r.data!
  },

  /** 删除上传任务 */
  deleteUploadTask: async (taskId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/upload-tasks/${taskId}`, { method: 'DELETE' })
    return r.data!
  },

  /** 创建视频拉取任务 */
  createPullTask: async (body: {
    task_name: string
    conditions: PullConditions
    account_id: string
    category_id: string
    interval: IntervalConfig
  }): Promise<VideoPullTask> => {
    const r = await request<ApiResp<VideoPullTask>>(
      '/materials/pull-tasks', { method: 'POST', body: JSON.stringify(body) },
    )
    return r.data!
  },

  /** 视频拉取任务列表 */
  pullTasks: async (page = 1, pageSize = 20): Promise<PageResult<VideoPullTask>> => {
    const r = await request<ApiResp<PageResult<VideoPullTask>>>(
      `/materials/pull-tasks?page=${page}&page_size=${pageSize}`,
    )
    return r.data!
  },

  /** 视频拉取任务执行日志 */
  listPullLogs: async (taskId: string, page = 1, pageSize = 20): Promise<PageResult<{
    id: string
    run_time: string
    new_count: number
    skip_count: number
    fail_reason: string | null
  }>> => {
    const r = await request<ApiResp<PageResult<{
      id: string
      run_time: string
      new_count: number
      skip_count: number
      fail_reason: string | null
    }>>>(`/materials/pull-tasks/${taskId}/logs?page=${page}&page_size=${pageSize}`)
    return r.data!
  },

  /** 拉取执行记录聚合列表（所有任务，按时间倒序，含任务名） */
  listAllPullLogs: async (page = 1, pageSize = 20): Promise<PageResult<{
    id: string
    task_id: string
    task_name: string | null
    run_time: string
    new_count: number
    skip_count: number
    fail_reason: string | null
  }>> => {
    const r = await request<ApiResp<PageResult<{
      id: string
      task_id: string
      task_name: string | null
      run_time: string
      new_count: number
      skip_count: number
      fail_reason: string | null
    }>>>(`/materials/pull-logs?page=${page}&page_size=${pageSize}`)
    return r.data!
  },

  /** #351：删除单条拉取执行日志（聚合列表批量删除，前端并发） */
  deletePullLog: async (logId: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/materials/pull-logs/${logId}`, { method: 'DELETE' },
    )
    return r.data!
  },

  /** 编辑视频拉取任务（全字段，下一轮生效） */
  updatePullTask: async (id: string, body: {
    task_name: string
    conditions: PullConditions
    account_id: string
    category_id: string
    interval: IntervalConfig
  }): Promise<VideoPullTask> => {
    const r = await request<ApiResp<VideoPullTask>>(
      `/materials/pull-tasks/${id}`, { method: 'PUT', body: JSON.stringify(body) },
    )
    return r.data!
  },

  /** 任务启停 */
  togglePullTask: async (id: string, enabled: boolean): Promise<VideoPullTask> => {
    const r = await request<ApiResp<VideoPullTask>>(`/materials/pull-tasks/${id}/toggle`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    })
    return r.data!
  },

  /** v22 阶梯重置并重新启用：清零 pull_round + total_pulled，重新启用任务。
   *  适用：达 max_count 自动停用后的"重新启用"按钮。仅对 status='disabled' 生效。 */
  restartPullTask: async (id: string): Promise<VideoPullTask> => {
    const r = await request<ApiResp<VideoPullTask>>(`/materials/pull-tasks/${id}/restart`, { method: 'POST' })
    return r.data!
  },

  /** 手动触发 */
  runPullTask: async (id: string): Promise<{ ok: boolean; bg_task_id: string }> => {
    const r = await request<ApiResp<{ ok: boolean; bg_task_id: string }>>(
      `/materials/pull-tasks/${id}/run`, { method: 'POST' },
    )
    return r.data!
  },

  /** 删除任务 */
  deletePullTask: async (id: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/materials/pull-tasks/${id}`, { method: 'DELETE' })
    return r.data!
  },
}
