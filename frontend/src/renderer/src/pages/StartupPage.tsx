// -*- coding: utf-8 -*-
/** 启动检查页（居中单项 + 上下分区）。
 *
 * 设计：
 * - 居中卡片：上 spinner / 下文字（label + detail + 进度）
 * - 不显示 STARTUP_STEPS.length 步列表，只展示"当前正在跑的步骤"的状态
 * - 失败时只展示错误原因（label + detail）
 * - #597：data_dir 步骤 failed + data.need_choose=true 时
 *   - 首次：自动调 IPC chooseDataDirFlow（主进程 ensureDataDirChoice 完整流程：默认盘 + 立即选择 + 5 次重试）
 *   - 用户取消：进入 need-choose 阶段，显示「重新选择数据目录」按钮
 *   - 用户点按钮：再次调 IPC 弹窗
 *   - 选成功：setDataDir 持久化 + 重试 check('data_dir')，继续后面 5 步
 */

import { useEffect, useState, useRef } from 'react'
import { STARTUP_STEPS, check, setDataDir, type CheckResult } from '@/api/startup'

interface Props {
  /** 全部 STARTUP_STEPS.length 步 ok 时回调（父组件切到主界面） */
  onComplete: () => void
}

type Phase = 'waiting-backend' | 'running' | 'done' | 'failed' | 'need-choose'

// data_dir 步骤在 STARTUP_STEPS 里的索引（用于 need-choose 时定位"重新选择"按钮文案）
const DATA_DIR_STEP_INDEX = STARTUP_STEPS.findIndex((s) => s.key === 'data_dir')
const DATA_DIR_STEP = STARTUP_STEPS[DATA_DIR_STEP_INDEX]

/** 单步检查（含网络异常 → 包装成 failed） */
async function runStep(key: string): Promise<CheckResult> {
  try {
    return await check(key)
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    return {
      key,
      status: 'failed',
      detail: msg,
      data: {},
      ts: new Date().toISOString(),
    }
  }
}

export function StartupPage({ onComplete }: Props) {
  const [steps, setSteps] = useState<Record<string, CheckResult>>({})
  const [phase, setPhase] = useState<Phase>('waiting-backend')
  const [currentIndex, setCurrentIndex] = useState(0)
  // 用 ref 持有 onComplete，避免 runFlow 依赖 props 导致 effect 重复触发
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete
  // done 阶段 setTimeout 句柄（unmount 时清理）
  const doneTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 防止 React 18 Strict Mode 下 effect 双调用导致 runFlow 卡死
  const runningRef = useRef(false)
  // need-choose 阶段"弹窗未关"标志（state 触发重渲染，按钮 disabled 才能生效）
  const [choosing, setChoosing] = useState(false)

  /** 调主进程 ensureDataDirChoice 弹窗选数据目录。
   *  返回 null = 用户取消；返 { abs_path } = 用户选了。
   *  选完后让 setDataDir 写 settings.json + init_data_dir 持久化。 */
  const runChooseDataDirFlow = async (
    prevDetail: string,
  ): Promise<{ abs_path: string } | null> => {
    setChoosing(true)
    // 弹窗期间把当前步骤状态切到 running，让 spinner 保持转（视觉提示"等待用户"）
    setSteps((prev) => ({
      ...prev,
      [DATA_DIR_STEP.key]: {
        key: DATA_DIR_STEP.key,
        status: 'running',
        detail: prevDetail || '正在选择数据目录…',
        data: { need_choose: true },
        ts: new Date().toISOString(),
      },
    }))
    try {
      const chosen = await window.electronAPI.chooseDataDirFlow()
      return chosen ?? null
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      console.error('[StartupPage] chooseDataDirFlow 异常:', e)
      setSteps((prev) => ({
        ...prev,
        [DATA_DIR_STEP.key]: {
          key: DATA_DIR_STEP.key,
          status: 'failed',
          detail: `选择数据目录失败：${msg}`,
          data: { need_choose: true },
          ts: new Date().toISOString(),
        },
      }))
      return null
    } finally {
      setChoosing(false)
    }
  }

  /** 从 startIndex 起依次跑 [startIndex .. 6) 步。
   *  失败处理：
   *  - data_dir 步骤 need_choose=true → 自动弹窗选 → 成功则重试 check('data_dir')，失败（取消）→ 切 need-choose
   *  - 其他失败 → 切 failed
   *  全 ok → 1500ms 切 done 后调 onComplete。
   *  cancelled（unmount）/ 已运行中 → 直接 return。 */
  const runFlowFrom = async (startIndex: number) => {
    if (runningRef.current) return
    runningRef.current = true
    // 清旧 done timer（避免 onComplete 重复触发）
    if (doneTimerRef.current !== null) {
      clearTimeout(doneTimerRef.current)
      doneTimerRef.current = null
    }
    setPhase('running')
    try {
      for (let i = startIndex; i < STARTUP_STEPS.length; i++) {
        const s = STARTUP_STEPS[i]
        setCurrentIndex(i)
        setSteps((prev) => ({
          ...prev,
          [s.key]: { key: s.key, status: 'running', detail: '', data: {}, ts: '' },
        }))
        let result = await runStep(s.key)

        // #597：data_dir 步骤 failed + need_choose → 触发选择弹窗
        if (
          result.status === 'failed' &&
          s.key === DATA_DIR_STEP.key &&
          result.data?.need_choose === true
        ) {
          const chosen = await runChooseDataDirFlow(result.detail)
          if (!chosen) {
            // 取消：进入 need-choose 阶段，显示按钮
            setSteps((prev) => ({
              ...prev,
              [s.key]: {
                key: s.key,
                status: 'failed',
                detail: result.detail,
                data: { need_choose: true },
                ts: new Date().toISOString(),
              },
            }))
            setCurrentIndex(DATA_DIR_STEP_INDEX)
            setPhase('need-choose')
            runningRef.current = false
            return
          }
          // 选成功：setDataDir 持久化 + 重试 check data_dir
          try {
            await setDataDir(chosen.abs_path)
          } catch (e) {
            const msg = e instanceof Error ? e.message : String(e)
            result = {
              key: s.key,
              status: 'failed',
              detail: `数据目录保存失败：${msg}`,
              data: { need_choose: true },
              ts: new Date().toISOString(),
            }
            setSteps((prev) => ({ ...prev, [s.key]: result }))
            setPhase('failed')
            runningRef.current = false
            return
          }
          result = await runStep(s.key)
          if (result.status === 'failed') {
            setSteps((prev) => ({ ...prev, [s.key]: result }))
            setPhase('failed')
            runningRef.current = false
            return
          }
        }

        setSteps((prev) => ({ ...prev, [s.key]: result }))
        if (result.status === 'failed') {
          setPhase('failed')
          runningRef.current = false
          return
        }
      }
      setPhase('done')
      runningRef.current = false
      // done 后 1500ms 切主界面（让用户看清"启动完成"）
      doneTimerRef.current = setTimeout(() => {
        doneTimerRef.current = null
        onCompleteRef.current()
      }, 1500)
    } catch (e) {
      // 兜底：runStep 已 catch 单步异常；这里捕获的是 setState / setPhase 等同步代码异常
      const msg = e instanceof Error ? e.message : String(e)
      console.error('[StartupPage] 启动检查流程异常:', e)
      setSteps((prev) => ({
        ...prev,
        data_dir: {
          key: 'data_dir',
          status: 'failed',
          detail: msg,
          data: {},
          ts: new Date().toISOString(),
        },
      }))
      setPhase('failed')
      runningRef.current = false
    }
  }

  // mount 时跑一次全部 6 步
  useEffect(() => {
    void runFlowFrom(0)
    return () => {
      runningRef.current = false  // Strict Mode 二次挂载时重新允许跑
      if (doneTimerRef.current !== null) {
        clearTimeout(doneTimerRef.current)
        doneTimerRef.current = null
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** need-choose 阶段用户点「重新选择数据目录」按钮 → 从 data_dir 起重跑 */
  const handleRetryChoose = async () => {
    if (choosing) return
    await runFlowFrom(DATA_DIR_STEP_INDEX)
  }

  // 当前展示的状态（按 phase）
  const currentStep = STARTUP_STEPS[Math.min(currentIndex, STARTUP_STEPS.length - 1)]
  const currentResult = steps[currentStep?.key ?? '']
  const totalSteps = STARTUP_STEPS.length
  const currentStepNum = phase === 'done' ? totalSteps : currentIndex + 1

  let title = '正在启动'
  let detail = ''
  let iconKind: 'spinner' | 'check' | 'cross' | 'waiting' = 'spinner'

  if (phase === 'waiting-backend') {
    title = '等待后端启动'
    detail = '正在连接后端服务…'
    iconKind = 'waiting'
  } else if (phase === 'running') {
    title = currentStep?.label ?? '检查中'
    detail = currentResult?.detail || currentStep?.description || ''
    iconKind = 'spinner'
  } else if (phase === 'done') {
    title = '启动完成'
    detail = '即将进入应用'
    iconKind = 'check'
  } else if (phase === 'failed') {
    title = `${currentStep?.label ?? '启动'} 失败`
    detail = currentResult?.detail ?? ''
    iconKind = 'cross'
  } else if (phase === 'need-choose') {
    title = '需要选择数据目录'
    detail = currentResult?.detail ?? '未配置数据目录，请点击下方按钮选择。'
    iconKind = 'cross'
  }

  return (
    <div className="flex h-full items-center justify-center bg-background">
      <div className="flex w-full max-w-md flex-col items-center gap-10 px-6">
        {/* 上部分：加载动画 */}
        <div className="flex h-24 w-24 items-center justify-center">
          {iconKind === 'spinner' && (
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-border border-t-accent" />
          )}
          {iconKind === 'waiting' && (
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-border border-t-muted-foreground" />
          )}
          {iconKind === 'check' && (
            <div className="flex h-20 w-20 items-center justify-center rounded-full bg-green-100 text-4xl text-green-600">
              ✓
            </div>
          )}
          {iconKind === 'cross' && (
            <div className="flex h-20 w-20 items-center justify-center rounded-full bg-red-100 text-4xl text-red-600">
              ✗
            </div>
          )}
        </div>

        {/* 下部分：文字信息 */}
        <div className="flex w-full flex-col items-center gap-3 text-center">
          <div className="text-xl font-semibold">{title}</div>
          {detail && (
            <div
              className={`min-h-[2.5rem] text-sm ${
                phase === 'failed' || phase === 'need-choose' ? 'text-danger' : 'text-muted-foreground'
              }`}
            >
              {detail}
            </div>
          )}
          {/* 进度：仅在 running 时显示（need-choose 也属于"卡住"状态，省略进度） */}
          {phase === 'running' && (
            <div className="mt-2 text-xs text-muted-foreground">
              第 {currentStepNum} / {totalSteps} 步
            </div>
          )}
        </div>

        {/* need-choose 阶段：显示「重新选择数据目录」按钮 */}
        {phase === 'need-choose' && (
          <button
            type="button"
            onClick={handleRetryChoose}
            className="rounded-md bg-accent px-6 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-accent/90 disabled:opacity-50"
            disabled={choosing}
          >
            重新选择数据目录
          </button>
        )}
      </div>
    </div>
  )
}
