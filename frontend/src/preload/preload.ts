import { contextBridge, ipcRenderer } from 'electron'

/** 渲染层可用的桌面能力（contextBridge 暴露，类型化） */
export interface ElectronAPI {
  /** 选择目录 */
  openDirectory: (defaultPath?: string) => Promise<string | null>
  /** 选择文件（多选），filters 为文件类型过滤 */
  openFiles: (
    filters?: { name: string; extensions: string[] }[],
    defaultPath?: string,
  ) => Promise<string[] | null>
  /** 保存文件对话框，返回用户选择的路径 */
  saveFile: (
    defaultName: string,
    filters?: { name: string; extensions: string[] }[],
  ) => Promise<string | null>
  /** 选择文件夹（用于上传文件夹，返回一个文件夹路径） */
  openFolder: (defaultPath?: string) => Promise<string | null>
  /** 递归列出目录内全部文件的绝对路径（上传文件夹展开用） */
  walkDir: (dirPath: string) => Promise<string[]>
  /** 打开文件/定位到文件所在目录 */
  openPath: (filePath: string) => Promise<void>
  /** #418：定位到目录（直接打开该目录，不会跳到父目录） */
  openDirInExplorer: (dirPath: string) => Promise<void>
  /** 用系统默认浏览器打开外链 */
  openExternal: (url: string) => Promise<void>
  /** 后端端口（渲染层拼 API 基址） */
  getBackendPort: () => Promise<number>
  /** #data-dir-choice：设置页"更改数据目录"用，弹框选目录并写后端 settings.json */
  chooseDataDir: () => Promise<{ abs_path: string } | null>
  /** #597：启动页触发的主进程数据目录选择完整流程（默认盘 + 立即选择 + 5 次重试 + retry-or-quit） */
  chooseDataDirFlow: () => Promise<{ abs_path: string } | null>
  /** 默认盘探测结果（"恢复默认"按钮展示） */
  defaultDataDirHint: () => Promise<{ disk: string; abs_path: string } | null>
  /** 重启 Electron（写入数据目录后立即生效） */
  relaunch: () => Promise<void>
  /** 手动触发检查更新（点击版本号时调） */
  checkUpdate: () => Promise<{ ok: boolean; version?: string | null; error?: string }>
  /** 立即安装已下载的更新（点击"有更新"标签时调） */
  installUpdate: () => void
  /** 订阅 autoUpdater 事件（进入首页时调一次，统一管理订阅生命周期） */
  onUpdateAvailable: (cb: (info: { version: string; releaseDate?: string | null }) => void) => () => void
  onUpdateNotAvailable: (cb: (info: { version: string }) => void) => () => void
  onUpdateDownloaded: (cb: (info: { version: string }) => void) => () => void
}

const api: ElectronAPI = {
  openDirectory: (defaultPath) => ipcRenderer.invoke('dialog:openDirectory', defaultPath),
  openFiles: (filters, defaultPath) => ipcRenderer.invoke('dialog:openFile', filters, defaultPath),
  openFolder: (defaultPath) => ipcRenderer.invoke('dialog:openFolder', defaultPath),
  walkDir: (dirPath) => ipcRenderer.invoke('fs:walkDir', dirPath),
  saveFile: (defaultName, filters) => ipcRenderer.invoke('dialog:saveFile', defaultName, filters),
  openPath: (filePath) => ipcRenderer.invoke('shell:openPath', filePath),
  openDirInExplorer: (dirPath) => ipcRenderer.invoke('shell:openDirInExplorer', dirPath),
  openExternal: (url) => ipcRenderer.invoke('shell:openExternal', url),
  getBackendPort: () => ipcRenderer.invoke('app:getBackendPort'),
  chooseDataDir: () => ipcRenderer.invoke('settings:chooseDataDir'),
  chooseDataDirFlow: () => ipcRenderer.invoke('dataDir:choose'),
  defaultDataDirHint: () => ipcRenderer.invoke('settings:defaultDataDirHint'),
  relaunch: () => ipcRenderer.invoke('app:relaunch'),
  checkUpdate: () => ipcRenderer.invoke('update:check'),
  installUpdate: () => ipcRenderer.invoke('update:install'),
  // 订阅事件：返回 unsubscribe 函数，调用方在 unmount 时清理
  onUpdateAvailable: (cb) => {
    const listener = (_: unknown, info: { version: string; releaseDate?: string | null }) => cb(info)
    ipcRenderer.on('update:available', listener)
    return () => ipcRenderer.removeListener('update:available', listener)
  },
  onUpdateNotAvailable: (cb) => {
    const listener = (_: unknown, info: { version: string }) => cb(info)
    ipcRenderer.on('update:not-available', listener)
    return () => ipcRenderer.removeListener('update:not-available', listener)
  },
  onUpdateDownloaded: (cb) => {
    const listener = (_: unknown, info: { version: string }) => cb(info)
    ipcRenderer.on('update:downloaded', listener)
    return () => ipcRenderer.removeListener('update:downloaded', listener)
  },
}

contextBridge.exposeInMainWorld('electronAPI', api)

declare global {
  interface Window {
    electronAPI: ElectronAPI
  }
}
