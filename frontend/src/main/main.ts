import { app, BrowserWindow, ipcMain, dialog, shell, Menu, net, session } from 'electron'
import {
  loadPreferredDataDir,
  writePreferredDataDir,
  validateDataDir,
  pickDefaultDataDirHint,
} from './preferredDataDir'
import { buildRelaunchArgs } from './buildRelaunchArgs'
import { decideNextAction, decideRetryOrQuit } from './dataDirChoice'
import { spawn, ChildProcess } from 'child_process'
import path from 'path'
import os from 'os'
import fs from 'fs'
import iconv from 'iconv-lite'

// #181：Windows cmd 默认 codepage 是 GBK（936），Node.js 主进程 stdout 写 UTF-8 字节流
// 被 cmd.exe 当 GBK 解释 → 中文乱码（"端口" → "绔彛"）。
// 子进程改 codepage（cmd /c chcp 65001）只影响子 shell，不传染父 console。
// 唯一可靠路径：在输出端把字符串编码成 GBK（cmd 当前 codepage）。
// GBK 与 UTF-8 对 ASCII 完全相同，中文部分编码有差异 → 整段字符串 GBK-encode 后，
// cmd 终端按 GBK 解码正确显示。
// POSIX 平台 stdout 默认 UTF-8，无需 patch。
if (process.platform === 'win32') {
  // 同时 patch stdout + stderr（console.log/warn/error 都走这两个）
  const _patchStream = (stream: NodeJS.WriteStream) => {
    const origWrite = stream.write.bind(stream)
    ;(stream as unknown as { write: typeof stream.write }).write = function (
      chunk: string | Buffer | Uint8Array,
      encodingOrCb?: BufferEncoding | ((err?: Error | null) => void),
      cb?: (err?: Error | null) => void,
    ): boolean {
      let enc: BufferEncoding | undefined
      let callback: ((err?: Error | null) => void) | undefined
      if (typeof encodingOrCb === 'function') {
        callback = encodingOrCb
      } else {
        enc = encodingOrCb
        callback = cb
      }
      if (typeof chunk === 'string') {
        const buf = iconv.encode(chunk, 'gbk')
        return origWrite(buf, 'binary', callback)
      }
      return origWrite(chunk, enc, callback)
    }
  }
  _patchStream(process.stdout)
  _patchStream(process.stderr)
}

// ===== 全局状态 =====
let mainWindow: BrowserWindow | null = null
let backendProcess: ChildProcess | null = null
let isQuitting = false
// #382：后端启动失败时用于弹窗展示的 stderr 累积（生产 stdio:'ignore' 拿不到，这里用全局 ring buffer 兜底）
// #109：原 backendStderrBuffer + join('').length 是 O(n²)，改 totalBytes 计数器避免
const backendStderrBuffer: string[] = []
let backendStderrBytes = 0
const STDERR_BUFFER_LIMIT = 8 * 1024

const isDev = !app.isPackaged
const BACKEND_PORT = 8765
const hasSingleInstanceLock = app.requestSingleInstanceLock()

// #108：统一 logger —— info 受 isDev 守卫（生产 debug 噪音屏蔽），error/warn 不守卫（生产必落盘）
// 文件日志写到 app.getPath('logs')/shortvideo-tool/main-YYYY-MM-DD.log，每日轮转。
let _logFilePath: string | null = null
let _logFileDate = ''

function _ensureLogFile(): string | null {
  // app 可能尚未 ready（被外部 require 时），延迟初始化
  if (!app.isReady()) return null
  const now = new Date()
  const ymd = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  if (_logFilePath && _logFileDate === ymd) return _logFilePath
  try {
    const logDir = path.join(app.getPath('logs'), 'shortvideo-tool')
    fs.mkdirSync(logDir, { recursive: true })
    const file = path.join(logDir, `main-${ymd}.log`)
    // 启动时追加一行分隔，方便 grep
    fs.appendFileSync(file, `\n--- start ${now.toISOString()} (isDev=${isDev}) ---\n`, 'utf8')
    _logFilePath = file
    _logFileDate = ymd
    return file
  } catch (e) {
    // 日志目录创建失败不阻塞主流程
    return null
  }
}

function _writeLog(level: 'INFO' | 'WARN' | 'ERROR', message: string): void {
  const line = `[${new Date().toISOString()}] [${level}] ${message}\n`
  const file = _ensureLogFile()
  if (file) {
    try { fs.appendFileSync(file, line, 'utf8') } catch { /* 写失败不阻塞 */ }
  }
}

/** info：开发环境输出 + 落盘；生产仅落盘（console 屏蔽减少噪音） */
function logDev(message: string): void {
  if (isDev) console.log(message)
  _writeLog('INFO', message)
}

/** warn：无论环境都 console + 落盘 */
function logWarn(message: string): void {
  console.warn(message)
  _writeLog('WARN', message)
}

/** error：无论环境都 console + 落盘（生产关键诊断不能丢） */
function errorDev(message: string, err?: unknown): void {
  const full = err ? `${message}: ${err instanceof Error ? err.stack || err.message : String(err)}` : message
  console.error(full)
  _writeLog('ERROR', full)
}

/** 开发模式下后端工程目录（frontend/../backend） */
function getDevBackendDir(): string {
  return path.join(__dirname, '../../../backend')
}

/** 定位打包内置后端 exe（extraResources/backend/），开发模式返回 null */
function getBundledBackendBinary(): string | null {
  if (isDev) return null
  const exeName = os.platform() === 'win32' ? 'shortvideo-backend.exe' : 'shortvideo-backend'
  const candidate = path.join(process.resourcesPath, 'backend', exeName)
  return fs.existsSync(candidate) ? candidate : null
}

/** 探活：端口上是否已有本工具后端在跑（health 接口可达即复用） */
async function isBackendAlive(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = net.request(`http://127.0.0.1:${BACKEND_PORT}/api/health`)
    // 2 秒内收到响应即认为存活（连不上/超时都视为未启动）
    const timer = setTimeout(() => {
      req.destroy()
      // 超时不记 ERROR（启动期间属正常等待），仅 debug 留痕
      logDev(`[Main] health 探活超时（端口 ${BACKEND_PORT}）`)
      resolve(false)
    }, 2000)
    req.on('response', (res) => {
      clearTimeout(timer)
      res.resume()
      resolve(res.statusCode === 200)
    })
    req.on('error', (e) => {
      clearTimeout(timer)
      // #113：e 是 Error，无 code；NodeJS.ErrnoException 才有 code
      const code = (e as NodeJS.ErrnoException).code
      logDev(`[Main] health 探活失败: ${code ?? e.message}`)
      resolve(false)
    })
    req.end()
  })
}

/** 拉起后端：生产用内置 exe，开发用系统 Python + uvicorn */
async function startBackend(dataDir: string | null = null) {
  // #data-dir-relaunch：主动清理端口任意占用进程（dev 手动启 / NSIS 覆盖安装残留 / 旧 Electron 孤儿）
  // 旧版"复用现有实例"的设计在 NSIS 覆盖安装场景下有 bug——旧后端 DATA_DIR/版本可能已过时，
  // 复用会导致"改了数据目录但 SettingsPage 显示旧值"或"两个进程并存"。统一为 spawn 新实例。
  await killPortOccupants(BACKEND_PORT)
  const portFree = await waitForPortFree(BACKEND_PORT, 3000)
  if (!portFree) {
    errorDev(`[Main] 端口 ${BACKEND_PORT} 3s 未释放，startBackend 放弃`)
    throw new Error(`端口 ${BACKEND_PORT} 被占用且无法释放`)
  }

  const bundled = getBundledBackendBinary()

  // #179：Windows 默认 cmd codepage 是 GBK（936），Python 子进程 stdout 按此编码，
  // Node.js 端按 UTF-8 解码会乱码。强制 PYTHONIOENCODING=utf-8 走 UTF-8 字节流，
  // Electron 主进程侧 .toString() 默认 UTF-8 解码正确。
  // PyInstaller 打包的 exe 同样识别此 env。
  const backendEnv: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',  // #179：同时启用 UTF-8 mode（PEP 540），覆盖 open()/sys.stdout 默认编码
    SHORTVIDEO_TOOL_RUNTIME: '1',  // #data-dir-relaunch：标记 Electron 主进程 spawn 的后端，
    // 后端 _args 只在此标记为 1 时才信任 env 透传的 SHORTVIDEO_DATA_DIR（避免测试/CI 污染）
  }

  // #data-dir-choice：透传首选数据目录
  //  - prod（PyInstaller exe）：用 --data-dir CLI 参数（argparse 生效）
  //  - dev（uvicorn CLI）：uvicorn 不识别 --data-dir → 用 SHORTVIDEO_DATA_DIR env 透传（main.py 读 env fallback）
  //  - dataDir=null（用户没选 / 弹窗 force-quit）：显式删除 env.SHORTVIDEO_DATA_DIR，
  //    防止 shell 残留 export 干扰后端启动路径选择
  const baseEnv: NodeJS.ProcessEnv = dataDir
    ? { ...backendEnv, SHORTVIDEO_DATA_DIR: dataDir }
    : (() => {
        const env = { ...backendEnv }
        delete env.SHORTVIDEO_DATA_DIR
        return env
      })()

  if (bundled) {
    // 生产路径：单文件后端，无需 Python 环境
    // #382：stdio 改为 'pipe' 以捕获 stderr，弹窗可展示真实失败原因
    const dataDirArg = dataDir ? ['--data-dir', dataDir] : []
    logDev(`[Main] 启动内置后端: ${bundled} --port ${BACKEND_PORT}${dataDir ? ` --data-dir ${dataDir}` : ''}`)
    backendProcess = spawn(
      bundled,
      ['--port', String(BACKEND_PORT), '--host', '127.0.0.1', ...dataDirArg],
      {
        cwd: path.dirname(bundled),
        stdio: ['ignore', 'pipe', 'pipe'],
        env: baseEnv,
      },
    )
  } else {
    // 开发路径：系统 Python（3.12.10）+ 项目依赖
    const backendDir = getDevBackendDir()
    const python = os.platform() === 'win32' ? 'python' : 'python3'
    logDev(`[Main] 启动开发后端: ${python} -m uvicorn app.main:app --port ${BACKEND_PORT}${dataDir ? ` (env SHORTVIDEO_DATA_DIR=${dataDir})` : ''}`)
    backendProcess = spawn(
      python,
      ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT), '--host', '127.0.0.1'],
      { cwd: backendDir, stdio: 'pipe', env: { ...baseEnv, PYTHONPATH: backendDir } },
    )
  }

  backendProcess.stdout?.on('data', (data) => logDev(`[Backend] ${data.toString().trim()}`))
  // #382：stderr 累积到全局 buffer，启动失败弹窗展示（替换原 dev-only log）
  backendProcess.stderr?.on('data', (data) => {
    const text = data.toString()
    backendStderrBytes += text.length
    backendStderrBuffer.push(text)
    // #109：原 join('').length 是 O(n²)；改按 totalBytes 判定，超过限制按 FIFO 丢弃。
    while (backendStderrBytes > STDERR_BUFFER_LIMIT && backendStderrBuffer.length > 0) {
      const dropped = backendStderrBuffer.shift()!
      backendStderrBytes -= dropped.length
    }
    errorDev(`[Backend] ${text.trim()}`)
  })
  backendProcess.on('error', (error) => errorDev(`[Backend] 启动失败: ${error.message}`))
  backendProcess.on('close', (code) => {
    // code=null 通常是被外部信号强杀（任务管理器/Electron 关闭父进程/Stop-Process 等），
    // 直接展示「null」容易让用户误以为是代码 bug，改成可读提示。
    const desc = code === null ? '被外部强制终止' : String(code)
    logDev(`[Backend] 退出 ${desc}`)
  })
}

/** 检测后端在 N 秒内是否能响应 health 接口；不可达返 Promise<false>。
 * 用于 #382：环境检查失败导致后端快速退出时，触发用户提示。 */
async function waitForBackendReady(timeoutMs: number): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await isBackendAlive()) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return false
}

const MAX_RETRY = 5

/** 写独立启动错误日志：app.getPath('logs')/shortvideo-tool/backend-startup-error-YYYY-MM-DD.log
 *  与主进程 log 分离，便于用户反馈启动失败问题时单独附此文件。
 *  每次失败追加一段（含 ISO 时间戳 + 失败原因 + 关键上下文）。
 *  异步 + 串行队列（防止并发 appendFile 字节错位 + 不阻塞主线程）。
 *  stderr 等大字段 256KB 上限（避免大文件触发 Windows Defender 扫描 + 占盘）。 */
let _writeErrorLogQueue: Promise<string | null> = Promise.resolve(null)
function writeStartupErrorLog(reason: string, extras?: Record<string, unknown>): Promise<string | null> {
  const job = _writeErrorLogQueue.then(async () => {
    try {
      const logDir = path.join(app.getPath('logs'), 'shortvideo-tool')
      await fs.promises.mkdir(logDir, { recursive: true })
      const now = new Date()
      const ymd = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
      const file = path.join(logDir, `backend-startup-error-${ymd}.log`)
      const STDERR_CAP = 256 * 1024
      const lines: string[] = []
      lines.push(`\n========== ${now.toISOString()} ==========`)
      lines.push(`Reason: ${reason}`)
      if (extras) {
        for (const [k, v] of Object.entries(extras)) {
          let val: string
          if (typeof v === 'string') {
            val = v.length > STDERR_CAP
              ? v.slice(0, STDERR_CAP) + `\n... [truncated ${v.length - STDERR_CAP} chars]`
              : v
          } else {
            val = JSON.stringify(v, null, 2)
          }
          lines.push(`-- ${k} --`)
          lines.push(val)
        }
      }
      await fs.promises.appendFile(file, lines.join('\n') + '\n', 'utf8')
      return file
    } catch (e) {
      errorDev('[Main] writeStartupErrorLog 失败', e)
      return null
    }
  })
  // 链到下一个 job：保持串行；失败不中断队列
  _writeErrorLogQueue = job.catch(() => null)
  return job
}

/** 强杀后端进程树：Windows taskkill /T /F（杀整树，含 ffmpeg/uvicorn 子进程），
 *  非 Windows SIGKILL。完成后兜底再清一次端口（防 dev 手动启的孤儿 / 树剪断残留）。
 *  异步 + 不抛异常（杀掉杀不死都安全）。
 *  pid 校验：必须是合法 number（防 exec 字符串注入）+ 先清引用再校验（防 toctou）。 */
async function forceKillBackend(): Promise<void> {
  if (!backendProcess) return
  // 拍快照后立刻清引用：避免后续 stopBackend / 二次 forceKillBackend 误操作
  const proc = backendProcess
  backendProcess = null
  const pid = proc.pid
  if (typeof pid !== 'number' || !Number.isFinite(pid) || pid <= 0) {
    logWarn(`[Main] forceKillBackend 跳过：pid 异常 ${pid}`)
    await killPortOccupants(BACKEND_PORT)
    return
  }

  try {
    if (process.platform === 'win32') {
      // /T 杀整棵进程树（python -m uvicorn 启的 ffmpeg subprocess / PyInstaller 启的 chromium）
      // /F 强杀（SIGTERM 在 Windows 上等价但走 TerminateProcess；/F 显式强语义）
      await new Promise<void>((resolve) => {
        const { exec } = require('child_process') as typeof import('child_process')
        exec(`taskkill /T /F /PID ${pid}`, () => resolve())
      })
      logDev(`[Main] taskkill /T /F /PID ${pid} 已发`)
    } else {
      proc.kill('SIGKILL')
      logDev(`[Main] SIGKILL ${pid} 已发`)
    }
  } catch (e) {
    errorDev(`[Main] forceKillBackend kill 异常`, e)
  }

  // 兜底：再扫一次端口（NSIS 覆盖安装残留 / dev 手动启的旧版不在 backendProcess 引用内）
  await killPortOccupants(BACKEND_PORT)
}

async function ensureDataDirChoice(): Promise<string | null> {
  // 0. 持久化路径校验通过 → 静默
  const persisted = loadPreferredDataDir()
  if (persisted) {
    const v = validateDataDir(persisted)
    if (v.ok) {
      logDev(`[Main] 沿用首选数据目录: ${persisted}`)
      return persisted
    }
    logWarn(`[Main] 首选数据目录失效（${v.reason}），将重新选择: ${persisted}`)
  }

  // 默认盘探测 → 极端：没找到任何可用盘直接弹错误退出
  const hint = pickDefaultDataDirHint()
  if (!hint) {
    await dialog.showMessageBox({
      type: 'error',
      title: '无可用磁盘',
      message: '未检测到可用的本地磁盘，无法启动。',
      detail: '请确认系统已挂载至少一个本地固定磁盘。',
      noLink: true,
    })
    return null
  }

  const isWin = process.platform === 'win32'
  const defaultLabel = isWin ? `使用默认（${hint.disk} 盘）` : '使用默认（系统根目录）'

  // 循环而非递归，防用户连续重选爆栈；超出次数强制退出
  // 决策逻辑抽到 dataDirChoice.decideNextAction（纯函数可单测）；本函数只负责弹 UI + 执行 action
  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    const choice = await dialog.showMessageBox({
      type: 'question',
      title: attempt === 1 ? '选择数据目录' : '重新选择数据目录',
      message: '请选择数据目录位置',
      buttons: [defaultLabel, '立即选择...', '取消'],
      defaultId: 0,
      cancelId: 2,
      detail: `默认将使用${isWin ? '空间最大的本地磁盘' : '当前系统根目录'}：${hint.abs_path}\n\n` +
        '该目录将用于存储数据库、素材、成品视频、账号登录态等全部本地数据。\n' +
        '可随时在「设置 → 数据目录」中更改（更改后需重启生效）。',
    })

    // 选了 + 校验（按钮=1 时）→ 拿 chosen + 校验结果
    let chosen: string | null = null
    let pickerCanceled = false
    let validation: { ok: boolean; reason?: string } | null = null
    if (choice.response === 1) {
      const picked = await dialog.showOpenDialog({
        title: '选择数据目录',
        defaultPath: hint.abs_path,
        properties: ['openDirectory', 'createDirectory'],
      })
      if (!picked.canceled && picked.filePaths.length > 0) {
        chosen = picked.filePaths[0]
        validation = validateDataDir(chosen)
      } else {
        pickerCanceled = true
      }
    }

    // 纯函数决策
    const next = decideNextAction({
      attempt,
      maxRetry: MAX_RETRY,
      buttonResponse: choice.response as 0 | 1 | 2,
      pickerCanceled,
      chosen,
      validation: validation ?? undefined,
    })

    if (next.action === 'use-default') {
      writePreferredDataDir(hint.abs_path)
      logDev(`[Main] 使用默认数据目录: ${hint.abs_path}`)
      return hint.abs_path
    }
    if (next.action === 'confirm') {
      writePreferredDataDir(next.chosen)
      logDev(`[Main] 用户选择数据目录: ${next.chosen}`)
      return next.chosen
    }
    if (next.action === 'show-validation-error') {
      await dialog.showMessageBox({
        type: 'error',
        title: '目录不可用',
        message: `所选目录无法使用：${next.reason}`,
        detail: '请重新选择其他目录。',
        noLink: true,
      })
      continue
    }

    // 'show-retry-or-quit' 或 'quit'：弹 retry-or-quit 框
    const isLast = attempt === MAX_RETRY
    if (next.action === 'quit' && isLast) {
      // 末轮：直接退出（不再弹 retry）
      logWarn(`[Main] 连续 ${MAX_RETRY} 次取消，退出启动`)
      return null
    }
    const retry = await dialog.showMessageBox({
      type: 'warning',
      title: '未选择数据目录',
      message: '数据目录是运行本工具的必备配置。',
      detail: isLast
          ? `已连续 ${MAX_RETRY} 次取消，应用将退出。下次启动仍可重新选择。`
          : '选择「重新选择」继续，或选择「退出」结束本次启动。\n下次启动仍可再次选择。',
      buttons: isLast ? ['退出'] : ['重新选择', '退出'],
      defaultId: 0,
      cancelId: isLast ? 0 : 1,
    })
    const retryAction = decideRetryOrQuit(retry.response as 0 | 1)
    if (retryAction.action === 'retry') {
      continue
    }
    return null
  }

  // 循环结束（理论上走不到，因为最后一轮只有「退出」按钮且返回 null）
  logWarn('[Main] 数据目录选择循环异常退出')
  return null
}

async function handleBackendStartupFailure(): Promise<boolean> {
  // 等 200ms 让最后一次 stderr flush 完
  await new Promise((r) => setTimeout(r, 200))
  const lastStderr = backendStderrBuffer.join('')

  // #110：完整 stderr 写错误日志文件，便于用户反馈时附日志
  errorDev(`[Main] 后端启动失败，stderr 最近 ${lastStderr.length} 字符：\n${lastStderr.slice(-4096)}`)

  // 检测是否是 ffmpeg/libx264 环境问题（#382：明确告知用户修复方式）
  const stderrText = lastStderr.toLowerCase()
  const isFfmpegMissing = stderrText.includes('未找到 ffmpeg') || stderrText.includes('libx264')
  let detail = '后端服务启动失败，请检查后端日志获取详情。'
  if (isFfmpegMissing) {
    detail =
      '本工具依赖 ffmpeg + libx264 编码器。\n\n' +
      '检测到环境不满足：\n' +
      '• ffmpeg 可执行文件缺失\n' +
      '• 或 ffmpeg 不支持 libx264 编码器\n\n' +
      '请按以下步骤修复：\n' +
      '1. 确认 backend/assets/ffmpeg/ffmpeg.exe 存在\n' +
      '2. 确认 ffmpeg 是带 libx264 的版本（运行 ffmpeg -encoders | grep libx264）\n' +
      '3. 如缺失 libx264，请重新安装 FFmpeg 或单独编译 libx264\n\n' +
      '修复后重新启动本程序。'
  }

  // 写独立启动错误文件（含 stderr 全量 + 关键 env + 进程状态），便于用户反馈问题
  const errorLogPath = await writeStartupErrorLog('后端启动失败', {
    isDev,
    env: {
      SHORTVIDEO_DATA_DIR: process.env.SHORTVIDEO_DATA_DIR ?? null,
      SHORTVIDEO_TOOL_RUNTIME: process.env.SHORTVIDEO_TOOL_RUNTIME ?? null,
      PYTHONIOENCODING: process.env.PYTHONIOENCODING ?? null,
    },
    backendProcess: backendProcess
      ? {
          pid: backendProcess.pid,
          killed: backendProcess.killed,
          exitCode: backendProcess.exitCode,
          signalCode: backendProcess.signalCode,
        }
      : null,
    port: BACKEND_PORT,
    stderr_total: lastStderr,
    stderr_tail: lastStderr.slice(-800),
  })

  // 错误框 detail 末尾追加日志路径（不论写成功与否都引导用户去 logs 目录）
  const logHint = errorLogPath
    ? `\n\n详细信息已写入日志：\n${errorLogPath}`
    : `\n\n详细信息请查看主进程日志：\n${path.join(app.getPath('logs'), 'shortvideo-tool', 'main-*.log')}`

  // #data-dir-relaunch：错误框加重试按钮——首次启动 on_startup 含 2 个 ffmpeg subprocess
  // + 数据迁移，首次跑 10-20s 才监听端口，30s 超时仍可能不够；给用户一次"再等 30s"机会
  const choice = await dialog.showMessageBox({
    type: 'error',
    title: '启动失败',
    message: '后端服务无法启动',
    detail: detail + (lastStderr ? `\n\n后端输出：\n${lastStderr.slice(-800)}` : '') + logHint,
    buttons: ['再等一会', '退出'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  })

  if (choice.response === 0) {
    // 再探活 30s（首次启动 ffmpeg 检查卡 ~20s 时给用户兜底）
    logDev('[Main] 用户选择「再等一会」，重探活 30s')
    if (await waitForBackendReady(30000)) {
      logDev('[Main] 重探活成功，后端已就绪')
      return true
    }
    logWarn('[Main] 重探活仍失败，弹窗用户选第二个「再等一会」可能无限循环，强制退出')
    // 兜底：再弹一次（不再加重试按钮，避免死循环）
    await dialog.showMessageBox({
      type: 'error',
      title: '启动失败',
      message: '后端服务仍无法启动',
      detail: '已等待超过 60 秒仍未就绪。\n请检查后端日志后重试。' + logHint,
      buttons: ['确定'],
      defaultId: 0,
      noLink: true,
    })
  }

  // 退出前：杀掉后端进程树 + 清端口（用户/模型加"进程还挂着 → 自动结束掉"）
  // 旧版只 stopBackend（SIGTERM）+ app.quit，进程可能挂死留僵尸。
  isQuitting = true
  await forceKillBackend()
  // 兜底等端口释放（forceKillBackend 内 taskkill 是 fire-and-forget，
  // LISTENING 状态可能在进程退出后还有 100-200ms 残留）
  const portFree = await waitForPortFree(BACKEND_PORT, 3000)
  if (!portFree) {
    logWarn(`[Main] forceKillBackend 后端口 ${BACKEND_PORT} 3s 仍未释放，可能有手动启的孤儿`)
  }
  app.quit()
  return false
}

function stopBackend() {
  // 同步兜底版本：仅发 SIGTERM（Windows 走 TerminateProcess），不等子进程真死。
  // 强杀（含进程树 + 端口兜底）请用异步的 forceKillBackend()，适用于用户主动退/重启/启动失败场景。
  if (backendProcess) {
    backendProcess.kill()
    backendProcess = null
  }
}

/** 创建主窗口（1280×860 可调，界面交互设计 9.1.1） */
function createWindow() {
  Menu.setApplicationMenu(null)

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1100,
    minHeight: 720,
    autoHideMenuBar: true,
    title: '短视频工具',
    webPreferences: {
      preload: path.join(__dirname, '../preload/preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  })

  if (isDev) {
    // 开发模式加载构建产物（npm run dev 先完成三段构建；需热更时另行引入 vite dev server）
    mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
  }

  // 渲染层 console 转发到主进程 stdout（便于无头排查 UI 问题）
  // #114：Electron 27+ 新签名 (event, level, message, line, sourceId)；
  // level 是数字（0=verbose, 1=info, 2=warning, 3=error），按级别走不同日志通道
  mainWindow.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    const lvlName = ['VERBOSE', 'INFO', 'WARN', 'ERROR'][level] ?? 'INFO'
    const loc = sourceId ? ` (${sourceId}:${line})` : ''
    const text = `[Renderer] [${lvlName}]${loc} ${message}`
    if (level >= 2) {
      // warn/error 直接走 errorDev（不被 isDev 守卫，生产落盘）
      errorDev(text)
    } else {
      logDev(text)
    }
  })

  // #412：Ctrl+F12 切换 DevTools（dev/prod 都生效；用户反馈生产环境排查 UI 问题需要）
  // dev 模式已自动 openDevTools()，按 F12 可关闭；prod 模式按 F12 打开
  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.type !== 'keyDown') return
    // Control + F12（Mac/Win/Linux 通杀）
    if (input.key === 'F12' && (input.control || input.meta)) {
      mainWindow?.webContents.toggleDevTools()
    }
  })

  mainWindow.once('ready-to-show', () => mainWindow?.show())

  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault()
      // #411：关闭主窗口时二次确认（防误触 / 误关导致正在跑的后台任务被打断）
      void confirmClose()
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

/** 兜底杀端口任意占用进程（用于 dev 模式用户手动启的后端 / 跨进程孤儿）。
 *  Windows 用 taskkill /F /PID；非 Windows 留空（用户场景 99% Win）。
 *  异步 + fire-and-forget；调用方负责等端口空闲。 */
function killPortOccupants(port: number): Promise<void> {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') {
      resolve()
      return
    }
    const { exec } = require('child_process') as typeof import('child_process')
    exec(
      `for /f "tokens=5" %a in ('netstat -ano ^| findstr :${port} ^| findstr LISTENING') do taskkill /F /PID %a`,
      () => resolve(),  // 不管成功失败都 resolve（taskkill 找不到 PID 也算成功）
    )
  })
}

/** 等端口空闲（maxMs 内），超时返回 false */
async function waitForPortFree(port: number, maxMs: number): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < maxMs) {
    const alive = await new Promise<boolean>((resolve) => {
      const req = net.request(`http://127.0.0.1:${port}/api/health`)
      const timer = setTimeout(() => { req.destroy(); resolve(false) }, 500)
      req.on('response', () => { clearTimeout(timer); req.abort(); resolve(true) })
      req.on('error', () => { clearTimeout(timer); resolve(false) })
      req.end()
    })
    if (!alive) return true
    await new Promise((r) => setTimeout(r, 200))
  }
  return false
}

/** 应用退出：关调度器（经后端 shutdown 钩子）→ 强杀后端进程树 → 清端口 */
async function shutdownApp() {
  if (isQuitting) return
  isQuitting = true
  // 强杀整棵进程树（含 uvicorn/ffmpeg 子进程）；forceKillBackend 内部已包含 killPortOccupants 兜底
  await forceKillBackend()
  mainWindow?.destroy()
  app.quit()
}

/** #411：关闭主窗口前的二次确认（防误触 / 防误关正在跑的后台任务）。
 * 用户点"确认退出"才真正走 shutdownApp；点"取消"或关弹窗 → 不退出。
 */
async function confirmClose(): Promise<void> {
  if (!mainWindow) return
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: 'warning',
    title: '退出确认',
    message: '确定要退出短视频工具吗？',
    detail: '正在运行的后台任务（如视频生成/发布/拉取）会被中断，'
      + '未保存的进度可能丢失。建议先等待当前任务完成。',
    buttons: ['确认退出', '取消'],
    defaultId: 1,  // 默认聚焦"取消"（防回车误退）
    cancelId: 1,
    noLink: true,
  })
  if (response === 0) {
    void shutdownApp()
  }
}

// ===== IPC 处理器（渲染层经 preload 调用） =====

ipcMain.handle('dialog:openDirectory', async (_, defaultPath?: string) => {
  if (!mainWindow) return null
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    defaultPath: defaultPath || undefined,
  })
  return result.canceled ? null : result.filePaths[0]
})

ipcMain.handle('dialog:openFile', async (_, filters, defaultPath?: string) => {
  if (!mainWindow) return null
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openFile', 'multiSelections'],
    filters: filters || [{ name: '所有文件', extensions: ['*'] }],
    defaultPath: defaultPath || undefined,
  })
  return result.canceled ? null : result.filePaths
})

ipcMain.handle('dialog:openFolder', async (_, defaultPath?: string) => {
  if (!mainWindow) return null
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    defaultPath: defaultPath || undefined,
  })
  return result.canceled || !result.filePaths[0] ? null : result.filePaths[0]
})

ipcMain.handle('dialog:saveFile', async (_, defaultName: string, filters) => {
  if (!mainWindow) return null
  const result = await dialog.showSaveDialog(mainWindow, {
    defaultPath: defaultName,
    filters: filters || [{ name: '备份文件', extensions: ['zip'] }],
  })
  return result.canceled || !result.filePath ? null : result.filePath
})

ipcMain.handle('shell:openPath', async (_, filePath: string) => {
  // 打开文件所在目录并选中：showItemInFolder 在各平台都会打开所在目录，无需先 openPath
  shell.showItemInFolder(filePath)
})

// #418：定位到目录（直接打开该目录，不会跳到父目录）；前端 wizard 点击目录图标用
ipcMain.handle('shell:openDirInExplorer', async (_, dirPath: string) => {
  // shell.openPath 在 Windows 上对目录会打开该目录；macOS 同；Linux 打开文件管理器
  await shell.openPath(dirPath)
})

ipcMain.handle('app:getBackendPort', () => BACKEND_PORT)

// 外链用系统默认浏览器打开（仅放行 http/https，防 file:// 等协议滥用）
// #116：安全审计日志——记录调用方与 URL，便于追溯可疑跳转
ipcMain.handle('shell:openExternal', async (_, url: string) => {
  if (!/^https?:\/\//i.test(url)) {
    logWarn(`[Main] 拒绝外链请求（协议不符）: ${url}`)
    throw new Error('仅支持 http/https 链接')
  }
  logDev(`[Main] 打开外链: ${url}`)
  await shell.openExternal(url)
})

// #116：walkDir 入口与异常审计
ipcMain.handle('fs:walkDir', async (_, dirPath: string) => {
  const out: string[] = []
  const walk = (dir: string) => {
    try {
      for (const name of fs.readdirSync(dir)) {
        const full = path.join(dir, name)
        try {
          if (fs.statSync(full).isDirectory()) walk(full)
          else out.push(full)
        } catch (e) {
          // 单文件 stat 失败不影响整批；常见：符号链接悬空、权限拒绝
          logDev(`[Main] walkDir stat 失败 ${full}: ${(e as Error).message}`)
        }
      }
    } catch (e) {
      errorDev(`[Main] walkDir 读取目录失败 ${dir}`, e)
      throw e
    }
  }
  walk(dirPath)
  return out
})

// ===== #data-dir-choice：数据目录选择与持久化 =====
/** SettingsPage「更改数据目录」用：选目录 → 写 userData → 返回新路径（不重启） */
ipcMain.handle('settings:chooseDataDir', async () => {
  if (!mainWindow) return null
  const cur = loadPreferredDataDir()
  const result = await dialog.showOpenDialog(mainWindow, {
    title: '选择数据目录',
    defaultPath: cur && validateDataDir(cur).ok ? cur : undefined,
    properties: ['openDirectory', 'createDirectory'],
    message: '将用于存储数据库、素材、成品视频等全部本地数据。\n可随时在「设置 → 数据目录」中更改（更改后需重启生效）。',
  })
  if (result.canceled || result.filePaths.length === 0) return null
  const chosen = result.filePaths[0]
  const v = validateDataDir(chosen)
  if (!v.ok) {
    logWarn(`[Main] 用户选择的目录不可用: ${chosen} (${v.reason})`)
    throw new Error(`所选目录不可用：${v.reason}`)
  }
  writePreferredDataDir(chosen)
  logDev(`[Main] 已保存首选数据目录: ${chosen}`)
  return { abs_path: chosen }
})

/** 获取当前首选目录（只读） */
ipcMain.handle('settings:getPreferredDataDir', () => {
  const p = loadPreferredDataDir()
  // #data-dir-choice：审计日志（IPC 频次低：mount + focus/visibility 触发；落盘便于排查）
  logDev(`[Main] getPreferredDataDir → ${p ? 'set' : 'null'}`)
  return p ? { abs_path: p } : null
})

/** 获取默认盘探测结果（首次启动弹窗 / 设置页"恢复默认"用） */
ipcMain.handle('settings:defaultDataDirHint', () => {
  const hint = pickDefaultDataDirHint()
  // #data-dir-choice：审计日志（高频调用，只记 disk 字母避免长路径刷屏）
  logDev(`[Main] defaultDataDirHint → ${hint ? `${hint.disk} 盘 (${hint.abs_path.length} chars)` : 'null'}`)
  return hint
})

/** 重启 Electron（设置页"立即重启"按钮用）。
 *  参数构造抽到 buildRelaunchArgs() 便于单测覆盖 dev/prod 双轨。
 *  重启前 forceKillBackend 杀整棵进程树（含 uvicorn 启的 ffmpeg 子进程）+ 兜底清端口，
 *  避免新进程复用旧实例导致数据目录不生效（#data-dir-relaunch）。 */
ipcMain.handle('app:relaunch', async () => {
  logDev('[Main] 应用重启（先杀占端口进程避免复用）')
  await forceKillBackend()
  const portFree = await waitForPortFree(BACKEND_PORT, 3000)
  if (!portFree) {
    // 极端：taskkill 失败 / 端口被系统服务占用 → 不 relaunch（否则复用旧实例，bug 未修）
    // 让用户知道要手动清端口，再重试
    errorDev(`[Main] 端口 ${BACKEND_PORT} 3s 未释放，relaunch 中止`)
    await dialog.showMessageBox({
      type: 'error',
      title: '重启失败',
      message: `端口 ${BACKEND_PORT} 仍被其他进程占用，无法立即重启。`,
      detail: '可能原因：手动启动的旧版后端 / 系统服务占用。\n' +
        '请在任务管理器结束占用该端口的进程后，再次点击「立即重启」。\n' +
        '（不会自动重启避免复用旧实例导致数据目录不生效）',
      noLink: true,
    })
    return
  }
  app.relaunch(buildRelaunchArgs(app))
  app.exit(0)
})

// ===== 应用生命周期 =====
if (!hasSingleInstanceLock) {
  // #16：双击图标无反应时用户会误以为程序挂了，留痕便于排查
  console.log('[Main] 检测到已有实例运行，本进程退出')
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  app.whenReady().then(async () => {
    // #data-dir-choice：首次启动前确认数据目录；返回路径或 null（用户取消）
    const dataDir = await ensureDataDirChoice()
    if (dataDir === null) {
      // 取消场景：已弹过错误框，直接退出
      logWarn('[Main] 用户取消数据目录选择，退出启动')
      app.exit(1)
      return
    }
    await startBackend(dataDir)
    // #382：等待后端就绪，10 秒内 health 不通则视为启动失败
    const ready = await waitForBackendReady(30000)
    if (!ready) {
      errorDev('[Main] 后端启动超时，30s 内 health 未通，触发错误弹窗（带重试）')
      const recovered = await handleBackendStartupFailure()
      if (!recovered) return
    }
    // 后端就绪后创建主窗口（延迟 1.5s 让 health 完全稳定）
    setTimeout(createWindow, 1500)
  })

  app.on('window-all-closed', () => {
    if (!isQuitting) void shutdownApp()
    if (process.platform !== 'darwin') app.quit()
  })

  app.on('before-quit', () => {
    isQuitting = true
    stopBackend()
  })
}
