/**
 * #data-dir-choice：reload 调度器（debounce + cancel + visible 守卫）。
 *
 * 纯函数版（不依赖 React），便于 tsx 单测覆盖核心调度行为。
 * React hook useReloadOnVisible 仅做生命周期绑定。
 */

export interface ReloadSchedulerOpts {
  /** 防抖延迟（ms）；默认 500 */
  debounceMs?: number
  /** visible 守卫：仅 document.visibilityState === 'visible' 才触发 */
  isVisible?: () => boolean
  /** timer 实现（注入便于测试） */
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (handle: unknown) => void
}

export interface ReloadScheduler {
  /** 触发 reload（受 visible 守卫 + debounce） */
  trigger: () => void
  /** 立即取消 pending timer + 后续触发都跳过的"cancelled"状态 */
  cancel: () => void
  /** 已 cancelled 状态 */
  readonly cancelled: boolean
}

export function createReloadScheduler(
  reload: () => void,
  opts: ReloadSchedulerOpts = {},
): ReloadScheduler {
  const {
    debounceMs = 500,
    isVisible = () => true,  // 测试时可注入；默认 BrowserWindow 总是 visible
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  } = opts

  let cancelled = false
  let timer: unknown = null

  const flush = () => {
    timer = null
    if (!cancelled) reload()
  }

  return {
    get cancelled() { return cancelled },
    trigger() {
      if (cancelled) return
      if (!isVisible()) return  // 守卫：visible 时才触发
      if (timer !== null) clearTimer(timer)
      timer = setTimer(flush, debounceMs)
    },
    cancel() {
      cancelled = true
      if (timer !== null) clearTimer(timer)
      timer = null
    },
  }
}