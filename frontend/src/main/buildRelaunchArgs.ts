/**
 * #data-dir-choice：app:relaunch 参数构造。
 *
 * dev 模式必须显式传 entry 路径：`process.argv.slice(1)` 在新进程 cwd 下可能找不到相对路径 '.'
 * （典型情况：`npm run dev` → electron . → process.argv = ['electron.exe', '.']）
 * 用 `app.getAppPath()` 拿到项目根绝对路径，新进程 cwd 一致即可。
 *
 * prod（NSIS 包）：args 可省略，沿用上次启动参数即可。
 */
import type { App } from 'electron'

export interface RelaunchArgs {
  /** 透传给 app.relaunch 的 args；空对象 = 不传 */
  args?: string[]
}

export function buildRelaunchArgs(app: Pick<App, 'isPackaged' | 'getAppPath'>): RelaunchArgs {
  if (!app.isPackaged) {
    // dev：传项目根绝对路径，避免新进程 cwd 找不到相对路径 '.'
    return { args: [app.getAppPath()] }
  }
  return {}
}