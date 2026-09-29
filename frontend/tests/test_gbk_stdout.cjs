// #181 / #183：Windows cmd GBK codepage 下 Electron 主进程 stdout 中文乱码
// 验证 iconv-lite GBK 编码 monkey-patch 正确。
// 模拟：在 patch 后调用 console.log + process.stdout.write，检查输出字节流是 GBK 编码。

const assert = require('assert')
const iconv = require('iconv-lite')

// 模拟 stdout 收集 buffer（patch 后应收到 Buffer，不再做编码转换）
const captured = []
const fakeStream = {
  write(chunk, encodingOrCb, cb) {
    captured.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk))
    if (typeof encodingOrCb === 'function') encodingOrCb()
    else if (typeof cb === 'function') cb()
    return true
  }
}

// 复现 main.ts 中的 patch 逻辑（仅 Windows）
if (process.platform === 'win32') {
  const origWrite = fakeStream.write.bind(fakeStream)
  fakeStream.write = function (chunk, encodingOrCb, cb) {
    let enc, callback
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

console.log = (...args) => {
  const msg = args.join(' ')
  fakeStream.write(msg + '\n')
}

// 测试用例
function test_chinese_ported_to_gbk() {
  captured.length = 0
  console.log('[Main] 端口 8765 已有后端，复用现有实例')
  const out = Buffer.concat(captured)
  // GBK 编码的 "端" 是 0xB6 0xCB（不是 UTF-8 的 0xE7 0xAB 0xAF）
  assert.ok(out.includes(Buffer.from([0xB6, 0xCB])),
    `输出应含 GBK 编码的"端" (0xB6 0xCB)，实际前 30 字节：${out.subarray(0, 30).toString('hex')}`)
  // 不应含 UTF-8 多字节序列
  assert.ok(!out.includes(Buffer.from([0xE7])),
    `输出不应含 UTF-8 首字节 0xE7（"端"的 UTF-8 首字节）`)
  console.log('  [OK] test_chinese_ported_to_gbk')
}

function test_ascii_passes_through() {
  captured.length = 0
  console.log('[Main] backend ready')
  const out = Buffer.concat(captured)
  // ASCII 段与 GBK/UTF-8 都一致
  assert.strictEqual(iconv.decode(out, 'gbk'), '[Main] backend ready\n')
  console.log('  [OK] test_ascii_passes_through')
}

function test_mixed_chinese_ascii() {
  captured.length = 0
  console.log('[Renderer] [WARN] 视频数 0/10')
  const out = Buffer.concat(captured)
  // 整个字符串按 GBK 解码应得原文
  assert.strictEqual(iconv.decode(out, 'gbk'), '[Renderer] [WARN] 视频数 0/10\n')
  console.log('  [OK] test_mixed_chinese_ascii')
}

test_chinese_ported_to_gbk()
test_ascii_passes_through()
test_mixed_chinese_ascii()
console.log('all gbk stdout tests passed')