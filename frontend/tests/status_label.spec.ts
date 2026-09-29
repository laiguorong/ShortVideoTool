/**
 * P0-3 / P1-7 修复后验证测试：STATUS_LABEL 完整性。
 *
 * 运行：cd frontend && npx tsx tests/status_label.spec.ts
 * 或  npx jest tests/status_label.spec.ts
 */
import { STATUS_LABEL } from '../src/renderer/src/components/shared/StatusBadge'

const REQUIRED_KEYS = [
  // 账号
  'normal', 'undetected', 'invalid', 'disabled',
  // 定时任务
  'enabled',
  // 门店标记
  'favorite', 'excluded',
  // 后台/生成/发布任务
  'pending', 'waiting', 'running', 'completed', 'publishing',
  'suspended', 'paused', 'success', 'partial', 'failed',
  'cancelled', 'finished', 'draft',
  // 成品视频
  'idle', 'occupied',
  // 发布记录
  'removed',
  // 素材文件
  'missing',
]

function assertContains(needle: string, hay: string[]): boolean {
  return hay.includes(needle)
}

function test_required_keys_present(): void {
  const missing: string[] = []
  for (const k of REQUIRED_KEYS) {
    if (!(k in STATUS_LABEL)) missing.push(k)
  }
  if (missing.length > 0) {
    throw new Error(`STATUS_LABEL 缺失 key: ${missing.join(', ')}`)
  }
}

function test_labels_non_empty_for_non_success(): void {
  // 所有状态都应有可见中文文案
  for (const [k, v] of Object.entries(STATUS_LABEL)) {
    if (!v || typeof v !== 'string') {
      throw new Error(`${k} 标签为空或非字符串`)
    }
  }
}

function test_no_duplicate_meaning(): void {
  // 终态文案应与 STATUS_LABEL 自洽：default_message 不会与 label 重复
  // success="" 故意空；其余 4 终态：
  // partial='部分成功' / failed='失败' / cancelled='已取消'
  // 确保这些文案与 STATUS_LABEL 不冲突（让前端冗余判断生效）
  if (STATUS_LABEL['partial'] !== '部分成功') throw new Error('partial 文案不一致')
  if (STATUS_LABEL['failed'] !== '失败') throw new Error('failed 文案不一致')
  if (STATUS_LABEL['cancelled'] !== '已取消') throw new Error('cancelled 文案不一致')
}

function test_chinese_only(): void {
  // 文案以中文为主；技术名词（Cookie/BGM 等）允许保留
  for (const [k, v] of Object.entries(STATUS_LABEL)) {
    // 必须含至少一个中文字符
    if (!/[一-龥]/.test(v)) {
      throw new Error(`${k} 标签应含中文: ${v}`)
    }
  }
}

// ===== runner =====
function runAll(): void {
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
;(globalThis as any).test_required_keys_present = test_required_keys_present
;(globalThis as any).test_labels_non_empty_for_non_success = test_labels_non_empty_for_non_success
;(globalThis as any).test_no_duplicate_meaning = test_no_duplicate_meaning
;(globalThis as any).test_chinese_only = test_chinese_only

if (require.main === module) {
  runAll()
}

export {}
