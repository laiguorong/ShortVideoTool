// electron-builder afterPack 钩子：打包后清理多余语言包
// Electron 默认带 55 个 locales/*.pak（~37M），本工具只用 zh-CN.pak
// Windows: appOutDir = release/win-unpacked → locales/
// Linux/Mac 类似路径

const fs = require('fs')
const path = require('path')

const KEEP_LOCALE = 'zh-CN.pak'

exports.default = async function (context) {
  const localesDir = path.join(context.appOutDir, 'locales')
  if (!fs.existsSync(localesDir)) {
    console.log(`[after-pack] ${localesDir} 不存在，跳过`)
    return
  }

  let removed = 0
  let keptSize = 0
  for (const name of fs.readdirSync(localesDir)) {
    const p = path.join(localesDir, name)
    if (name === KEEP_LOCALE) {
      keptSize = fs.statSync(p).size
      continue
    }
    if (!name.endsWith('.pak')) continue
    try {
      fs.unlinkSync(p)
      removed++
    } catch (e) {
      console.warn(`[after-pack] 删除 ${name} 失败：${e.message}`)
    }
  }
  console.log(
    `[after-pack] 清理 locales/：删除 ${removed} 个，保留 ${KEEP_LOCALE}（${(keptSize / 1024).toFixed(1)} KB）`,
  )
}
