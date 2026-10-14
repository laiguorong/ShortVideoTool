import { app, BrowserWindow, ipcMain, dialog, shell, Menu, net, session } from 'electron'
import {
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

/** 匹配 ANSI 控制序列（CSI + OSC），顶层常量避免每次调用重新编译正则
 * - CSI: ESC [ <params 0x30-0x3F> <intermediates 0x20-0x2F> <final 0x40-0x7E>
 * - OSC: ESC ] <payload> BEL 或 ESC ] <payload> ESC \
 *   payload 排除 BEL 与 ESC，匹配到终止符为止，避免吃后续内容 */
const ANSI_RE = /\x1b\[[\x30-\x3F]*[\x20-\x2F]*[\x40-\x7E]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g

/** 剥离 ANSI 控制序列（CSI 颜色 + cursor 控制 + OSC 标题），文件日志保留纯文本 */
function _stripAnsi(s: string): string {
  return s.replace(ANSI_RE, '')
}

/** ISO 8601 本地时间 + 时区偏移：YYYY-MM-DDTHH:mm:ss.SSS+08:00。
 * 用 getFullYear 等本地字段（不用 toISOString 避免拿到 UTC），与后端 loguru `{time}ZZ` 对齐。 */
function _localIsoStamp(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms = String(d.getMilliseconds()).padStart(3, '0')
  const offsetMin = -d.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMin)
  const tzStr = `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`
  return `${y}-${m}-${day}T${hh}:${mm}:${ss}.${ms}${tzStr}`
}

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
    // 启动时追加一行分隔，方便 grep；时间戳格式与 _writeLog 一致
    fs.appendFileSync(file, `\n--- start ${_localIsoStamp(now)} (isDev=${isDev}) ---\n`, 'utf8')
    _logFilePath = file
    _logFileDate = ymd
    return file
  } catch (e) {
    // 日志目录创建失败不阻塞主流程
    return null
  }
}

/** 写文件日志：strip ANSI + 折叠内部换行保持每条单行，便于 grep/解析。
 * console 走原始 message（含 ANSI 与换行，dev 终端展示更直观）。 */
function _writeLog(level: 'INFO' | 'WARN' | 'ERROR', message: string): void {
  const singleLine = _stripAnsi(message).replace(/[\r\n]+/g, ' ')
  const line = `[${_localIsoStamp(new Date())}] [${level}] ${singleLine}\n`
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

/** 端口占用查询（#port-probe 2026-10-14）：返回端口上是否有 LISTENING 进程。
 * 用 netstat LISTENING 行判定，比 HTTP 探活更早命中——uvicorn bind 端口的瞬间就 true，
 * 不需要等到 FastAPI 路由 ready。
 * 仅用于启动期"端口是否被占用"的快速判断，不替代 /api/health heartbeat 校验
 * （heartbeat 判断"后端服务是否正常工作"必须看 HTTP 返 200）。
 * Windows-only：项目打包目标平台仅为 Windows；非 Windows 留 false（与 killPortOccupants 守门一致）。 */
function isPortOccupied(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') {
      resolve(false)
      return
    }
    const { exec } = require('child_process') as typeof import('child_process')
    exec(
      `netstat -ano | findstr :${port} | findstr LISTENING`,
      (_e, stdout) => {
        resolve(typeof stdout === 'string' && stdout.trim().length > 0)
      },
    )
  })
}

/** 单次心跳探活：HTTP /api/health，200ms 超时（#heartbeat-poll-200 2026-10-14）。
 * 用途：
 *   - 步骤 1.5 二次验证（最多循环 3 次，确认是自家 FastAPI 不是别的占端口服务）
 *   - 步骤 3 / 步骤 6 总 5s 期间的循环探活
 * 单次 200ms 超时远比 1s 激进——通的路径（HTTP 头到达）几 ms 内 resolve true，
 * 不通的路径（连接被拒 / 端口 LISTENING 但路由没起）200ms 内 false。
 * 循环节奏靠外层 waitForBackendReady / 步骤 1.5 控；本函数本身不在内循环。 */
async function isBackendAlive(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = net.request(`http://127.0.0.1:${BACKEND_PORT}/api/health`)
    const HEARTBEAT_SINGLE_TIMEOUT_MS = 200
    const timer = setTimeout(() => {
      req.destroy()
      // 超时不记 ERROR（启动期间属正常等待），仅 debug 留痕
      logDev(`[Main] health 探活超时（端口 ${BACKEND_PORT}, ${HEARTBEAT_SINGLE_TIMEOUT_MS}ms 单次）`)
      resolve(false)
    }, HEARTBEAT_SINGLE_TIMEOUT_MS)
    req.on('response', (res) => {
      clearTimeout(timer)
      res.resume()
      // HTTP 头一到达就触发 response 事件，resolve 同步执行——通的路径不等 200ms 超时
      resolve(res.statusCode === 200)
    })
    req.on('error', (e) => {
      clearTimeout(timer)
      // #113：e 是 Error，无 code；NodeJS.ErrnoException 才有 code
      const code = (e as NodeJS.ErrnoException).code
      logDev(`[Main] health 探活失败: ${code ?? e.message}`)
      // 连接被拒等错误也是瞬时的，立即 resolve(false) 不等 200ms 超时
      resolve(false)
    })
    req.end()
  })
}

/** 拉起后端：生产用内置 exe，开发用系统 Python + uvicorn。
 * opts.skipPortCleanup = true：跳过前置 kill+wait+throw（用于"已知端口空闲"的二级重启调用）。
 * 默认 = 完整清理逻辑（兜底杀端口任意占用 → 3s 内未释放就抛错）。 */
async function startBackend(dataDir: string | null = null, opts: { skipPortCleanup?: boolean } = {}) {
  if (!opts.skipPortCleanup) {
    // #data-dir-relaunch：主动清理端口任意占用进程（dev 手动启 / NSIS 覆盖安装残留 / 旧 Electron 孤儿）
    // 旧版"复用现有实例"的设计在 NSIS 覆盖安装场景下有 bug——旧后端 DATA_DIR/版本可能已过时，
    // 复用会导致"改了数据目录但 SettingsPage 显示旧值"或"两个进程并存"。统一为 spawn 新实例。
    await killPortOccupants(BACKEND_PORT)
    const portFree = await waitForPortFree(BACKEND_PORT, 3000)
    if (!portFree) {
      errorDev(`[Main] 端口 ${BACKEND_PORT} 3s 未释放，startBackend 放弃`)
      throw new Error(`端口 ${BACKEND_PORT} 被占用且无法释放`)
    }
  }

  const bundled = getBundledBackendBinary()

  // #179：Windows 默认 cmd codepage 是 GBK（936），Python 子进程 stdout 按此编码，
  // Node.js 端按 UTF-8 解码会乱码。强制 PYTHONIOENCODING=utf-8 走 UTF-8 字节流，
  // Electron 主进程侧 .toString() 默认 UTF-8 解码正确。
  // PyInstaller 打包的 exe 同样识别此 env。
  // 仅 Windows 平台需要强制：POSIX 下 Python 3.12 启动本就 UTF-8，加 '1' 无意义但可能影响 subprocess.Popen 默认编码语义。
  const isWin = process.platform === 'win32'
  // PEP 540 规定 PYTHONUTF8 值必须是字符串 '1'/'0'，数字会被 Python 静默忽略
  const PYTHONUTF8_VALUE = '1'
  const backendEnv: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    ...(isWin ? { PYTHONUTF8: PYTHONUTF8_VALUE } : {}),  // #179：PEP 540 UTF-8 mode，覆盖 open()/sys.stdout 默认编码
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
    // 指向 extraResources 拷入的 chromium 目录，避免走默认 %LOCALAPPDATA%\ms-playwright
    const msPlaywrightPath = path.join(path.dirname(bundled), 'assets', 'ms-playwright')
    logDev(`[Main] 启动内置后端: ${bundled} --port ${BACKEND_PORT}${dataDir ? ` --data-dir ${dataDir}` : ''} (PLAYWRIGHT_BROWSERS_PATH=${msPlaywrightPath})`)
    backendProcess = spawn(
      bundled,
      ['--port', String(BACKEND_PORT), '--host', '127.0.0.1', ...dataDirArg],
      {
        cwd: path.dirname(bundled),
        stdio: ['ignore', 'pipe', 'pipe'],
        // baseEnv 已含 PYTHONUTF8（仅 Windows，见 backendEnv 构造处），无需重复
        env: { ...baseEnv, PLAYWRIGHT_BROWSERS_PATH: msPlaywrightPath },
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

  backendProcess.stdout?.on('data', (data) => {
    // 按 \n 拆：data 事件可能合并多条 stdout 行，保留中间换行会导致
    // 写入日志文件时后续行变成裸 ANSI 序列（无 [Backend] 包装，文本编辑器渲染为方框）
    // 只按 \n 拆（不用 /\r?\n/）：后端 _safe_stderr_sink 走 sys.stderr TextIOWrapper 写 \n，
    // Python 默认 binary mode 不做 \n → \r\n 转译
    for (const line of data.toString().split('\n')) {
      if (line) logDev(`[Backend] ${line}`)
    }
  })
  // #382：stderr 累积到全局 buffer，启动失败弹窗展示（替换原 dev-only log）
  backendProcess.stderr?.on('data', (data) => {
    const text = data.toString()
    // 剥 ANSI 后入 buffer，弹窗展示纯文本（避免 `[32m...[0m` 字面渲染）
    const sanitized = _stripAnsi(text)
    backendStderrBytes += sanitized.length
    backendStderrBuffer.push(sanitized)
    // #109：原 join('').length 是 O(n²)；改按 totalBytes 判定，超过限制按 FIFO 丢弃。
    while (backendStderrBytes > STDERR_BUFFER_LIMIT && backendStderrBuffer.length > 0) {
      const dropped = backendStderrBuffer.shift()!
      backendStderrBytes -= dropped.length
    }
    // #backend-log-level（2026-10-14）：之前所有 stderr 都走 errorDev，但 Python uvicorn 默认 INFO 日志
    // 也输出到 stderr（uvicorn 不像多数 CLI 那样把 INFO 分流到 stdout），导致 `[ERROR] [Backend] INFO: ...`
    // 这种矛盾日志。改成所有 [Backend] 走 logDev（INFO 级别），让原文本里的 `ERROR:` / `ERROR |`
    // 字符自己传达严重性 —— grep 或颜色仍可定位。
    // 未来如果需要保留分级（stdout/WARN/ERROR 分流）：解析行首的 `LEVEL:` (uvicorn 格式) 或
    // loguru 的 `| LEVEL |` 模式，按级别分派到 logDev/logWarn/errorDev。
    for (const line of sanitized.split('\n')) {
      if (line) logDev(`[Backend] ${line}`)
    }
  })
  backendProcess.on('error', (error) => errorDev(`[Backend] 启动失败: ${error.message}`))
  backendProcess.on('close', (code) => {
    // code=null 通常是被外部信号强杀（任务管理器/Electron 关闭父进程/Stop-Process 等），
    // 直接展示「null」容易让用户误以为是代码 bug，改成可读提示。
    const desc = code === null ? '被外部强制终止' : String(code)
    logDev(`[Backend] 退出 ${desc}`)
  })
}

/** 检测后端在 N 毫秒内是否能响应 health 接口；不可达返 Promise<false>。
 * 用于 #382：环境检查失败导致后端快速退出时，触发用户提示。
 *
 * #heartbeat-poll-200（2026-10-14 用户拍板）：
 *   - 单次 isBackendAlive 探活上限 200ms（通的路径几 ms resolve true；不通 200ms 内 resolve false）
 *   - 总超时 timeoutMs 期间**循环请求**心跳接口——不通立即 retry，不额外 sleep
 *   - 不通路径：每轮回 ≈ 200ms（单次卡满），5s 总预算约 25 次机会
 *   - 通路径：首次 resolve true 就 return（不消耗更多时间） */
async function waitForBackendReady(timeoutMs: number): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await isBackendAlive()) return true
    // 不通了立即 retry（单次 200ms 卡住本身已经是节奏控制，不需要额外 sleep）
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
  // #597 重构 + #598 整套删 userData 持久化：
  // - 启动时由后端 check_data_dir 走 env / settings.json 配置检测（都没配置时
  //   返 need_choose=true 触发启动页调本函数），启动流程不读 userData
  // - 本弹窗函数也**不读** userData"静默沿用持久化"（preferredDataDir 已整套删），
  //   弹窗每次都从默认盘开始让用户重新选

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
      logDev(`[Main] 使用默认数据目录: ${hint.abs_path}`)
      return hint.abs_path
    }
    if (next.action === 'confirm') {
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

  // 写启动错误日志（含 stderr 全量 + 关键 env + 进程状态），便于用户反馈问题
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
  errorDev(`[Main] 后端启动失败，stderr 最近 ${lastStderr.length} 字符：\n${lastStderr.slice(-4096)}`)

  // 错误框 detail 末尾追加日志路径
  const logHint = errorLogPath
    ? `\n\n详细信息已写入日志：\n${errorLogPath}`
    : `\n\n详细信息请查看主进程日志：\n${path.join(app.getPath('logs'), 'shortvideo-tool', 'main-*.log')}`

  // 启动页 (StartupPage) 会通过 /api/startup/check 给出结构化错误。
  // 这里只负责让用户知道后端进程没起来 + 让出选择（退出）。
  const choice = await dialog.showMessageBox({
    type: 'error',
    title: '后端服务无法启动',
    message: '后端服务无法启动',
    detail:
      '后端进程在 30 秒内未能响应健康检查。\n\n' +
      '可能原因：端口被占用 / 后端进程启动失败 / 数据目录不可访问。\n\n' +
      '详细诊断信息将通过启动页 7 步检查展示。' +
      logHint,
    buttons: ['继续启动（查看诊断）', '退出'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  })

  if (choice.response === 0) {
    // 用户选"继续启动" → 不要杀后端，让 StartupPage 接管诊断
    // 原因：杀后端后 7 步 check 第一步就 fail（init_data_dir / settings 都依赖后端活着），
    //      用户只能重启，等于强制退出。保留后端让 StartupPage 跑出真实失败步骤的原因更友好。
    logDev('[Main] 用户选择继续启动，启动页将展示诊断信息')
    return true
  }

  // 用户选"退出" → 杀进程 + 退
  isQuitting = true
  await forceKillBackend()
  const portFree = await waitForPortFree(BACKEND_PORT, 3000)
  if (!portFree) {
    logWarn(`[Main] forceKillBackend 后端口 ${BACKEND_PORT} 3s 仍未释放`)
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

  // 启动即最大化窗口（每次启动固定最大化，不记忆上次状态）
  // maximize() 必须在 show() 前调用以避免从 1280×860 闪一下再放大的视觉跳变
  mainWindow.once('ready-to-show', () => {
    mainWindow?.maximize()
    mainWindow?.show()
  })

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

// ===== #data-dir-choice：数据目录选择（#598 整套删 userData 持久化） =====
/** SettingsPage「更改数据目录」用：
 *  - 弹 dialog 选目录
 *  - 校验存在 + 是目录 + 可写
 *  - 主进程直接 fetch 后端 POST /api/startup/data-dir 写 settings.json + init_data_dir
 *  - 返回新路径
 *  历史 bug 修复：原本实现"写 userData + 弹重启"——但**根本不写 settings.json**，
 *  重启后 check_data_dir 读 settings.json 拿旧 data_dir → 沿用旧目录 → 改目录实际不生效。
 *  现在写后端 settings.json 才是真正生效。重启后整个后端从头 init 走新路径。 */
ipcMain.handle('settings:chooseDataDir', async () => {
  if (!mainWindow) return null
  const hint = pickDefaultDataDirHint()
  const result = await dialog.showOpenDialog(mainWindow, {
    title: '选择数据目录',
    defaultPath: hint?.abs_path,
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
  // #598：走 /api/settings/data-dir-prepare 端点（只写 settings.json，不调 init_data_dir）。
  // 避免运行时改 DATA_DIR 全局导致业务引用旧路径派生 key（数据漂移）。
  // 重启后整个后端从头 init 走新 settings.json 才真正生效。
  try {
    const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/settings/data-dir-prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ new_dir: chosen }),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      errorDev(`[Main] 写后端 settings.json 失败: ${res.status} ${text.slice(0, 200)}`)
      throw new Error(`保存数据目录失败（HTTP ${res.status}）`)
    }
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('保存数据目录失败')) throw e
    errorDev(`[Main] 写后端 settings.json 异常`, e)
    throw new Error(`保存数据目录失败：${(e as Error).message}`)
  }
  logDev(`[Main] 已保存数据目录到 settings.json: ${chosen}`)
  return { abs_path: chosen }
})

/** #597：启动页触发的主进程数据目录选择完整流程（默认盘探测 + 5 次重试 + retry-or-quit）。
 *  返回用户最终选择的路径（默认盘 / 立即选择 / 取消返回 null）。 */
ipcMain.handle('dataDir:choose', async (): Promise<{ abs_path: string } | null> => {
  logDev('[Main] IPC dataDir:choose 触发 ensureDataDirChoice 弹窗')
  const chosen = await ensureDataDirChoice()
  if (!chosen) return null
  return { abs_path: chosen }
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
    // #597 重构：删除主进程 ensureDataDirChoice 自动弹窗
    // - 启动页先渲染（phase=waiting-backend），等 health 通后跑 6 步 check
    // - data_dir 步骤如未配置会返 failed + need_choose=true，启动页自动调 IPC 触发主进程 ensureDataDirChoice 弹窗
    // - 不再静默沿用 preferred_data_dir.json（明确每次启动都让用户感知数据目录）
    logDev('[Main] 创建主窗口（启动页先显示，后端后台启动）')
    createWindow()

    // 后端启动编排（2026-10-14 #user-flow-rewrite + #port-probe + 步骤日志）：
    //   步骤 1）端口占用探活（netstat LISTENING）
    //   步骤 1.5）HTTP 二次验证（确认是 FastAPI，不是别的服务占着同端口）
    //   步骤 2）端口空闲 → 异步启后端（不等 ready）
    //   步骤 3）等待 heartbeat ready（HTTP /api/health 200），5s 内不通视为启动失败
    //   步骤 4）杀自己 spawn 的后端 + 清端口任意占用
    //   步骤 5）重启后端
    //   步骤 6）再 5s heartbeat，仍不通就弹错误对话框
    //
    // #port-probe-verify（2026-10-14）：netstat LISTENING 只是必要条件，不是充分条件
    //   （实测 EIS.Toolbox 等外部服务占着 8765 不响应 HTTP）。所以步骤 1 命中后必须做 HTTP 二次验证：
    //     - 通：确认为自家后端 → 复用
    //     - 不通：占着的是别的服务 → 杀端口 + 走步骤 2 spawn
    //
    // 日志约定：每步起一条 logDev（`步骤 N: ...`），结束一条 ✓/✗ 总结，方便排错时 grep。
    //
    // 设计取舍：
    //   - 步骤 1 用 netstat 而非 HTTP 探活：netstat LISTENING 在 uvicorn bind 端口瞬间就成立，
    //     HTTP 探活则要等 FastAPI 路由 ready（毫秒到秒级窗口）——这段时间误判会让前端以为没人占着
    //     进而 spawn 自己杀同端口进程。netstat 完美契合"端口被占用"的字面语义。
    //   - heartbeat 仍走 HTTP（看 /api/health 返 200）：这是判断"后端服务是否正常工作"的真探针，
    //     netstat LISTENING 只能反映"进程在 listen"，不能反映"应用层能否响应"
    //   - 复用命中时 backendProcess = null → forceKillBackend / shutdownApp 关闭不杀这个进程
    //   - 杀+重启分支只在 spawn 后 timeout 才走，不在 spawn 前主动 kill（不与 NSIS 残留冲突）

    // 步骤 1：端口占用探活（netstat LISTENING）
    // #port-probe-verify（2026-10-14）：netstat 命中只是必要条件，不充分——
    // 别的服务也可能 LISTENING 在同一端口（实测：C:\EIS\4.9.33.0\EIS.Toolbox.exe 占着 8765，
    // 全程不响应 HTTP → 前端 fetch 5s 超时 → 失败 10 次 toast "后端响应异常"）。
    // 所以步骤 1.5 必须用 isBackendAlive() 做 HTTP 二次验证：
    //   - 通：确认为自家 FastAPI → 复用
    //   - 不通：占着的是别的服务 → 杀端口 + 走步骤 2 spawn
    logDev(`[Main] 步骤 1: 端口占用探活 (port=${BACKEND_PORT}, 检测方式=netstat LISTENING)`)
    if (await isPortOccupied(BACKEND_PORT)) {
      logDev(`[Main] 步骤 1 ✓ 端口 ${BACKEND_PORT} 已被占用，做 HTTP 二次验证（确认是 FastAPI 而不是别的服务）`)
      // 步骤 1.5：HTTP 探活循环（#port-probe-verify 2026-10-14）
      // 总超时 HEALTH_VERIFY_TIMEOUT_MS 期间循环请求心跳接口，单次 200ms 不通立即 retry。
      // 复用 waitForBackendReady 函数——与步骤 3 / 步骤 6 行为统一（"总超时内循环"）。
      // 数字 2000ms 选定理由：自家 FastAPI 路由 ready 通常 200-500ms；外部死服务（占端口不响应 HTTP）
      // 200ms × 10 次卡满即 false，2s 总超时足够区分，又不会让用户等太久。
      const HEALTH_VERIFY_TIMEOUT_MS = 2000
      logDev(`[Main] 步骤 1.5: HTTP 二次验证循环 (timeout=${HEALTH_VERIFY_TIMEOUT_MS}ms, 单次探活 200ms, 不通立即 retry)`)
      const healthy = await waitForBackendReady(HEALTH_VERIFY_TIMEOUT_MS)
      if (healthy) {
        logDev(`[Main] 步骤 1.5 ✓ HTTP 探活通过，确认是自家后端，复用`)
        backendProcess = null
        logDev('[Main] 后端就绪（复用），启动页将自动接续 6 步检查')
        return
      }
      logDev(`[Main] 步骤 1.5 ✗ ${HEALTH_VERIFY_TIMEOUT_MS}ms 内 HTTP 始终未通，确认不是 FastAPI，杀端口 + 走步骤 2 spawn`)
      await killPortOccupants(BACKEND_PORT)
      const freedAfterKill = await waitForPortFree(BACKEND_PORT, 5000)
      if (!freedAfterKill) {
        // #review-2（2026-10-14）：杀不掉（系统服务/PID 4 等 taskkill 杀不动）时直接退出。
        // 不要弹窗让用户选 [继续] —— 用户选 [继续] 后会走步骤 2 spawn，但端口还占着，spawn 必失败
        // → 步骤 2 catch 再弹一次 [继续]/[退出] → 用户得连点两次 [退出] 才真退。直接 app.quit 干净。
        errorDev(`[Main] 端口 ${BACKEND_PORT} 被外部服务占用且杀不掉，强制退出`)
        isQuitting = true
        app.quit()
        return
      }
    } else {
      logDev(`[Main] 步骤 1 ✓ 端口 ${BACKEND_PORT} 空闲，需要启动后端`)
    }

    // 步骤 2：端口空闲 → 异步启后端
    logDev('[Main] 步骤 2: 开始异步启动后端进程（不含前置 cleanup）')
    try {
      await startBackend(null)
      logDev('[Main] 步骤 2 ✓ 后端进程已 spawn，进入 heartbeat 等待')
    } catch (e) {
      // startBackend 内部 kill+wait 抛错（端口被外部服务占着且 3s 内未释放）的兜底弹窗
      errorDev('[Main] 步骤 2 ✗ startBackend 抛错:', e)
      const recovered = await handleBackendStartupFailure()
      if (!recovered) return
    }

    // 步骤 3：5s heartbeat 超时 → 杀进程 + 重启
    const HEARTBEAT_TIMEOUT_MS = 5000
    logDev(`[Main] 步骤 3: 等待 heartbeat ready (timeout=${HEARTBEAT_TIMEOUT_MS}ms, 单次探活 200ms, 不通立即 retry)`)
    let ready = await waitForBackendReady(HEARTBEAT_TIMEOUT_MS)
    if (ready) {
      logDev('[Main] 步骤 3 ✓ heartbeat ready，后端服务正常')
    } else {
      logDev(`[Main] 步骤 3 ✗ ${HEARTBEAT_TIMEOUT_MS}ms heartbeat 未通，杀进程重启`)
      // 杀自己 spawn 的（如果还活着）
      if (backendProcess) {
        try {
          backendProcess.kill()
          logDev(`[Main] 步骤 4: 已 kill 后端子进程 (pid=${backendProcess.pid})`)
        } catch (e) {
          errorDev('[Main] 步骤 4 ✗ kill 失败', e)
        }
        backendProcess = null
      }
      // 兜底清端口任意占用（防 dev uvicorn 等外部孤儿干扰）
      await killPortOccupants(BACKEND_PORT)
      // #review-1（2026-10-14）：kill 完不能立即 spawn——Windows 端口释放有延迟（TCP 关闭流程几 ms 到几秒），
      // 直接 spawn uvicorn 会撞 `[Errno 10048] Only one usage of each socket address`。
      // 等端口空闲 5s，不通就报错不进步骤 5（避免"spawn 失败 → catch 弹窗 → 再循环"）。
      // 这与步骤 1.5 杀端口后 waitForPortFree(5000) 行为对齐。
      const freedAfterRestart = await waitForPortFree(BACKEND_PORT, 5000)
      if (!freedAfterRestart) {
        errorDev(`[Main] 步骤 4 ✗ 端口 ${BACKEND_PORT} 杀完仍占，无法重启`)
        const recovered = await handleBackendStartupFailure()
        if (!recovered) return
      }
      logDev('[Main] 步骤 4: 端口清理完毕，准备重启后端')

      // 步骤 5：重新启后端（已知端口已清，跳过 startBackend 的前置 cleanup）
      logDev('[Main] 步骤 5: 重新启动后端进程')
      try {
        await startBackend(null, { skipPortCleanup: true })
        logDev('[Main] 步骤 5 ✓ 后端进程已重新 spawn，进入 heartbeat 再次等待')
      } catch (e) {
        errorDev('[Main] 步骤 5 ✗ startBackend 重试抛错:', e)
        const recovered = await handleBackendStartupFailure()
        if (!recovered) return
      }

      // 步骤 6：再 5s heartbeat，仍不通就报错
      logDev(`[Main] 步骤 6: 再次等待 heartbeat ready (timeout=${HEARTBEAT_TIMEOUT_MS}ms, 单次 200ms)`)
      ready = await waitForBackendReady(HEARTBEAT_TIMEOUT_MS)
      if (ready) {
        logDev('[Main] 步骤 6 ✓ 重启后 heartbeat ready')
      } else {
        logDev('[Main] 步骤 6 ✗ 重启后 heartbeat 仍不通，启动失败')
        const recovered = await handleBackendStartupFailure()
        if (!recovered) return
      }
    }

    logDev('[Main] 后端就绪，启动页将自动接续 6 步检查')
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
