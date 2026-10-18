import { request, type PageResult, type ApiResp } from './client'

/** 账号记录（后端 account 表投影，不含 Cookie） */
export interface Account {
  id: string
  douyin_id: string
  nickname: string | null
  avatar: string | null
  remark: string | null
  status: 'normal' | 'undetected' | 'invalid' | 'disabled'
  last_login_time: string | null
  fan_count: number | null
  work_count: number | null
  create_time: string
}

/** 有 profile 的账号（用于选品任务创建） */
export interface AvailableAccount {
  id: string
  nickname: string | null
  remark: string | null
  status: string
}

export const accountApi = {
  /** 有 storage 的账号列表（选品拉取任务创建时优先用，避免孤儿账号） */
  available: async (): Promise<{ list: AvailableAccount[]; total: number }> => {
    const r = await request<ApiResp<{ list: AvailableAccount[]; total: number }>>('/accounts/available')
    return r.data!
  },

  /** 账号分页列表 */
  list: async (params: { status?: string; keyword?: string; page?: number; page_size?: number } = {}): Promise<PageResult<Account>> => {
    const q = new URLSearchParams()
    if (params.status) q.set('status', params.status)
    if (params.keyword) q.set('keyword', params.keyword)
    q.set('page', String(params.page ?? 1))
    q.set('page_size', String(params.page_size ?? 20))
    const r = await request<ApiResp<PageResult<Account>>>(`/accounts?${q}`)
    return r.data!
  },

  /** 添加账号（登录窗抓 cookie + 页面身份信息）。后端收到完整 Cookie 串 */
  add: async (
    cookie: string,
    remark: string,
    profile: { nickname?: string; douyin_id?: string; avatar?: string } = {},
    profileDir?: string,
  ): Promise<Account> => {
    const r = await request<ApiResp<Account>>('/accounts', {
      method: 'POST',
      body: JSON.stringify({ cookie, remark, ...profile, profile_dir: profileDir ?? null }),
    })
    return r.data!
  },

  /** 弹 Playwright headed 浏览器登录窗，登录成功后返回 cookie + 页面 evaluate 拿到的身份信息。
   *  accountId 可选：重新登录时传，登录成功 storage.json 自动落到账号目录。
   *  添加账号场景额外返回 profile_dir（临时 chromium profile 路径），前端 add 时回传让后端搬移到账号目录 */
  openLogin: async (accountId?: string): Promise<{
    cookie: string | null
    nickname: string
    douyin_id: string
    avatar: string
    profile_dir?: string
  }> => {
    const r = await request<ApiResp<{
      cookie: string | null
      nickname: string
      douyin_id: string
      avatar: string
      profile_dir?: string
    }>>('/accounts/login-window', {
      method: 'POST',
      body: JSON.stringify({ account_id: accountId ?? null }),
    })
    return r.data!
  },

  /** 重新登录（登录窗抓 cookie + 页面身份信息，更新账号） */
  relogin: async (
    id: string,
    newCookie: string,
    profile: { nickname?: string; douyin_id?: string; avatar?: string } = {},
  ): Promise<{ account_id: string; status: string; message: string }> => {
    const r = await request<ApiResp<{ account_id: string; status: string; message: string }>>(
      `/accounts/${id}/relogin`,
      { method: 'POST', body: JSON.stringify({ new_cookie: newCookie, ...profile }) },
    )
    return r.data!
  },

  /** 手动检测单个账号的会话有效性（无 UI 触发，由前端点"检测"按钮调用） */
  check: async (id: string): Promise<{
    account_id: string
    status: string
    message: string
    auto_relogin_started?: boolean
    bg_task_id?: string
  }> => {
    const r = await request<ApiResp<{
      account_id: string
      status: string
      message: string
      auto_relogin_started?: boolean
      bg_task_id?: string
    }>>(`/accounts/${id}/check`, { method: 'POST' })
    return r.data!
  },

  /** 编辑备注名 */
  updateRemark: async (id: string, remark: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/accounts/${id}/remark`,
      { method: 'PUT', body: JSON.stringify({ remark }) },
    )
    return r.data!
  },

  /** 启停 */
  toggleDisable: async (id: string, disabled: boolean): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(
      `/accounts/${id}/toggle-disable`,
      { method: 'POST', body: JSON.stringify({ disabled }) },
    )
    return r.data!
  },

  /** 删除 */
  remove: async (id: string): Promise<{ ok: boolean }> => {
    const r = await request<ApiResp<{ ok: boolean }>>(`/accounts/${id}`, { method: 'DELETE' })
    return r.data!
  },
}
