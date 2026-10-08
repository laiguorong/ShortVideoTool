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
}

contextBridge.exposeInMainWorld('electronAPI', api)

declare global {
  interface Window {
    electronAPI: ElectronAPI
  }
}
