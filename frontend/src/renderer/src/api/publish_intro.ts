/** 视频简介 + 项目-门店绑定 API（#413 发布管理重设计，#415 存储合并）。 */
import { request } from './client'

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
}

export const publishIntroApi = {
  /** 项目简介列表 */
  listIntros: (projectId: string) =>
    request<VideoIntro[]>(`/publish/intro/projects/${projectId}/intros`),

  /** 新增简介 */
  createIntro: (body: { project_id: string; content: string; topics?: string[] }) =>
    request<VideoIntro>('/publish/intro/intros', {
      method: 'POST', body: JSON.stringify(body),
    }),

  /** 更新简介 */
  updateIntro: (introId: string, body: { content?: string; topics?: string[] }) =>
    request<{ ok: boolean; updated: number }>(`/publish/intro/intros/${introId}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),

  /** 删除简介 */
  deleteIntro: (introId: string) =>
    request<{ ok: boolean; deleted: number }>(`/publish/intro/intros/${introId}`, {
      method: 'DELETE',
    }),

  /** 项目已绑门店列表（按 sort_order） */
  listProjectShops: (projectId: string) =>
    request<ProjectShopItem[]>(`/publish/intro/projects/${projectId}/shops`),

  /** 全量覆盖项目-门店绑定（按传入顺序） */
  setProjectShops: (projectId: string, shopIds: string[]) =>
    request<{ ok: boolean; bound: number }>(`/publish/intro/projects/${projectId}/shops`, {
      method: 'PUT', body: JSON.stringify({ shop_ids: shopIds }),
    }),

  /** #415：富文本编辑器一次性提交所有标题/话题（全量覆盖）。
   *  每条就是一行字符串（content 已含 #话题，前端按行解析）。 */
  setProjectIntros: (projectId: string, items: string[]) =>
    request<{ ok: boolean; saved: number }>(`/publish/intro/projects/${projectId}/intros`, {
      method: 'PUT', body: JSON.stringify({ items }),
    }),

  /** 项目列表 + 已绑门店数（向导用） */
  listProjectsWithShops: () =>
    request<Array<{ id: string; title: string; shop_count: number }>>(
      '/publish/intro/projects-with-shops'),

  /** #416：可发布项目列表（status='normal'），用于新建发布任务向导 */
  listPublishableProjects: () =>
    request<Array<{
      id: string
      title: string
      status: string
      shop_count: number
      /** #448：可用成品数（status='idle' 的 generated_video 数），用于 step2 显示。 */
      generated_idle?: number
    }>>('/publish/intro/projects-publishable'),

  /** #416：停用/启用项目。status='disabled' 停用、'normal' 启用。
   *  停用后新建发布任务不能再选；不影响已有任务/成品。 */
  setProjectStatus: (projectId: string, status: 'normal' | 'disabled') =>
    request<{ ok: boolean; status: string }>(
      `/publish/intro/projects/${projectId}/status`,
      { method: 'PUT', body: JSON.stringify({ status }) },
    ),
}