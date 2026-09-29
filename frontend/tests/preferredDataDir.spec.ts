/**
 * #data-dir-choice 模块单测：固化 load / write / validate / pickDefault 行为。
 *
 * 设计要点：
 *  - 通过 userDataDir 参数注入临时目录，避开真实 Electron app.getPath
 *  - 跨平台行为：Windows 校验盘符存在用 fs.existsSync（C:\\ 等），本测试在 Windows + CI
 *    上都跑得通；非 Windows 路径直接返回绝对路径（跳过盘符校验）
 *
 * 运行：cd frontend && npx tsx tests/preferredDataDir.spec.ts
 *      或 npx jest tests/preferredDataDir.spec.ts
 */
import fs from 'fs'
import os from 'os'
import path from 'path'
import {
  loadPreferredDataDir,
  writePreferredDataDir,
  validateDataDir,
  pickDefaultDataDirHint,
} from '../src/main/preferredDataDir'

const PERSIST_FILE = 'preferred_data_dir.json'

function makeTempUserDataDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'dt_pref_'))
}
function writeRawJson(userDataDir: string, raw: string): void {
  fs.writeFileSync(path.join(userDataDir, PERSIST_FILE), raw, 'utf8')
}
function cleanup(userDataDir: string): void {
  try { fs.rmSync(userDataDir, { recursive: true, force: true }) } catch { /* ignore */ }
}

const isWin = process.platform === 'win32'
const validAbsPath = isWin ? 'C:\\TestData' : '/tmp/TestData'
const validExistingDir = isWin ? os.tmpdir() : os.tmpdir()  // 现有目录做验证

// ============ load 测试 ============

function test_load_returns_null_when_no_file(): void {
  const dir = makeTempUserDataDir()
  try {
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`期望 null，实际 ${r}`)
  } finally { cleanup(dir) }
}

function test_load_returns_abs_path_when_valid(): void {
  const dir = makeTempUserDataDir()
  try {
    writeRawJson(dir, JSON.stringify({ abs_path: validAbsPath, chosen_at: '2026-01-01' }))
    const r = loadPreferredDataDir(dir)
    if (r !== validAbsPath) throw new Error(`期望 ${validAbsPath}，实际 ${r}`)
  } finally { cleanup(dir) }
}

function test_load_rejects_relative_path(): void {
  const dir = makeTempUserDataDir()
  try {
    writeRawJson(dir, JSON.stringify({ abs_path: 'relative/foo' }))
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`相对路径应返回 null，实际 ${r}`)
    // backup 应在
    if (!fs.existsSync(path.join(dir, `${PERSIST_FILE}.broken`))) {
      throw new Error('损坏文件应被备份为 .broken')
    }
  } finally { cleanup(dir) }
}

function test_load_rejects_empty_abs_path(): void {
  const dir = makeTempUserDataDir()
  try {
    writeRawJson(dir, JSON.stringify({ abs_path: '' }))
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`空 abs_path 应返回 null，实际 ${r}`)
  } finally { cleanup(dir) }
}

function test_load_backs_up_broken_json(): void {
  const dir = makeTempUserDataDir()
  try {
    writeRawJson(dir, 'this is not json {{{')
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`损坏 JSON 应返回 null，实际 ${r}`)
    if (!fs.existsSync(path.join(dir, `${PERSIST_FILE}.broken`))) {
      throw new Error('损坏 JSON 应被备份为 .broken')
    }
  } finally { cleanup(dir) }
}

function test_load_validates_windows_drive_letter(): void {
  if (!isWin) return  // 非 Windows 平台跳过
  const dir = makeTempUserDataDir()
  try {
    // 缺盘符前缀（仅冒号）
    writeRawJson(dir, JSON.stringify({ abs_path: ':\\foo' }))
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`缺盘符路径应返回 null，实际 ${r}`)
    if (!fs.existsSync(path.join(dir, `${PERSIST_FILE}.broken`))) {
      throw new Error('无效盘符路径应被备份')
    }
  } finally { cleanup(dir) }
}

function test_load_rejects_unreachable_drive(): void {
  if (!isWin) return  // 非 Windows 平台跳过
  const dir = makeTempUserDataDir()
  try {
    // Z:\\ 是极少存在的盘符（CI 环境通常没有）
    writeRawJson(dir, JSON.stringify({ abs_path: 'Z:\\ShortVideoToolData' }))
    const r = loadPreferredDataDir(dir)
    if (r !== null) throw new Error(`Z:\\ 不应可达（CI 上无此盘），但返回 ${r}`)
    if (!fs.existsSync(path.join(dir, `${PERSIST_FILE}.broken`))) {
      throw new Error('不可达盘符应被备份')
    }
  } finally { cleanup(dir) }
}

// ============ write 测试 ============

function test_write_creates_file_and_round_trips(): void {
  const dir = makeTempUserDataDir()
  try {
    writePreferredDataDir(validAbsPath, dir)
    const r = loadPreferredDataDir(dir)
    if (r !== validAbsPath) throw new Error(`write→load 期望 ${validAbsPath}，实际 ${r}`)
  } finally { cleanup(dir) }
}

function test_write_is_atomic_via_tmp_rename(): void {
  const dir = makeTempUserDataDir()
  try {
    writePreferredDataDir(validAbsPath, dir)
    // 不应残留 .tmp
    if (fs.existsSync(path.join(dir, `${PERSIST_FILE}.tmp`))) {
      throw new Error('.tmp 文件未清理')
    }
    // 主文件存在
    if (!fs.existsSync(path.join(dir, PERSIST_FILE))) {
      throw new Error('主文件未写入')
    }
  } finally { cleanup(dir) }
}

function test_write_overwrites_existing(): void {
  const dir = makeTempUserDataDir()
  try {
    writePreferredDataDir(validAbsPath, dir)
    writePreferredDataDir('D:\\AnotherPath', dir)
    const r = loadPreferredDataDir(dir)
    if (r !== 'D:\\AnotherPath') throw new Error(`覆盖写入失败，实际 ${r}`)
  } finally { cleanup(dir) }
}

// ============ validate 测试 ============

function test_validate_rejects_empty(): void {
  const r = validateDataDir('')
  if (r.ok) throw new Error('空路径应 ok=false')
}

function test_validate_rejects_non_directory(): void {
  const dir = makeTempUserDataDir()
  try {
    const file = path.join(dir, 'notadir.txt')
    fs.writeFileSync(file, 'x')
    const r = validateDataDir(file)
    if (r.ok) throw new Error(`文件不应被识别为目录，实际 ok=${r.ok}`)
  } finally { cleanup(dir) }
}

function test_validate_accepts_writable_existing_dir(): void {
  // os.tmpdir() 一定存在 + 可写
  const r = validateDataDir(validExistingDir)
  if (!r.ok) throw new Error(`临时目录应 ok=true，实际 reason=${r.reason}`)
}

function test_validate_rejects_nonexistent(): void {
  const dir = makeTempUserDataDir()
  try {
    const nonexistent = path.join(dir, 'no-such-dir')
    const r = validateDataDir(nonexistent)
    if (r.ok) throw new Error('不存在路径应 ok=false')
    if (!r.reason?.includes('路径不存在')) {
      throw new Error(`reason 应含「路径不存在」，实际 ${r.reason}`)
    }
  } finally { cleanup(dir) }
}

// ============ pickDefault 测试 ============

function test_pickDefault_returns_valid_hint_on_win(): void {
  if (!isWin) return
  const r = pickDefaultDataDirHint()
  // CI 上可能没固定盘 → 跳过断言；开发机上应返回
  if (r !== null) {
    if (!/^[A-Z]$/.test(r.disk)) throw new Error(`disk 格式错：${r.disk}`)
    if (!r.abs_path.startsWith(`${r.disk}:\\`)) throw new Error(`abs_path 缺盘符：${r.abs_path}`)
    if (!r.abs_path.endsWith('ShortVideoToolData')) throw new Error(`abs_path 缺子目录：${r.abs_path}`)
  }
}

function test_pickDefault_returns_root_hint_on_unix(): void {
  if (isWin) return
  const r = pickDefaultDataDirHint()
  if (r === null) throw new Error('非 Windows 应返回根 hint')
  if (r.disk !== '/') throw new Error(`disk 应为 '/', 实际 ${r.disk}`)
  if (path.basename(r.abs_path) !== 'ShortVideoToolData') {
    throw new Error(`abs_path 应以 ShortVideoToolData 结尾，实际 ${r.abs_path}`)
  }
}

// ============ runner ============
function runAllTests(): void {
  const funcs: Array<[string, () => void]> = Object.entries(globalThis)
    .filter(([k, v]) => k.startsWith('test_') && typeof v === 'function') as any
  let passed = 0
  let failed = 0
  for (const [name, fn] of funcs) {
    try {
      fn()
      console.log(`  [OK] ${name}`)
      passed++
    } catch (e: any) {
      console.log(`  [FAIL] ${name}: ${e.message}`)
      failed++
    }
  }
  console.log(`\n${passed} passed, ${failed} failed (total ${funcs.length})`)
  if (failed > 0) process.exit(1)
}

// 注册到 globalThis 让 runner 找到
;(globalThis as any).test_load_returns_null_when_no_file = test_load_returns_null_when_no_file
;(globalThis as any).test_load_returns_abs_path_when_valid = test_load_returns_abs_path_when_valid
;(globalThis as any).test_load_rejects_relative_path = test_load_rejects_relative_path
;(globalThis as any).test_load_rejects_empty_abs_path = test_load_rejects_empty_abs_path
;(globalThis as any).test_load_backs_up_broken_json = test_load_backs_up_broken_json
;(globalThis as any).test_load_validates_windows_drive_letter = test_load_validates_windows_drive_letter
;(globalThis as any).test_load_rejects_unreachable_drive = test_load_rejects_unreachable_drive
;(globalThis as any).test_write_creates_file_and_round_trips = test_write_creates_file_and_round_trips
;(globalThis as any).test_write_is_atomic_via_tmp_rename = test_write_is_atomic_via_tmp_rename
;(globalThis as any).test_write_overwrites_existing = test_write_overwrites_existing
;(globalThis as any).test_validate_rejects_empty = test_validate_rejects_empty
;(globalThis as any).test_validate_rejects_non_directory = test_validate_rejects_non_directory
;(globalThis as any).test_validate_accepts_writable_existing_dir = test_validate_accepts_writable_existing_dir
;(globalThis as any).test_validate_rejects_nonexistent = test_validate_rejects_nonexistent
;(globalThis as any).test_pickDefault_returns_valid_hint_on_win = test_pickDefault_returns_valid_hint_on_win
;(globalThis as any).test_pickDefault_returns_root_hint_on_unix = test_pickDefault_returns_root_hint_on_unix

runAllTests()