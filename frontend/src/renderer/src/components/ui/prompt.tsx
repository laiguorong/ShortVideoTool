import { useEffect, useState } from 'react'
import { Dialog } from './dialog'
import { Button } from './button'

/**
 * 单行文本输入弹窗 —— 替代 window.prompt（Electron 渲染进程不实现 prompt，调用只会返回 null）。
 * 用法：先挂一个状态 `const prompt = usePrompt()`，再 `await prompt('标题', '默认值')` 拿结果；
 * 用户取消返回 null。同页面多处复用共用一个实例即可（后打开的覆盖前一个）。
 */
export function usePrompt() {
  const [state, setState] = useState<{
    resolve: (v: string | null) => void
    title: string
    value: string
    placeholder?: string
  } | null>(null)

  // 打开弹窗：返回 Promise，确认 resolve(输入值)，取消 resolve(null)
  const open = (title: string, value = '', placeholder?: string) =>
    new Promise<string | null>((resolve) => setState({ resolve, title, value, placeholder }))

  // 渲染层：无请求时不占位
  const dialog = state ? (
    <PromptDialogImpl
      title={state.title}
      initialValue={state.value}
      placeholder={state.placeholder}
      onDone={(v) => {
        state.resolve(v)
        setState(null)
      }}
    />
  ) : null

  return [open, dialog] as const
}

/** 输入弹窗实现（内部组件，勿直接使用） */
function PromptDialogImpl({ title, initialValue, placeholder, onDone }: {
  title: string
  initialValue: string
  placeholder?: string
  onDone: (v: string | null) => void
}) {
  const [value, setValue] = useState(initialValue)

  // 弹窗每次打开重置为初始值
  useEffect(() => { setValue(initialValue) }, [initialValue])

  return (
    <Dialog
      open
      onOpenChange={(v) => !v && onDone(null)}
      title={title}
      width={440}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onDone(null)}>取消</Button>
          <Button size="sm" onClick={() => onDone(value)}>确定</Button>
        </>
      }
    >
      <input
        className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm focus:border-accent focus:outline-none"
        value={value}
        placeholder={placeholder}
        onChange={(e) => setValue(e.target.value)}
        // 回车直接提交
        onKeyDown={(e) => { if (e.key === 'Enter') onDone(value) }}
        autoFocus
        // 打开即全选，便于直接覆盖输入
        onFocus={(e) => e.target.select()}
      />
    </Dialog>
  )
}
