/**
 * #data-dir-choice：首次启动数据目录选择 state machine（纯函数版）。
 *
 * 输入：当前 attempt（1..MAX_RETRY）+ 用户点击的按钮响应 + picker 是否取消
 * 输出：下一步 action。ensureDataDirChoice 主流程按 action 推进。
 *
 * 设计：
 *  - 循环而非递归：attempt 1..MAX_RETRY 线性增长，避免深递归爆栈
 *  - 末轮（attempt === MAX_RETRY）：取消/选了又取消 → 直接返回 'quit'
 *    不再弹 retry-or-quit（避免无限弹窗）
 *  - 普通轮：取消 / 选了又取消 → 返回 'show-retry-or-quit'
 */

export type DataDirChoiceAction =
  /** 使用默认数据目录（hint.abs_path） */
  | { action: 'use-default' }
  /** 打开文件选择对话框，让用户挑目录 */
  | { action: 'open-picker' }
  /** 选了 + 校验通过，确认写入 */
  | { action: 'confirm'; chosen: string }
  /** 选了但校验失败（reason 由调用方提供）；回到循环顶部 */
  | { action: 'show-validation-error'; chosen: string; reason: string }
  /** 弹出"重新选择 / 退出"二选一对话框 */
  | { action: 'show-retry-or-quit' }
  /** 用户彻底退出 */
  | { action: 'quit' }
  /** 重置 attempt 计数器（picker 取消后回到循环顶部） */
  | { action: 'retry' }

export interface DataDirChoiceInput {
  /** 当前循环轮次（1-indexed） */
  attempt: number
  /** 最大循环轮次 */
  maxRetry: number
  /** 三按钮弹窗用户选择：0=使用默认 / 1=立即选择 / 2=取消 */
  buttonResponse: 0 | 1 | 2
  /** 选了之后是否又取消了 picker（true = 选了再取消） */
  pickerCanceled?: boolean
  /** 校验结果（仅在 picker 选了路径时有效） */
  validation?: { ok: boolean; reason?: string }
  /** 选中的路径（仅 picker 返回了文件时有效） */
  chosen?: string | null
}

export function decideNextAction(input: DataDirChoiceInput): DataDirChoiceAction {
  const { attempt, maxRetry, buttonResponse, pickerCanceled = false, validation, chosen } = input
  const isLastAttempt = attempt >= maxRetry

  // 1) 默认按钮
  if (buttonResponse === 0) {
    return { action: 'use-default' }
  }

  // 2) 立即选择：开了 picker，但用户可能又取消了
  if (buttonResponse === 1) {
    if (pickerCanceled || !chosen) {
      // 选了但又取消 → 走重选/退出
      return isLastAttempt ? { action: 'quit' } : { action: 'show-retry-or-quit' }
    }
    // 选了路径 → 校验
    if (!validation) {
      throw new Error('picker 选了路径但未提供 validation 结果')
    }
    if (validation.ok) {
      return { action: 'confirm', chosen }
    }
    return { action: 'show-validation-error', chosen, reason: validation.reason ?? '未知原因' }
  }

  // 3) 取消按钮
  return isLastAttempt ? { action: 'quit' } : { action: 'show-retry-or-quit' }
}

/**
 * 根据上一步 action 决定 retry-or-quit 弹窗响应：
 *  - 'retry' → 回到循环顶部（attempt 不变，进入下一轮）
 *  - 'quit' → 终止
 */
export function decideRetryOrQuit(response: 0 | 1): DataDirChoiceAction {
  // 0 = 重新选择，1 = 退出
  return response === 0 ? { action: 'retry' } : { action: 'quit' }
}