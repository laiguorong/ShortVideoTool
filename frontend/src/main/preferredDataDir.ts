/**
 * 数据目录路径校验 + 默认盘探测（与 Electron userData 持久化解耦，#598 删）。
 *
 * 历史：原本还有 loadPreferredDataDir / writePreferredDataDir 维护
 * app.getPath('userData')/preferred_data_dir.json，但 #597 重构后启动流程
 * 走 settings.json::data_dir 而非 userData 持久化；设置页"更改数据目录"原本
 * 只写 userData + 弹重启（实际重启后仍用旧 data_dir，因为 settings.json 没更新），
 * 是历史 bug。整套删除 userData 持久化后，settings:chooseDataDir 改走主进程
 * fetch 后端 POST /api/settings/data-dir-prepare 写 settings.json——
 * 这才真正生效（且不触发 init_data_dir 避免运行时改 DATA_DIR）。
 *
 * v1.0.2 起（#598）仅保留这两个 export：
 * - validateDataDir：弹窗内"用户选目录 → 校验存在 + 是目录 + 可写"
 * - pickDefaultDataDirHint：弹窗"默认盘"按钮的提示来源
 */
import fs from 'fs'
import os from 'os'
import path from 'path'

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
