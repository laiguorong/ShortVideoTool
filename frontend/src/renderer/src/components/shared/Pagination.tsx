import { useState } from 'react'
import { cn } from '@/lib/utils'

/** 每页记录数可选项 */
const PAGE_SIZE_OPTIONS = [20, 50, 100]

/** 生成页码序列：当前页居中，首尾固定，中间省略（如 1 … 4 5 6 … 20） */
function pageSeq(page: number, totalPages: number): (number | string)[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1)
  const items: (number | string)[] = [1]
  const left = Math.max(2, page - 1)
  const right = Math.min(totalPages - 1, page + 1)
  if (left > 2) items.push('…l')
  for (let i = left; i <= right; i++) items.push(i)
  if (right < totalPages - 1) items.push('…r')
  items.push(totalPages)
  return items
}

/** 分页器（右下分页：共 N 条 + 每页条数 + 数字页码 + 快捷跳转） */
export function Pagination({
  page,
  pageSize,
  total,
  onChange,
  onPageSizeChange,
}: {
  page: number
  pageSize: number
  total: number
  onChange: (page: number) => void
  /** 每页记录数切换回调（可选，不传则不显示切换器） */
  onPageSizeChange?: (size: number) => void
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const [jump, setJump] = useState('')
  /** 数字页码按钮 */
  const PageBtn = ({ n }: { n: number }) => (
    <button
      className={cn('h-7 min-w-7 rounded-md px-1.5 text-sm leading-none',
        page === n
          ? 'bg-accent font-medium text-white'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground')}
      onClick={() => onChange(n)}
    >
      {n}
    </button>
  )
  const doJump = () => {
    const n = parseInt(jump, 10)
    if (!Number.isNaN(n)) onChange(Math.max(1, Math.min(totalPages, n)))
    setJump('')
  }
  return (
    <div className="flex items-center justify-end gap-3 py-3 text-sm text-muted-foreground">
      <span>共 {total} 条</span>
      {onPageSizeChange && (
        <select
          className="h-7 rounded-md border border-border bg-white px-1.5 text-xs"
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>{n} 条/页</option>
          ))}
        </select>
      )}
      <div className="flex items-center gap-1">
        <button
          className="h-7 rounded-md px-2 text-sm leading-none disabled:opacity-40"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
        >
          ‹
        </button>
        {pageSeq(page, totalPages).map((item) =>
          typeof item === 'number'
            ? <PageBtn key={item} n={item} />
            : <span key={item} className="px-1 text-sm">…</span>,
        )}
        <button
          className="h-7 rounded-md px-2 text-sm leading-none disabled:opacity-40"
          disabled={page >= totalPages}
          onClick={() => onChange(page + 1)}
        >
          ›
        </button>
      </div>
      <div className="flex items-center gap-1">
        <span>跳至</span>
        <input
          type="number"
          min={1}
          max={totalPages}
          value={jump}
          onChange={(e) => setJump(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && doJump()}
          className="h-7 w-14 rounded-md border border-border bg-white px-1.5 text-center text-sm"
        />
        <span>页</span>
        <button
          className="h-7 rounded-md border border-border bg-white px-2 text-sm hover:bg-muted"
          onClick={doJump}
        >
          GO
        </button>
      </div>
    </div>
  )
}
