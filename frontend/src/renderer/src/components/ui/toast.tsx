import { cn } from '@/lib/utils'

/** toast 可选 action（用于"重试"等交互按钮） */
export interface ToastAction {
  label: string
  onClick: () => void
}

/** 轻提示（UI-4 长任务反馈 / UI-10 错误提示）
 * #115：错误级别同步 console.error（经 main.ts console-message 转发落盘），
 * 瞬时错误不再蒸发，运维可复盘"用户14:32:17 触发了什么 error"。
 */
export function toast(
  message: string,
  type: 'info' | 'error' | 'success' = 'info',
  options?: { action?: ToastAction; duration?: number },
) {
  // 错误级别同步落日志：3.2s 后 DOM 自动移除，用户来不及复现的瞬时错误也保留痕
  if (type === 'error') {
    console.error(`[UI] toast: ${message}`)
  } else if (type === 'success') {
    console.info(`[UI] toast: ${message}`)
  } else {
    console.info(`[UI] toast: ${message}`)
  }
  // 直接操作 DOM 级轻提示，避免 store 复杂化：挂载到 #toast-root
  const root = document.getElementById('toast-root')
  if (!root) return
  const el = document.createElement('div')
  el.className = cn(
    'flex items-center gap-3 px-4 py-2 rounded-md shadow-lg text-sm text-white max-w-md',
    type === 'error' ? 'bg-danger' : type === 'success' ? 'bg-ok' : 'bg-gray-800',
  )
  const text = document.createElement('span')
  text.textContent = message
  el.appendChild(text)
  if (options?.action) {
    const btn = document.createElement('button')
    btn.className = 'rounded bg-white/20 px-2 py-0.5 text-xs hover:bg-white/30'
    btn.textContent = options.action.label
    btn.onclick = () => {
      options.action!.onClick()
      el.remove()
    }
    el.appendChild(btn)
  }
  root.appendChild(el)
  // duration: 自定义 > 默认（error 8s / 其他 3.2s）
  const duration = options?.duration ?? (type === 'error' ? 8000 : 3200)
  setTimeout(() => el.remove(), duration)
}

/** Toast 容器（App 挂载一次） */
export function ToastContainer() {
  return (
    <div
      id="toast-root"
      className="pointer-events-none fixed top-5 left-1/2 z-[100] flex -translate-x-1/2 flex-col items-center gap-2"
    />
  )
}
