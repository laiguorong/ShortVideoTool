// PyInstaller onedir 后处理：把 dist/shortvideo-backend/ 子目录内容平移到 dist/ 顶层
// 让 extraResources 直接复制 dist/ → <install>/resources/backend/（路径与 onefile 一致）
//
// 用法：node scripts/flatten-backend-dist.cjs [backendDistPath=../backend/dist]
//
// 行为：
// - 如果 backendDistPath/shortvideo-backend/ 存在 → 把内部全部平移到 backendDistPath/
// - _internal/ 目录保留（与 PyInstaller contents_directory 默认行为一致）
// - 平移冲突的文件跳过（已有同名文件不动）

const fs = require('fs')
const path = require('path')

const target = path.resolve(process.argv[2] || '../backend/dist')
const subdir = path.join(target, 'shortvideo-backend')

if (!fs.existsSync(subdir)) {
  console.log(`[flatten-backend] ${subdir} 不存在，跳过`)
  process.exit(0)
}

let moved = 0
for (const name of fs.readdirSync(subdir)) {
  const src = path.join(subdir, name)
  const dst = path.join(target, name)
  if (fs.existsSync(dst)) {
    console.log(`[flatten-backend] 跳过 ${name}（目标已存在）→ 清源残留`)
    // 目标已存在说明上次构建残留：源目录里的旧产物全部丢弃，
    // 否则后续 rmdir 会因非空失败（ENOTEMPTY）。
    fs.rmSync(src, { recursive: true, force: true })
    continue
  }
  // fs.renameSync 跨盘符抛 EXDEV（如 C: → D:），用 cpSync + rmSync 兜底保证跨盘兼容
  try {
    fs.renameSync(src, dst)
  } catch (e) {
    if (e.code === 'EXDEV') {
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
