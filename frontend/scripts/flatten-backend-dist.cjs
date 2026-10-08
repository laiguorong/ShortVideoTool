// PyInstaller onedir 后处理：把 dist/shortvideo-backend/ 子目录内容平移到 dist/ 顶层
// 让 extraResources 直接复制 dist/ → <install>/resources/backend/（路径与 onefile 一致）
//
// 用法：node scripts/flatten-backend-dist.cjs [backendDistPath=../backend/dist]
//
// 行为：
// - **总是先清空** target 下 PyInstaller 平移后的产物（_internal / shortvideo-backend.exe），
//   保证 build:backend 失败时也不会保留旧 _internal 污染 electron-builder extraResources
//   （旧脚本在 subdir 不存在时早 return → 旧 _internal 留底 → 跟"目标已存在 → 跳过"
//   是同一种 bug——见 #597）
// - **不要**把 subdir 本身（shortvideo-backend/）加入清空列表：subdir 是 PyInstaller COLLECT
//   写入位置（内含 _internal/ + shortvideo-backend.exe），清掉 subdir 等于清空 PyInstaller 产出
// - 然后把 dist/shortvideo-backend/ 子目录内容平移到 dist/ 顶层
// - 平移冲突的文件：先清旧再平移（防 EXDEV cpSync 跨盘覆盖冲突 + 防保留旧字节码）
// - rmSync 失败给友好提示（常见：Windows 360 杀软锁 _internal 文件）

const fs = require('fs')
const path = require('path')

const target = path.resolve(process.argv[2] || '../backend/dist')
const subdir = path.join(target, 'shortvideo-backend')

// PyInstaller onedir 平移到 dist 根的产物（COLLECT 输出的 _internal 是先在 subdir/ 内，
// 由本脚本平移上来；EXE 阶段直接写 dist/shortvideo-backend.exe）。
// 注意：不要把 'shortvideo-backend'（subdir 本身）放进来！
const KNOWN_PYI_ARTIFACTS = ['_internal', 'shortvideo-backend.exe']

function safeRm(name) {
  const p = path.join(target, name)
  if (!fs.existsSync(p)) return
  try {
    fs.rmSync(p, { recursive: true, force: true })
    console.log(`[flatten-backend] 清旧 ${name}`)
  } catch (e) {
    // Windows 360 杀软锁文件时 rmSync 抛 EBUSY。给友好提示但不阻塞 build：
    // 让后续平移失败暴露问题（dst 被占的话 rename/cpSync 也会失败）
    console.error(`[flatten-backend] ⚠️ 清旧 ${name} 失败: ${e.message}`)
    console.error(`[flatten-backend]    提示：若 Windows 360 杀软锁文件，请把 dist/ 加入信任区后重试`)
  }
}

// 1. 总是先清空 PyInstaller 平移后产物（无论 subdir 是否存在）
console.log(`[flatten-backend] 清空 PyInstaller 平移后产物：${target}`)
for (const name of KNOWN_PYI_ARTIFACTS) {
  safeRm(name)
}

// 2. 如果 subdir 不存在（pyinstaller 没跑或失败），到此结束
if (!fs.existsSync(subdir)) {
  console.log(`[flatten-backend] ${subdir} 不存在，跳过平移（pyinstaller 未产出）`)
  process.exit(0)
}

// 3. 平移 subdir → target
let moved = 0
for (const name of fs.readdirSync(subdir)) {
  const src = path.join(subdir, name)
  const dst = path.join(target, name)
  // 平移冲突的文件：先清旧再平移（防 EXDEV cpSync 跨盘覆盖冲突 + 防保留旧字节码）
  if (fs.existsSync(dst)) {
    console.log(`[flatten-backend] 清旧 ${name} → 平移新内容`)
    safeRm(name)
  }
  // fs.renameSync 跨盘符抛 EXDEV（如 C: → D:），用 cpSync + rmSync 兜底保证跨盘兼容
  try {
    fs.renameSync(src, dst)
  } catch (e) {
    if (e.code === 'EXDEV') {
      // EXDEV 跨盘：先清旧 dst 再 cpSync（前面 safeRm 已处理，但保留防御）
      if (fs.existsSync(dst)) fs.rmSync(dst, { recursive: true, force: true })
      fs.cpSync(src, dst, { recursive: true })
      fs.rmSync(src, { recursive: true, force: true })
    } else {
      throw e
    }
  }
  moved++
}

fs.rmdirSync(subdir)
console.log(`[flatten-backend] 平移 ${moved} 项：${subdir} → ${target}`)
