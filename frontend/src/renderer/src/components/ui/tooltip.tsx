/**
 * 轻量 Tooltip：hover/focus 触发，200ms 延迟显示，无外部依赖。
 * 用途：选项右侧问号说明（#110/#112/#115/#117/#118/#119）。
 *
 * #117：显式 text-[12px]，不被主题字体覆盖。
 * #118：气泡三角位置跟随 trigger 中心（clamp 后 tooltip 偏左/偏右，三角不再死板居中）。
 *
 * 用法：
 *   <Tooltip content="说明文字"><span className="help-icon">ⓘ</span></Tooltip>
 *
 * ⚠ 契约：trigger 子元素必须带 `.help-icon` class——Tooltip 内部
 * 用 querySelector('.help-icon') 锁定真实指向点（避免父 span 的 padding
 * 导致 bbox 偏移 2~4px）。如果想自定义锚点，必须保留这个 class。
 *
 * 关键实现要点：
 *   - createPortal 渲染到 body，脱离 overflow-hidden 父级（#119）
 *   - position 用 useLayoutEffect 计算（DOM 已挂载）
 *   - 用 useRef + JSON.stringify 比较位置，避免无限循环（#119 关键修复）
 *   - viewport-aware：trigger 顶部空间不够则自动切到下方
 *   - 视口边界 clamp：左右各留 8px 安全距离
 *   - 三角水平位置独立于 tooltip，指向 trigger 真实中心（#118）
 */

import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'

interface TooltipProps {
  content: React.ReactNode
  children: React.ReactNode
  delay?: number
  side?: 'top' | 'bottom'
}

interface Position {
  top: number
  left: number
  /** 三角相对 tooltip 的水平偏移（px），指向 trigger 中心（#118） */
  arrowLeft: number
  side: 'top' | 'bottom'
}

const MARGIN = 8
/** 三角左右两端到 tooltip 边的最小距离，避免三角被 clamp 拉到边缘外（#118） */
const ARROW_INSET = 10

export function Tooltip({ content, children, delay = 200, side = 'top' }: TooltipProps) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<Position | null>(null)
  const triggerRef = useRef<HTMLSpanElement>(null)
  const tooltipRef = useRef<HTMLSpanElement>(null)
  const timer = useRef<number | null>(null)

  // open 状态变化时，tooltip 挂载后用 useLayoutEffect 计算位置
  // 只在 [open] 变化时跑，不会循环（pos 变化不会触发此 effect）
  useLayoutEffect(() => {
    if (!open) {
      setPos(null)
      return
    }
    if (!triggerRef.current || !tooltipRef.current) return
    // #118：定位以可见 ⓘ 为准。trigger 父 span 偶有 padding/对齐导致 bbox 偏 2~4px，
    // 找首个子 .help-icon（ⓘ 圆点）作为指向目标，箭头才能精确对准 ⓘ
    const target = (triggerRef.current.querySelector('.help-icon') as HTMLElement | null)
      || triggerRef.current
    const tr = target.getBoundingClientRect()
    const tipH = tooltipRef.current.offsetHeight
    const tipW = tooltipRef.current.offsetWidth
    const spaceTop = tr.top
    const spaceBottom = window.innerHeight - tr.bottom
    const useBottom = side === 'bottom' || (spaceTop < tipH + MARGIN && spaceBottom > spaceTop)
    const finalSide: 'top' | 'bottom' = useBottom ? 'bottom' : 'top'
    const minLeft = MARGIN
    const maxLeft = window.innerWidth - tipW - MARGIN
    const desiredCenter = tr.left + tr.width / 2
    const left = Math.max(minLeft, Math.min(maxLeft, desiredCenter - tipW / 2))
    const top = finalSide === 'top' ? tr.top - tipH - MARGIN : tr.bottom + MARGIN
    // #118：三角水平位置 = trigger 中心 − tooltip.left，并约束在 [ARROW_INSET, tipW-ARROW_INSET]
    // 这样 clamp 把 tooltip 推到视口边缘时，三角仍指向 trigger，不会跑到 tooltip 边外
    const arrowLeft = Math.max(ARROW_INSET, Math.min(tipW - ARROW_INSET, desiredCenter - left))
    setPos({ top, left, arrowLeft, side: finalSide })
  }, [open, side])

  const show = () => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOpen(true), delay)
  }
  const hide = () => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOpen(false), 80)
  }

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current)
  }, [])

  const arrowShapeCls = pos?.side === 'bottom'
    ? '[clip-path:polygon(50%_0,100%_100%,0_100%)]'
    : '[clip-path:polygon(0_0,100%_0,50%_100%)]'

  return (
    <span
      ref={triggerRef}
      className="relative inline-block align-middle"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {open && createPortal(
        <span
          ref={tooltipRef}
          role="tooltip"
          className="pointer-events-none fixed z-[100] w-max max-w-[260px] whitespace-normal break-words rounded-xl bg-gray-900 px-3 py-2 text-[12px] leading-relaxed text-white shadow-xl ring-1 ring-black/5"
          style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999, visibility: 'hidden' }}
        >
          {content}
          {pos && (
            <span
              aria-hidden
              className={
                'absolute h-3 w-3 bg-gray-900 ' + arrowShapeCls + ' ' +
                (pos.side === 'top'
                  ? 'bottom-[-5px] -translate-x-1/2'
                  : 'top-[-5px] -translate-x-1/2')
              }
              // #118：三角水平偏移独立定位（不再写死 50%）
              style={{ left: pos.arrowLeft }}
            />
          )}
        </span>,
        document.body
      )}
    </span>
  )
}