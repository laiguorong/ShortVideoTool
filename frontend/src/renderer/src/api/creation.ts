import { request, type PageResult } from './client'

/** 片段 */
export interface Clip {
  id: string
  shot_id: string
  material_id: string
  clip_start_ms: number
  clip_end_ms: number
  mirrored: number
  sort_order?: number
  rev?: number
  source_index?: number | null
  thumb_path?: string | null
  file_path?: string
  file_status?: 'pending' | 'ready' | 'failed' | null
  fail_reason?: string | null
  material_title?: string | null
  material_status?: string | null
  orientation?: string | null
  /** #399：素材首帧封面相对路径（来自 material.cover_url JOIN），用于原视频预览图列表 */
  material_cover_url?: string | null
  /** #404：素材时长毫秒（来自 material.duration_ms JOIN），用于原视频预览图右下角 */
  material_duration_ms?: number | null
  /** v25：累计被选次数（按权重 max(0, 10000 - used_count) 采样用） */
  used_count?: number
}

/** 分镜（含片段） */
export interface Shot {
  id: string
  project_id: string
  name: string
  sort_order: number
  clips: Clip[]
  clip_count: number
}

/** 项目 */
export interface Project {
  id: string
  title: string
  shot_count: number
  bgm_strategy: 'order' | 'random'
  dedup_rules_json: string
  output_resolution: string | null
  status: string
  /** #96：是否保留原视频声音（默认 false） */
  keep_original_audio?: boolean
  /** 项目级 BGM 音量比例（0.05~1.0，默认 0.3=30%，生成时统一应用） */
  bgm_volume?: number
  /** #多画面适配：cover=铺满裁剪 / contain=居中补黑 / fill=拉伸 / blur_bg=模糊背景+前景居中 */
  fit_mode?: 'cover' | 'contain' | 'fill' | 'blur_bg'
  /** #427：项目画幅 */
  width?: number
  height?: number
  shots?: Shot[]
  bgm_count?: number
  /** #413：累计生成数（项目表字段，删除成品不减） */
  generated_total: number
  generated_idle?: number
  /** 可生成视频数量（合法组合数 − 已生成，#64） */
  combination_available?: number
  combination_legal_total?: number
}

/** 项目列表行 */
export interface ProjectRow extends Project {
  shot_count_real: number
  /** 全部分镜的片段总数（#77） */
  clip_count: number
  generated_total: number
  generated_idle: number
  /** 可生成数：合法组合上界（倒推规则，有空分镜则为 0） */
  combination_total: number
  create_time: string
}

/** 成品视频 */
export interface GeneratedVideo {
  id: string
  project_id: string
  combination_key: string
  file_path: string
  file_size: number | null
  duration_ms: number | null
  status: 'idle' | 'occupied'
  dedup_params_json: string | null
  project_title?: string | null
  create_time: string
  /** 发布情况（#68）：published=已发布 / scheduled=待发布 / 明细状态 / unpublished=未发布 */
  publish_state?: 'published' | 'scheduled' | 'unpublished' | 'waiting' | 'publishing'
    | 'suspended' | 'failed' | 'cancelled' | null
  publish_account?: string | null
  publish_time?: string | null
  online_video_id?: string | null
  publish_fail_reason?: string | null
}

/** BGM 条目（音量走项目级，本表不再带 bgm_volume 列） */
export interface BgmItem {
  id: string
  material_id: string
  sort_order: number
  title: string | null
  duration_ms: number | null
  file_status: string | null
}

/** 文案池条目 */
export interface TextPoolEntry {
  id: string
  pool_type: 'title' | 'topic'
  scope: 'global' | 'project'
  project_id: string | null
  content: string
  enabled: number
}

export const creationApi = {
  /** 项目 CRUD */
  createProject: (title: string, width?: number, height?: number, fitMode?: string) =>
    request<Project>('/creation/projects', {
      method: 'POST',
      body: JSON.stringify({ title, width, height, fit_mode: fitMode }),
    }),
  listProjects: (page = 1, pageSize = 20) =>
    request<PageResult<ProjectRow>>(`/creation/projects?page=${page}&page_size=${pageSize}`),
  /** #426：画幅预设列表（横竖屏/分辨率） */
  resolutionPresets: () =>
    request<Array<{ key: string; w: number; h: number; label: string }>>(
      '/creation/resolution-presets'),
  getProject: (id: string) => request<Project>(`/creation/projects/${id}`),
  updateProject: (id: string, body: Partial<{
    title: string; bgm_strategy: string; dedup_rules: Record<string, unknown>;
    output_resolution: string; keep_original_audio: boolean; bgm_volume: number;
    fit_mode: 'cover' | 'contain' | 'fill' | 'blur_bg';
  }>) =>
    request<Project>(`/creation/projects/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  copyProject: (id: string, newTitle: string) =>
    request<Project>(`/creation/projects/${id}/copy`, { method: 'POST', body: JSON.stringify({ new_title: newTitle }) }),
  deleteProject: (id: string, deleteFiles: boolean) =>
    request<{ ok: boolean }>(`/creation/projects/${id}?delete_files=${deleteFiles}`, { method: 'DELETE' }),

  /** 分镜 */
  addShot: (projectId: string, name = '', afterShotId?: string, beforeShotId?: string) =>
    request<Shot>(`/creation/projects/${projectId}/shots`, {
      method: 'POST',
      body: JSON.stringify({
        name, after_shot_id: afterShotId || null, before_shot_id: beforeShotId || null,
      }),
    }),
  renameShot: (shotId: string, name: string) =>
    request<{ ok: boolean }>(`/creation/shots/${shotId}/rename`, { method: 'PUT', body: JSON.stringify({ name }) }),
  moveShot: (shotId: string, direction: 'up' | 'down') =>
    request<{ ok: boolean }>(`/creation/shots/${shotId}/move`, { method: 'POST', body: JSON.stringify({ direction }) }),
  deleteShot: (shotId: string) =>
    request<{ ok: boolean }>(`/creation/shots/${shotId}`, { method: 'DELETE' }),

  /** 片段 */
  addClips: (body: {
    project_id: string
    material_ids: string[]
    cut_mode: 'whole' | 'fixed' | 'scene' | 'trim_after'
    fixed_seconds?: number
    trim_head_s?: number
    trim_tail_s?: number
    min_clip_seconds?: number
    target_shot_ids?: string[]
  }) =>
    request<{ clip_count: number; skipped: number; per_shot: Record<string, number>; bg_task_id: string }>(
      '/creation/shots/add-clips', { method: 'POST', body: JSON.stringify(body) },
    ),
  moveClip: (clipId: string, targetShotId: string) =>
    request<{ ok: boolean }>(`/creation/clips/${clipId}/move`, {
      method: 'POST', body: JSON.stringify({ target_shot_id: targetShotId }),
    }),
  reorderClips: (shotId: string, clipIds: string[]) =>
    request<{ ok: boolean }>(`/creation/shots/${shotId}/reorder-clips`, {
      method: 'POST', body: JSON.stringify({ clip_ids: clipIds }),
    }),
  deleteClip: (clipId: string) =>
    request<{ ok: boolean }>(`/creation/clips/${clipId}`, { method: 'DELETE' }),
  mirrorClip: (clipId: string) =>
    request<Clip>(`/creation/clips/${clipId}/mirror`, { method: 'POST' }),
  /** 批量操作：delete / mirror / move / retry_render（#57） */
  batchClips: (body: { clip_ids: string[]; action: 'delete' | 'mirror' | 'move' | 'retry_render'; target_shot_id?: string }) =>
    request<{ ok: boolean; affected: number; failed: { clip_id: string; reason: string }[] }>(
      '/creation/clips/batch', { method: 'POST', body: JSON.stringify(body) }),
  /** 重新渲染片段文件（失败重试 / 缩略图恢复） */
  renderClip: (clipId: string) =>
    request<Clip>(`/creation/clips/${clipId}/render`, { method: 'POST' }),
  /** #399：按素材批量删除项目内片段（含落盘文件，保留素材库本身） */
  deleteClipsByMaterials: (projectId: string, materialIds: string[]) =>
    request<{ ok: boolean; deleted: number }>(
      `/creation/projects/${projectId}/delete-by-materials`,
      { method: 'POST', body: JSON.stringify({ material_ids: materialIds }) },
    ),
  /** 差异轮询：返 rev > since_rev 的片段状态（#轮询优化） */
  clipsStatus: (projectId: string, sinceRev: number) =>
    request<{
      clips: Array<{
        id: string
        shot_id: string
        file_path: string
        thumb_path: string | null
        file_status: 'pending' | 'ready' | 'failed' | null
        fail_reason: string | null
        rev: number | null
      }>
      max_rev: number
      pending: number
      failed: number
      /** #408：项目级"分切处理中"状态（pending > 0），前端顶部警示与轮询决策用 */
      cutting: boolean
    }>(`/creation/projects/${projectId}/clips-status?since_rev=${sinceRev}`),

  /** BGM（音量走项目级） */
  addBgms: (projectId: string, materialIds: string[]) =>
    request<{ ok: boolean; added: number }>(`/creation/projects/${projectId}/bgms`, {
      method: 'POST', body: JSON.stringify({ material_ids: materialIds }),
    }),
  removeBgm: (projectId: string, materialId: string) =>
    request<{ ok: boolean }>(`/creation/projects/${projectId}/bgms?material_id=${materialId}`, { method: 'DELETE' }),
  listBgms: (projectId: string) => request<BgmItem[]>(`/creation/projects/${projectId}/bgms`),

  /** 文案池 */
  listTextPool: (poolType: 'title' | 'topic', scope: 'global' | 'project', projectId = '') =>
    request<TextPoolEntry[]>(`/creation/text-pool?pool_type=${poolType}&scope=${scope}&project_id=${projectId}`),
  addTextPool: (contents: string[], scope: 'global' | 'project', projectId?: string) =>
    request<{ ok: boolean; added: number }>('/creation/text-pool', {
      method: 'POST', body: JSON.stringify({ contents, scope, project_id: projectId }),
    }),
  deleteTextPool: (entryId: string) =>
    request<{ ok: boolean }>(`/creation/text-pool/${entryId}`, { method: 'DELETE' }),

  /** 生成与成品 */
  generate: (projectId: string, count: number) =>
    request<{ generate_task_id: string; bg_task_id: string; plan_count: number }>(
      `/creation/projects/${projectId}/generate`, { method: 'POST', body: JSON.stringify({ count }) },
    ),
  listVideos: (params: { project_id?: string; status?: string; page?: number; page_size?: number } = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
    return request<PageResult<GeneratedVideo>>(`/creation/videos?${q}`)
  },
  deleteVideo: (videoId: string, keepFile = false) =>
    request<{ ok: boolean }>(`/creation/videos/${videoId}?keep_file=${keepFile}`, { method: 'DELETE' }),
}
