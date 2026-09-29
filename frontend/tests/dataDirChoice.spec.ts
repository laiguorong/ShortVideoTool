/**
 * #data-dir-choice：decideNextAction + decideRetryOrQuit 纯函数单测。
 *
 * 覆盖：
 *  - 按钮 0（默认）→ use-default
 *  - 按钮 1（立即选择）+ 选了 + 通过 → confirm
 *  - 按钮 1 + 选了 + 校验失败 → show-validation-error
 *  - 按钮 1 + 取消 picker → show-retry-or-quit
 *  - 按钮 2（取消）→ show-retry-or-quit
 *  - 末轮按钮 2 → quit
 *  - 末轮按钮 1 + 取消 → quit（直接退出不再弹 retry）
 *  - decideRetryOrQuit：0=retry / 1=quit
 *
 * 运行：cd frontend && npx tsx tests/dataDirChoice.spec.ts
 */
import { decideNextAction, decideRetryOrQuit } from '../src/main/dataDirChoice'

function test_button0_use_default(): void {
  const a = decideNextAction({ attempt: 1, maxRetry: 5, buttonResponse: 0 })
  if (a.action !== 'use-default') throw new Error(`期望 use-default，实际 ${a.action}`)
}

function test_button1_chosen_and_valid_confirms(): void {
  const a = decideNextAction({
    attempt: 1, maxRetry: 5, buttonResponse: 1,
    chosen: 'D:\\data', validation: { ok: true },
  })
  if (a.action !== 'confirm' || a.chosen !== 'D:\\data') {
    throw new Error(`期望 confirm D:\\data，实际 ${JSON.stringify(a)}`)
  }
}

function test_button1_chosen_but_invalid_shows_error(): void {
  const a = decideNextAction({
    attempt: 1, maxRetry: 5, buttonResponse: 1,
    chosen: 'D:\\readonly', validation: { ok: false, reason: '目录无写入权限' },
  })
  if (a.action !== 'show-validation-error') throw new Error(`期望 show-validation-error，实际 ${a.action}`)
  if (a.reason !== '目录无写入权限') throw new Error(`reason 应传递，实际 ${a.reason}`)
}

function test_button1_picker_canceled_shows_retry(): void {
  const a = decideNextAction({
    attempt: 1, maxRetry: 5, buttonResponse: 1, pickerCanceled: true,
  })
  if (a.action !== 'show-retry-or-quit') throw new Error(`期望 show-retry-or-quit，实际 ${a.action}`)
}

function test_button2_shows_retry(): void {
  const a = decideNextAction({ attempt: 1, maxRetry: 5, buttonResponse: 2 })
  if (a.action !== 'show-retry-or-quit') throw new Error(`期望 show-retry-or-quit，实际 ${a.action}`)
}

function test_last_attempt_button2_quits(): void {
  const a = decideNextAction({ attempt: 5, maxRetry: 5, buttonResponse: 2 })
  if (a.action !== 'quit') throw new Error(`末轮按钮2应 quit，实际 ${a.action}`)
}

function test_last_attempt_button1_picker_canceled_quits(): void {
  const a = decideNextAction({
    attempt: 5, maxRetry: 5, buttonResponse: 1, pickerCanceled: true,
  })
  if (a.action !== 'quit') throw new Error(`末轮 picker 取消应 quit，实际 ${a.action}`)
}

function test_retry_or_quit_0_is_retry(): void {
  if (decideRetryOrQuit(0).action !== 'retry') throw new Error('0 应 retry')
}
function test_retry_or_quit_1_is_quit(): void {
  if (decideRetryOrQuit(1).action !== 'quit') throw new Error('1 应 quit')
}

;(globalThis as any).test_button0_use_default = test_button0_use_default
;(globalThis as any).test_button1_chosen_and_valid_confirms = test_button1_chosen_and_valid_confirms
;(globalThis as any).test_button1_chosen_but_invalid_shows_error = test_button1_chosen_but_invalid_shows_error
;(globalThis as any).test_button1_picker_canceled_shows_retry = test_button1_picker_canceled_shows_retry
;(globalThis as any).test_button2_shows_retry = test_button2_shows_retry
;(globalThis as any).test_last_attempt_button2_quits = test_last_attempt_button2_quits
;(globalThis as any).test_last_attempt_button1_picker_canceled_quits = test_last_attempt_button1_picker_canceled_quits
;(globalThis as any).test_retry_or_quit_0_is_retry = test_retry_or_quit_0_is_retry
;(globalThis as any).test_retry_or_quit_1_is_quit = test_retry_or_quit_1_is_quit

const funcs = Object.entries(globalThis).filter(([k, v]) => k.startsWith('test_') && typeof v === 'function') as Array<[string, () => void]>
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