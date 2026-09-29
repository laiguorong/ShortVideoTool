/**
 * #data-dir-choice：buildRelaunchArgs 单测（dev/prod 双轨）。
 *
 * 运行：cd frontend && npx tsx tests/buildRelaunchArgs.spec.ts
 */
import type { App } from 'electron'
import { buildRelaunchArgs } from '../src/main/buildRelaunchArgs'

function mockApp(opts: { isPackaged: boolean; getAppPath?: string }): Pick<App, 'isPackaged' | 'getAppPath'> {
  return {
    isPackaged: opts.isPackaged,
    getAppPath: () => opts.getAppPath ?? 'C:\\AppPath',
  }
}

function test_dev_returns_app_path(): void {
  const r = buildRelaunchArgs(mockApp({ isPackaged: false, getAppPath: 'D:\\Project' }))
  if (!r.args || r.args.length !== 1 || r.args[0] !== 'D:\\Project') {
    throw new Error(`dev 应返 { args: ['D:\\Project'] }，实际 ${JSON.stringify(r)}`)
  }
}

function test_prod_returns_empty(): void {
  const r = buildRelaunchArgs(mockApp({ isPackaged: true, getAppPath: 'C:\\AppData\\Packaged' }))
  if (r.args !== undefined) {
    throw new Error(`prod 应不设 args，实际 ${JSON.stringify(r)}`)
  }
}

;(globalThis as any).test_dev_returns_app_path = test_dev_returns_app_path
;(globalThis as any).test_prod_returns_empty = test_prod_returns_empty

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