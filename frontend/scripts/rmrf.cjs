// 跨平台 rmdir 工具
//
// #fix-rmrf-windows: 之前 package.json 的 build:backend 用 `rm -rf dist`（POSIX
// 命令），Windows cmd 不识别（cmd 只有 `rmdir /s /q`），PowerShell `rm` 是
// Remove-Item 的 alias 但只支持 `-r -fo` 不支持 `-rf` 短选项。
// 这里用 Node 内置 fs.rmSync（Node 14.14+）跨平台删除，npx/npm scripts 调用都 OK。
//
// 用法：node scripts/rmrf.cjs <path1> [path2] [...]
// 退出码：成功 0，失败 1（ENOENT 忽略，不算错）
// 行为对应 `rm -rf <path>`：缺失不算错，递归删，强制忽略只读属性

const fs = require('fs')
const path = require('path')

let failed = 0
for (const target of process.argv.slice(2)) {
  const abs = path.resolve(target)
  try {
    fs.rmSync(abs, { recursive: true, force: true })
  } catch (e) {
    if (e.code === 'ENOENT') continue
    console.error(`[rmrf] ${abs}: ${e.message}`)
    failed++
  }
}
process.exit(failed > 0 ? 1 : 0)
