/**
 * #data-dir-choice：mount + 窗口回到前台时 reload（带 debounce + 仅 visible 守卫）。
 *
 * 解决：
 *  - 切窗口时 focus + visibilitychange 同时触发 → debounce 500ms 合并
 *  - 仅在页面可见时 reload（visibilitychange 切回前台时）；避免后台 tab 抢资源
 *  - focus 事件在 BrowserWindow 内任意元素获焦都会触发 → 必须 visible 守卫
 *  - 卸载 race：cleanup 调 scheduler.cancel() 阻止 unmount 后 setState
 *
 * 核心调度逻辑抽到 createReloadScheduler() 纯函数便于 tsx 单测。
 */
import { useEffect } from 'react'
import { createReloadScheduler } from './createReloadScheduler'

export interface UseReloadOnVisibleOpts {
  /** 防抖延迟（ms）；默认 500 */
  debounceMs?: number
  /** 是否在 mount 时立即触发 reload；默认 true */
  immediate?: boolean
}

export function useReloadOnVisible(
  reload: () => void,
  opts: UseReloadOnVisibleOpts = {},
): void {
  const { debounceMs = 500, immediate = true } = opts
  useEffect(() => {
    const scheduler = createReloadScheduler(reload, {
      debounceMs,
      isVisible: () => document.visibilityState === 'visible',
    })
    const onFocus = () => scheduler.trigger()
    const onVisibility = () => scheduler.trigger()
    if (immediate) reload()
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      scheduler.cancel()
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload, debounceMs, immediate])
}