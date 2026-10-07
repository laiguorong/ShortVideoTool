// -*- coding: utf-8 -*-
/** 启动检查页（居中单项 + 上下分区）。
 *
 * 设计：
 * - 居中卡片：上 spinner / 下文字（label + detail + 进度）
 * - 不显示 STARTUP_STEPS.length 步列表，只展示"当前正在跑的步骤"的状态
 * - 失败时只展示错误原因（label + detail），不提供按钮（用户自行决定如何处理）
 * - 数据目录选择由 Electron 主进程 ensureDataDirChoice 在主窗口创建前完成（透传 env），
 *   启动页 data_dir 步骤只展示状态，不在 UI 重复选路径
 */

import { useEffect, useState, useRef } from 'react'
import { STARTUP_STEPS, check, type CheckResult } from '@/api/startup'

interface Props {
  /** 全部 STARTUP_STEPS.length 步 ok 时回调（父组件切到主界面） */
  onComplete: () => void
}

type Phase = 'waiting-backend' | 'running' | 'done' | 'failed'

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

  // effect 仅在挂载时跑一次，runFlow 内部用 runningRef 守卫
  useEffect(() => {
    let cancelled = false
    const runFlow = async () => {
      if (runningRef.current) return
      runningRef.current = true
      setPhase('running')
      try {
        for (let i = 0; i < STARTUP_STEPS.length; i++) {
          if (cancelled) return
          const s = STARTUP_STEPS[i]
          setCurrentIndex(i)
          setSteps((prev) => ({
            ...prev,
            [s.key]: { key: s.key, status: 'running', detail: '', data: {}, ts: '' },
          }))
          const result = await check(s.key)
          if (cancelled) return
          setSteps((prev) => ({ ...prev, [s.key]: result }))
          if (result.status === 'failed') {
            setPhase('failed')
            runningRef.current = false
            return
          }
        }
        if (cancelled) return
        setPhase('done')
        runningRef.current = false
        // done 后 1500ms 切主界面（让用户看清"启动完成"）
        doneTimerRef.current = setTimeout(() => {
          doneTimerRef.current = null
          onCompleteRef.current()
        }, 1500)
      } catch (e) {
        if (cancelled) return
        const msg = e instanceof Error ? e.message : String(e)
        console.error('[StartupPage] 启动检查请求异常:', e)
        // 用最后一次记录的 currentIndex 找步骤（闭包安全）
        setSteps((prev) => {
          const idx = Math.max(0, currentIndex)
          const s = STARTUP_STEPS[idx] ?? STARTUP_STEPS[0]
          return {
            ...prev,
            [s.key]: { key: s.key, status: 'failed', detail: msg, data: {}, ts: new Date().toISOString() },
          }
        })
        setPhase('failed')
        runningRef.current = false
      }
    }
    runFlow()
    return () => {
      cancelled = true
      runningRef.current = false  // Strict Mode 二次挂载时重新允许跑
      if (doneTimerRef.current !== null) {
        clearTimeout(doneTimerRef.current)
        doneTimerRef.current = null
      }
    }
  // 仅 mount 时跑一次；onComplete / currentIndex 通过 ref / state 自管理
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
                phase === 'failed' ? 'text-danger' : 'text-muted-foreground'
              }`}
            >
              {detail}
            </div>
          )}
          {/* 进度：仅在 running / failed / done 时显示 */}
          {phase !== 'waiting-backend' && (
            <div className="mt-2 text-xs text-muted-foreground">
              第 {currentStepNum} / {totalSteps} 步
            </div>
          )}
        </div>

        {/* 失败时只展示错误原因，无按钮（用户自行决定如何处理） */}
      </div>
    </div>
  )
}
