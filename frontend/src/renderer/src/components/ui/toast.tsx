import { cn } from '@/lib/utils'

/** 轻提示（UI-4 长任务反馈 / UI-10 错误提示）
 * #115：错误级别同步 console.error（经 main.ts console-message 转发落盘），
 * 瞬时错误不再蒸发，运维可复盘"用户14:32:17 触发了什么 error"。
 */
export function toast(message: string, type: 'info' | 'error' | 'success' = 'info') {
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
    'px-4 py-2 rounded-md shadow-lg text-sm text-white max-w-md',
    type === 'error' ? 'bg-danger' : type === 'success' ? 'bg-ok' : 'bg-gray-800',
  )
  el.textContent = message
  root.appendChild(el)
  // 错误级别延长显示时间（8s），便于用户复制/复现
  setTimeout(() => el.remove(), type === 'error' ? 8000 : 3200)
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
