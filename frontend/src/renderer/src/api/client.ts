/** 后端 HTTP 客户端基础封装（参考 VideoMatrix client.ts 模式）。 */

const getBaseUrl = async (): Promise<string> => {
  if (window.electronAPI) {
    const port = await window.electronAPI.getBackendPort()
    return `http://127.0.0.1:${port}/api`
  }
  return 'http://127.0.0.1:8765/api'
}

/** data 相对路径 → 可访问文件 URL（头像等经 /api/files/ 读取；中文目录逐段编码） */
export const fileUrl = async (relPath: string): Promise<string> =>
  `${await getBaseUrl()}/files/${relPath.split('/').map(encodeURIComponent).join('/')}`

/** FastAPI 422 校验错误转可读中文 */
function formatApiError(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== 'object') return null
        const error = item as { loc?: unknown[]; msg?: string }
        const field = error.loc?.at(-1)
        return field && error.msg ? `${String(field)}：${error.msg}` : error.msg
      })
      .filter((m): m is string => Boolean(m))
    if (messages.length) return messages.join('；')
  }
  return fallback
}

/** 后端 HTTP 客户端基础封装（参考 VideoMatrix client.ts 模式）。 */

/** 错误分类——用于 UI 区分 toast 样式（业务红 / 鉴权红 / 限流黄 / 系统红 / 网络黄 / 校验红） */
export type ApiErrorKind = 'business' | 'validation' | 'system' | 'network' | 'auth' | 'throttle'

/** 自定义错误：携带 status + kind，便于调用方按类型分支处理（toast/重试/上报/跳登录） */
export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly kind: ApiErrorKind) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 友好错误提示——按 status 给出中文建议。
 * path 传入便于 404 报接口名（不让 fallback 文本污染提示）。
 */
function friendlyHttpHint(status: number, path: string, fallback: string): string {
  if (status === 401 || status === 403) return `${fallback}（无权限，请重新登录）`
  if (status === 404) return `${fallback}（接口不存在：${path}）`
  if (status === 409) return `${fallback}（数据冲突，请刷新后重试）`
  if (status === 429) return `${fallback}（请求过于频繁，请稍候再试）`
  if (status >= 500) return `${fallback}（服务异常，请稍后重试）`
  return fallback
}

/** 通用请求：JSON 解析 + 错误分类
 * #111：补 method/path/status/duration 日志（dev 启用 console，prod 静默）。
 * 网络层/5xx 错误统一打 console.error 便于 DevTools 排查。
 * 错误类型：business (4xx 业务) / validation (422) / system (5xx) / network (Failed to fetch)
 *           / auth (401/403) / throttle (429)——细分 kind 让 UI 区分处理
 */
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const baseUrl = await getBaseUrl()
  const method = options?.method ?? 'GET'
  const start = performance.now()
  let res: Response
  try {
    res = await fetch(`${baseUrl}${path}`, {
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      ...options,
    })
  } catch (e) {
    // 网络层失败（ECONNREFUSED / Failed to fetch / CSP 拦截等）
    console.error(`[API] ${method} ${path} 网络异常:`, e)
    throw new ApiError('后端无响应，请检查服务是否启动', 0, 'network')
  }
  const duration = (performance.now() - start).toFixed(1)
  if (!res.ok) {
    const raw = await res.text()
    let detail: unknown = null
    try {
      detail = JSON.parse(raw).detail
    } catch {
      // 非 JSON 响应（如后端 panic 时返回 HTML），保留原文便于排查
      console.warn(`[API] ${method} ${path} 响应非 JSON status=${res.status}: ${raw.slice(0, 200)}`)
    }
    console.error(`[API] ${method} ${path} ${res.status} ${duration}ms:`, detail ?? raw.slice(0, 200))
    // 错误分类：401/403 → auth（应跳登录）；429 → throttle（应静默重试）；422 → validation；5xx → system；其他 4xx → business
    const kind: ApiErrorKind =
      res.status === 401 || res.status === 403 ? 'auth'
      : res.status === 429 ? 'throttle'
      : res.status === 422 ? 'validation'
      : res.status >= 500 ? 'system'
      : 'business'
    const msg = formatApiError(detail, `请求失败（HTTP ${res.status}）`)
    throw new ApiError(friendlyHttpHint(res.status, path, msg), res.status, kind)
  }
  // dev 调试：成功请求打 console.log 便于 trace API 流程（prod 不打以免污染）
  if (import.meta.env.DEV) {
    console.log(`[API] ${method} ${path} ${res.status} ${duration}ms`)
  }
  return res.json()
}

/** 分页结果类型（后端 PageResult 结构） */
export interface PageResult<T = Record<string, unknown>> {
  total: number
  page: number
  page_size: number
  list: T[]
}
