/**
 * 首选数据目录（首次启动选择 / 设置页更改）的持久化与默认盘探测。
 *
 * 落点：app.getPath('userData')/preferred_data_dir.json
 *  - Electron 标准 userData 不漂移；多账号同客户端共享。
 *  - 单字段 { "abs_path": "D:\\..." }；路径无效（不存在/不可写）时主流程回退到弹窗。
 *
 * 与后端 setting_service.resolve_default_data_dir() 互补：
 *  - 后端首次自动选最大固定盘（无 UI）；本模块提供「先弹窗、再持久化」的入口。
 *  - spawn 时透传：
 *      - prod（PyInstaller exe）：--data-dir CLI 参数（argparse 生效）
 *      - dev（uvicorn CLI）：SHORTVIDEO_DATA_DIR env（uvicorn 不认 --data-dir）
 *  - 后端 init_data_dir 显式参数不写 settings.json，避免测试 fixture 临时目录污染兜底配置。
 */
import { app } from 'electron'
import fs from 'fs'
import os from 'os'
import path from 'path'

/** 持久化文件名（userData 根下） */
const PERSIST_FILE_NAME = 'preferred_data_dir.json'

export interface PreferredDataDirRecord {
  /** 用户选定的数据目录绝对路径；空串表示尚未选择 */
  abs_path: string
  /** 首次确认时间（ISO 字符串；仅诊断用） */
  chosen_at?: string
}

/** 持久化文件绝对路径（Electron userData 根下） */
function persistFilePathFor(userDataDir: string): string {
  return path.join(userDataDir, PERSIST_FILE_NAME)
}
function persistFilePath(): string {
  return persistFilePathFor(app.getPath('userData'))
}

/** 读取首选目录；返回 null = 未选定或文件不存在/解析失败/路径不安全。
 *  userDataDir：默认走 Electron app.getPath('userData')；测试可注入临时目录。 */
export function loadPreferredDataDir(userDataDir?: string): string | null {
  const file = persistFilePathFor(userDataDir ?? app.getPath('userData'))
  let raw: string
  try {
    raw = fs.readFileSync(file, 'utf8')
  } catch (e) {
    // ENOENT/权限不足都按"未选定"处理；仅 debug 留痕
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.warn('[PreferredDataDir] 读取失败:', (e as Error).message)
    }
    return null
  }
  try {
    const rec = JSON.parse(raw) as PreferredDataDirRecord
    if (typeof rec.abs_path !== 'string' || rec.abs_path.length === 0) {
      return null
    }
    // #data-dir-validate：校验为绝对路径
    // - 防相对路径（用户手动编辑 JSON / 损坏写入）造成后续 spawn 行为异常
    // - 非绝对路径一律视为损坏，触发 backup + null 返回（让主流程重弹窗）
    if (!path.isAbsolute(rec.abs_path)) {
      console.warn('[PreferredDataDir] 持久化路径非绝对，视为无效:', rec.abs_path)
      return _backupAndNull(file)
    }
    // #data-dir-validate：Windows 额外校验盘符存在（C:\\ / D:\\ ...）
    // 非 Windows 跳过（macOS/Linux fs.existsSync 对不存在根会 false，意义不大）
    if (process.platform === 'win32') {
      const m = /^[A-Za-z]:[\\/]/.exec(rec.abs_path)
      if (!m) {
        console.warn('[PreferredDataDir] Windows 路径缺盘符，视为无效:', rec.abs_path)
        return _backupAndNull(file)
      }
      // driveRoot("C:\\foo\\bar") -> "C:\\"；固定 3 字符（前缀正则保证）
      const root = driveRoot(rec.abs_path)
      if (!fs.existsSync(root)) {
        console.warn('[PreferredDataDir] 盘符不可达，视为无效:', root)
        return _backupAndNull(file)
      }
    }
    return rec.abs_path
  } catch (e) {
    return _backupAndNull(file)
  }
}

/** 内部：损坏文件备份为 .broken，返回 null */
function _backupAndNull(file: string): null {
  try {
    fs.renameSync(file, file + '.broken')
  } catch (renameErr) {
    console.warn('[PreferredDataDir] 损坏文件备份失败:', (renameErr as Error).message)
  }
  return null
}

/** 取 Windows 盘符根（"C:\\foo\\bar" -> "C:\\"）。
 *  前置条件：路径已通过 `/^[A-Za-z]:[\\/]/` 校验，保证至少 3 字符。
 *  - 不在 Windows 平台调用本函数，行为未定义。 */
function driveRoot(absPath: string): string {
  return absPath.slice(0, 3)
}

/** 写入首选目录（先写 .tmp 再 rename，原子替换）。
 *  userDataDir：默认走 Electron app.getPath('userData')；测试可注入临时目录。 */
export function writePreferredDataDir(absPath: string, userDataDir?: string): void {
  const rec: PreferredDataDirRecord = {
    abs_path: absPath,
    chosen_at: new Date().toISOString(),
  }
  const file = persistFilePathFor(userDataDir ?? app.getPath('userData'))
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(rec, null, 2), 'utf8')
  // rename 在同 fs 内是原子替换；写失败时 tmp 残留下次启动会被覆盖
  fs.renameSync(tmp, file)
}

/** 校验路径可用：存在 + 是目录 + 可写（用 'wx' flag 探测创建 + finally 删除） */
export function validateDataDir(absPath: string): { ok: boolean; reason?: string } {
  if (!absPath) return { ok: false, reason: '路径为空' }
  try {
    const stat = fs.statSync(absPath)
    if (!stat.isDirectory()) {
      return { ok: false, reason: '路径不是目录' }
    }
  } catch (e) {
    return { ok: false, reason: `路径不存在（${(e as NodeJS.ErrnoException).code ?? '未知'}）` }
  }
  // 可写性试探：'wx' 失败模式避免覆盖现存文件；random 后缀避免并发/重试冲突
  const rand = Math.random().toString(36).slice(2, 8)
  const probe = path.join(absPath, `.shortvideotool_write_test_${process.pid}_${rand}`)
  let writeOk = false
  let writeErr: Error | null = null
  try {
    // 'wx' = O_EXCL：文件已存在则失败，避免误删用户文件
    fs.writeFileSync(probe, 'ok', { flag: 'wx' })
    writeOk = true
  } catch (e) {
    writeErr = e as Error
  }
  if (writeOk) {
    // 删除失败不影响"可写"判定（已写成功证明有权限；只是清理残留下次再覆盖）
    try { fs.unlinkSync(probe) } catch (rmErr) {
      console.warn(`[PreferredDataDir] probe 清理失败 ${probe}:`, (rmErr as Error).message)
    }
    return { ok: true }
  }
  return {
    ok: false,
    reason: `目录无写入权限（${(writeErr as NodeJS.ErrnoException).code ?? writeErr?.message ?? '未知'}）`,
  }
}

/** 探测默认数据目录：可用空间最大的本地固定盘根 + 子目录名。
 *  镜像后端 setting_service.pick_largest_disk()：仅 DRIVE_FIXED=3 的本地盘。
 *  - Windows：按字母顺序探 A:\\ → Z:\\，用 fs.statfsSync 算 free bytes；
 *    盘类型判断跳过（fs 不暴露；保守全部按本地固定盘处理）。
 *  - 非 Windows：开发态 fallback，不真探测可用空间，直接返回 '/' + 子目录名。
 *  返回：{ disk: 'C', abs_path: 'C:\\ShortVideoToolData' } 或 null（无可用盘） */
export function pickDefaultDataDirHint(): { disk: string; abs_path: string } | null {
  const dirName = 'ShortVideoToolData'
  if (os.platform() !== 'win32') {
    // 开发态 fallback：Mac/Linux 不做盘符探测，按用户实际工作目录的根算
    return { disk: '/', abs_path: path.join(path.sep, dirName) }
  }
  let best: { letter: string; free: number } | null = null
  for (let code = 65; code <= 90; code++) {
    const letter = String.fromCharCode(code)
    const root = `${letter}:\\`
    if (!fs.existsSync(root)) continue
    try {
      // fs.statfsSync Node 18.15+；Electron 28 内置 Node 18.x 支持
      // 单次调用复用结果，避免 26 盘 52 次 syscall
      const st = fs.statfsSync(root)
      const free = st.bsize * st.bavail
      if (best === null || free > best.free) {
        best = { letter, free }
      }
    } catch {
      continue
    }
  }
  if (best === null) return null
  return {
    disk: best.letter,
    abs_path: path.join(`${best.letter}:\\`, dirName),
  }
}