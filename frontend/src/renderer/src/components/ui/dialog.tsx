import { type ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { cn } from '@/lib/utils'
import { Button } from './button'

/** 模态弹窗（UI-2：表单类 560~720px；破坏性确认按钮红色 UI-3）
 * #414 新增 `side='right'` 走右侧抽屉样式 */
export function Dialog({
  open,
  onOpenChange,
  title,
  children,
  footer,
  width = 560,
  height,
  side = 'center',
  stickyHeader,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  children: ReactNode
  footer?: ReactNode
  width?: number
  /** 固定高度（可选）：传入后头/尾固定，仅中间内容区滚动 */
  height?: number
  /** 弹窗位置。center 居中弹窗；right 右侧抽屉（width 默认更窄、高度撑满）。 */
  side?: 'center' | 'right'
  /** 粘性头部内容（如进度指示条）；紧贴标题栏下方、中间滚动区上方，
   *  会随 title 一起随 dialog 上下滚动而不消失。需要同时传 height 才会真正"固定"。 */
  stickyHeader?: ReactNode
}) {
  // 样式分支：居中 vs 右抽屉
  const positionCls = side === 'right'
    ? cn(
      'right-0 top-0 h-screen max-h-screen translate-x-0 translate-y-0 rounded-l-lg',
      'flex flex-col overflow-hidden',
    )
    : cn(
      'left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-lg',
      height ? 'flex flex-col overflow-hidden' : 'overflow-auto',
    )

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <DialogPrimitive.Content
          style={side === 'right' ? { width } : { width, ...(height ? { height } : {}) }}
          className={cn(
            'fixed z-50 bg-background-elev shadow-xl transition-transform',
            positionCls,
          )}
          onPointerDownOutside={(e) => e.preventDefault()}
        >
          <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
            <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close className="rounded p-1 text-muted-foreground hover:bg-muted">
              ✕
            </DialogPrimitive.Close>
          </div>
          {/* 可选粘性头部槽位：标题栏下方、滚动区上方，仅在 center + height 模式下会真正"随 dialog 固定"
              #429：justify-center 让 inline-flex 子元素水平居中（子元素需用 inline-flex + mx-auto 才能真正居中） */}
          {stickyHeader && (
            <div className="flex shrink-0 justify-center border-b border-border bg-background-elev px-5 py-3">
              {stickyHeader}
            </div>
          )}
          <div className={cn(
            // side='right' 走 flex 列布局时必须 min-h-0 + flex-1 + overflow-y-auto，
            // 否则内部 max-h 容器无法触发滚动条（flex 子项默认 min-height: auto 会撑破 max-h-screen）
            side === 'right' || height ? 'min-h-0 flex-1 overflow-y-auto' : '',
            'px-5 py-4',
          )}>{children}</div>
          {footer && <div className="flex shrink-0 justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/** 二次确认弹窗（危险操作） */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  content,
  danger,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  content: string
  danger?: boolean
  onConfirm: () => void
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      width={440}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            size="sm"
            onClick={() => {
              onConfirm()
              onOpenChange(false)
            }}
          >
            确认
          </Button>
        </>
      }
    >
      <p className={cn('text-sm', danger && 'text-danger')}>{content}</p>
    </Dialog>
  )
}
