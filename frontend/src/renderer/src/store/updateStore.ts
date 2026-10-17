import { create } from 'zustand'
import { toast } from '@/components/ui/toast'

/** 自动更新状态机
 *  - idle: 初始 / 未知
 *  - checking: checkForUpdates 进行中
 *  - upToDate: 显式收到 update-not-available 事件，确认无新版本
 *  - downloading: 收到 update-available，autoUpdater 自动下载中
 *  - ready: 下载完成，可安装
 *  - error: check 抛错 / 8s 兜底超时（UI 上跟 upToDate 一样不弹反馈，仅 store 内部区分）
 *
 * 状态流转：
 *  idle → checking → upToDate (事件) / downloading (事件) → ready (事件)
 *  idle → checking → error (catch / 8s 兜底，update-not-available 没来) */
export type UpdateStatus = 'idle' | 'checking' | 'upToDate' | 'downloading' | 'ready' | 'error'

interface UpdateState {
  status: UpdateStatus
  /** 远端最新版本（发现新版本或下载完成后才有） */
  remoteVersion: string | null

  setStatus: (s: UpdateStatus) => void
  setRemoteVersion: (v: string | null) => void
}

export const useUpdateStore = create<UpdateState>((set) => ({
  status: 'idle',
  remoteVersion: null,

  setStatus: (status) => set({ status }),
  setRemoteVersion: (remoteVersion) => set({ remoteVersion }),
}))

/** 8s 兜底：比原来 1.5s 更宽容，留足慢网络时间给真 update-available 事件到达
 *  - 如果 8s 内 update-not-available 来了 → status 已被事件改成 upToDate，timeout 检查 status==='checking' 为 false → 不动作
 *  - 如果 8s 内 update-available 来了 → status 已被事件改成 downloading，timeout 不动作
 *  - 如果 8s 内啥都没来（网络挂 / GitHub 不可达）→ timeout 把 status 改成 error */
const CHECK_FALLBACK_TIMEOUT_MS = 8000

/** 一次性订阅 autoUpdater 事件，挂到 zustand store 上
 *  调用方：App.tsx 顶层 useEffect，deps=[startupComplete]，只跑一次
 *  返回：清理函数（unmount 时 removeListener） */
export function setupUpdateEventBindings() {
  if (!window.electronAPI) return () => {}

  const unsubAvail = window.electronAPI.onUpdateAvailable((info) => {
    useUpdateStore.getState().setRemoteVersion(info.version)
    useUpdateStore.getState().setStatus('downloading')
  })
  const unsubNotAvail = window.electronAPI.onUpdateNotAvailable((info) => {
    useUpdateStore.getState().setRemoteVersion(info.version)
    useUpdateStore.getState().setStatus('upToDate')
  })
  const unsubDl = window.electronAPI.onUpdateDownloaded((info) => {
    useUpdateStore.getState().setRemoteVersion(info.version)
    useUpdateStore.getState().setStatus('ready')
  })
  return () => {
    unsubAvail()
    unsubNotAvail()
    unsubDl()
  }
}

/** 手动触发检查更新（包装 IPC + 状态机）
 *  spec：只设状态，不弹任何反馈（除了 UI 上版本号旁的转圈） */
export async function triggerUpdateCheck(): Promise<void> {
  if (!window.electronAPI) return
  // 防并发：连点版本号只触发一次 check
  if (useUpdateStore.getState().status === 'checking') return
  const store = useUpdateStore.getState()
  store.setStatus('checking')
  try {
    await window.electronAPI.checkUpdate()
    // 正常情况下事件链会更新 status（upToDate / downloading / ready）
    // 兜底：8s 内没收到 update-not-available（说明网络挂 / 服务挂）→ error
    setTimeout(() => {
      const cur = useUpdateStore.getState().status
      if (cur === 'checking') {
        useUpdateStore.getState().setStatus('error')
      }
    }, CHECK_FALLBACK_TIMEOUT_MS)
  } catch {
    useUpdateStore.getState().setStatus('error')
  }
}

/** 点击"有更新"标签时调
 *  - downloading: 弹 toast "正在下载"
 *  - ready: 执行安装
 *  - 其他状态：什么都不做（按 spec） */
export function clickUpdateBadge(): void {
  const { status } = useUpdateStore.getState()
  if (status === 'downloading') {
    toast('正在下载', 'info', { duration: 2000 })
  } else if (status === 'ready') {
    window.electronAPI?.installUpdate()
  }
  // 其他状态无操作
}