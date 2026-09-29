// #pack-lock-fix：清理 release/win-unpacked 给 electron-builder 重打包用。
//
// 背景：360 / 火绒等第三方杀软会持续锁住 app.asar（mmapped 句柄不释放），
// rm 直接报 "Device or resource busy"。retry + sleep 等锁释放窗口。
//
// 用法：node scripts/clean-release.cjs [path=release/win-unpacked] [maxRetries=20] [sleepMs=3000]
//
// 退出码：
//   0 = 删干净或目录本就不存在
//   1 = 重试耗尽仍有文件 busy（多半需要去杀软加信任区）

const fs = require('fs')
const path = require('path')

const target = path.resolve(process.argv[2] || 'release/win-unpacked')
const maxRetries = Number(process.argv[3] || 20)
const sleepMs = Number(process.argv[4] || 3000)

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

async function tryRemove(p) {
  try {
    fs.rmSync(p, { recursive: true, force: true })
    return true
  } catch (e) {
    return false
  }
}

async function main() {
  if (!fs.existsSync(target)) {
    console.log(`[clean-release] ${target} 不存在，跳过`)
    process.exit(0)
  }
  for (let i = 1; i <= maxRetries; i++) {
    if (await tryRemove(target)) {
      console.log(`[clean-release] 第 ${i} 次尝试删干净：${target}`)
      process.exit(0)
    }
    if (i < maxRetries) {
      console.log(`[clean-release] 第 ${i}/${maxRetries} 次失败，${sleepMs}ms 后重试`)
      await sleep(sleepMs)
    }
  }
  console.error(`[clean-release] ${maxRetries} 次重试耗尽仍 busy。`)
  console.error(`[clean-release] 大概率是 360 / 火绒等杀软实时扫描锁住 app.asar。`)
  console.error(`[clean-release] 解决：在杀软「信任区」添加 frontend/release/ 目录。`)
  process.exit(1)
}

main()