import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { creationApi, type Project, type ProjectRow, type Shot, type Clip, type GeneratedVideo, type BgmItem } from '@/api/creation'
import { publishIntroApi } from '@/api/publish_intro'
import { fileUrl, request } from '@/api/client'
import { materialApi, type Material, type Category } from '@/api/material'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Pagination } from '@/components/shared/Pagination'
import { toast } from '@/components/ui/toast'
import { usePrompt } from '@/components/ui/prompt'
import { Tooltip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { ProjectShopsDrawer } from './ProjectShopsDrawer'
import { ProjectIntrosDrawer } from './ProjectIntrosDrawer'

// 「未分类」虚拟分类 ID（与后端 UNCATEGORIZED_ID 保持一致）
const UNCATEGORIZED_ID = '-'

/** 素材选择器：待选列表每页条数（#87/#89 服务端分页，默认 1000） */
const PICKER_PAGE_SIZE = 1000
/** 素材选择器：已选素材数量上限（#87） */
const PICKER_MAX_SELECTED = 50
/** 单分镜片段上限（与后端 creation_service.MAX_CLIPS_PER_SHOT 一致，#88 预判用） */
const MAX_CLIPS_PER_SHOT = 200

/** 画面模式预览 SVG（#430）：40×40 viewBox，三种颜色块+圆形+横条代表视频帧，叠加各模式视觉特征 */
function fitModePreview(mode: string): JSX.Element {
  switch (mode) {
    case 'cover': // 缩放铺满 → 上下被裁掉
      return (
        <svg viewBox="0 0 40 40" className="h-full w-full">
          <rect x="0" y="0" width="40" height="40" rx="3" fill="#1e3a8a" />
          <rect x="-5" y="6" width="50" height="28" fill="#60a5fa" />
          <circle cx="20" cy="14" r="5" fill="#fbbf24" />
          <rect x="6" y="20" width="28" height="6" fill="#1e40af" />
          <rect x="6" y="27" width="28" height="6" fill="#1e40af" />
        </svg>
      )
    case 'contain': // 等比缩放居中，上下黑边
      return (
        <svg viewBox="0 0 40 40" className="h-full w-full">
          <rect x="0" y="0" width="40" height="40" rx="3" fill="#0f172a" />
          <rect x="12" y="6" width="16" height="28" fill="#60a5fa" />
          <circle cx="20" cy="13" r="3" fill="#fbbf24" />
          <rect x="14" y="18" width="12" height="3" fill="#1e40af" />
          <rect x="14" y="22" width="12" height="3" fill="#1e40af" />
          <rect x="14" y="26" width="12" height="3" fill="#1e40af" />
        </svg>
      )
    case 'fill': // 直接拉伸，圆形变椭圆
      return (
        <svg viewBox="0 0 40 40" className="h-full w-full">
          <rect x="0" y="0" width="40" height="40" rx="3" fill="#7c3aed" />
          <rect x="2" y="6" width="36" height="28" fill="#a78bfa" />
          <ellipse cx="20" cy="14" rx="6" ry="4" fill="#fbbf24" />
          <rect x="4" y="20" width="32" height="6" fill="#5b21b6" />
          <rect x="4" y="28" width="32" height="6" fill="#5b21b6" />
        </svg>
      )
    case 'blur_bg': // 模糊背景 + 前景居中
      return (
        <svg viewBox="0 0 40 40" className="h-full w-full">
          <rect x="0" y="0" width="40" height="40" rx="3" fill="#1e40af" />
          <circle cx="6" cy="6" r="10" fill="#60a5fa" opacity="0.4" />
          <circle cx="32" cy="32" r="12" fill="#93c5fd" opacity="0.3" />
          <rect x="0" y="20" width="40" height="20" fill="#1e3a8a" opacity="0.5" />
          <rect x="11" y="6" width="18" height="28" fill="#3b82f6" />
          <circle cx="20" cy="14" r="3" fill="#fbbf24" />
          <rect x="13" y="19" width="14" height="3" fill="#1e40af" />
          <rect x="13" y="23" width="14" height="3" fill="#1e40af" />
          <rect x="13" y="27" width="14" height="3" fill="#1e40af" />
        </svg>
      )
    default:
      return <div className="h-full w-full bg-muted" />
  }
}

/** 画幅预览 SVG（#430）：按真实 W:H 输出四边形（外框灰底+内框蓝填充） */
function resolutionPreview(w: number, h: number): JSX.Element {
  // 用 40×40 viewBox，根据比例算出内框
  const padding = 4
  const maxW = 40 - padding * 2
  const maxH = 40 - padding * 2
  const ratio = w / h
  let bw = maxW
  let bh = maxH
  if (ratio > 1) bh = maxW / ratio
  else bw = maxH * ratio
  const bx = (40 - bw) / 2
  const by = (40 - bh) / 2
  return (
    <svg viewBox="0 0 40 40" className="h-full w-full">
      <rect x="0" y="0" width="40" height="40" rx="3" fill="#f3f4f6" />
      <rect x={bx} y={by} width={bw} height={bh} rx="2" fill="#60a5fa" stroke="#1e40af" strokeWidth="1.5" />
    </svg>
  )
}

/** 预览下拉（#430）：触发按钮显示当前项的预览缩略图；popover 内列出所有项，每项含预览+标签。
 *
 * #430 修复：
 * - createPortal 渲染到 body + position: fixed，跳出 Dialog 容器 overflow 裁剪与 stacking context 限制
 * - 选项 mousedown 直接触发选择 + 阻止默认 + 阻止冒泡，避开 Dialog/Radix 在 document 上的 mousedown listener
 * - popover 容器 stopPropagation(mousedown) 与 stopPropagation(wheel, capture)，
 *   避免 Radix Dialog 的 wheel/outside-click 拦截导致选项点击失效和滚动失效
 * - backdrop 全屏透明层用于点击空白处关闭
 * - options: { value, label, sub?, preview: JSX.Element }[]
 */
function PreviewSelect({ value, onChange, options, width = 'flex-1', previewSize = 32 }: {
  value: string
  onChange: (v: string) => void
  options: Array<{ value: string; label: string; sub?: string; preview: JSX.Element }>
  width?: string
  previewSize?: number
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const popRef = useRef<HTMLDivElement | null>(null)
  const current = options.find((o) => o.value === value)

  // 打开时计算位置；滚动/resize 重算
  useLayoutEffect(() => {
    if (!open) { setPos(null); return }
    const recalc = () => {
      const btn = triggerRef.current
      const pop = popRef.current
      if (!btn || !pop) return
      const r = btn.getBoundingClientRect()
      const popH = pop.offsetHeight || 240
      const spaceBelow = window.innerHeight - r.bottom
      const top = spaceBelow >= popH + 8 ? r.bottom + 4 : Math.max(8, r.top - popH - 4)
      const width = Math.max(240, r.width)
      const maxLeft = window.innerWidth - width - 8
      const left = Math.min(maxLeft, Math.max(8, r.left))
      setPos({ top, left, width })
    }
    recalc()
    window.addEventListener('resize', recalc)
    window.addEventListener('scroll', recalc, true)
    return () => {
      window.removeEventListener('resize', recalc)
      window.removeEventListener('scroll', recalc, true)
    }
  }, [open])

  return (
    <div className={width}>
      <button ref={triggerRef} type="button"
        // 用 mousedown 早于 document listener 触发切换，避免 Radix outside-click 抢先把 popover 关掉
        onMouseDown={(e) => { e.preventDefault(); setOpen((o) => !o) }}
        className={'flex h-10 w-full items-center gap-3 rounded-md border border-border bg-white px-3 text-base ' +
          (open ? 'ring-2 ring-blue-200' : 'hover:border-blue-300')}>
        <span className="shrink-0 rounded border border-border bg-muted/30"
          style={{ width: previewSize, height: previewSize }}>
          {current?.preview}
        </span>
        <span className="flex flex-1 flex-col items-start text-left leading-tight">
          <span className="font-medium">{current?.label ?? ''}</span>
          {current?.sub && <span className="text-xs text-muted-foreground">{current.sub}</span>}
        </span>
        <span className={'text-muted-foreground transition ' + (open && 'rotate-180')}>▾</span>
      </button>
      {open && createPortal(
        <>
          {/* 全屏透明背景：点击空白处关闭；mousedown stopPropagation 防止被 Dialog/Radix 当作 outside-click 关掉
           * #430 fix 4：Radix Dialog 给 body 设 pointer-events:none，backdrop 同样被禁用 → 内联 pointer-events:auto 恢复关闭交互 */}
          <div onMouseDown={(e) => { e.stopPropagation(); setOpen(false) }}
            style={{ position: 'fixed', inset: 0, zIndex: 9998, pointerEvents: 'auto' }} />
          <div ref={popRef}
            // #430 fix 2：Radix Dialog open 时给 body 加 pointer-events:none 阻止整个页面交互，popover 用 portal 挂在 body 下被一并禁用。
            //   内联 pointer-events:auto 强制恢复点击/滚动/wheel。
            // #430 fix 3：Radix Dialog 在 window 注册 wheel handler 并 preventDefault()，阻止 popover 内部滚动；
            //   用 wheel capture stopPropagation 抢在 Radix 之前消费事件，让 popover 内 wheel 不冒泡到 window。
            style={{
              ...(pos ? { top: pos.top, left: pos.left, width: pos.width } : { top: -9999, left: -9999, visibility: 'hidden' }),
              pointerEvents: 'auto',
            }}
            // mousedown stopPropagation：防止 Radix 监听到 document 时误判为 outside-click 并关弹窗
            onMouseDown={(e) => e.stopPropagation()}
            // wheel native capture：阻止冒泡到 Radix window handler，保留 popover 内部滚动默认行为
            onWheelCapture={(e) => e.stopPropagation()}
            className="fixed z-[9999] max-h-72 overflow-auto rounded-md border border-border bg-white shadow-lg">
            {options.map((o) => {
              const active = o.value === value
              return (
                <button key={o.value} type="button"
                  // onMouseDown 早于 onClick 且先 setOpen(false)；preventDefault 阻止后续 click 抢焦点导致 Dialog 重新 focus trap
                  onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); onChange(o.value); setOpen(false) }}
                  className={
                    'flex w-full items-center gap-3 px-3 py-2 text-left transition ' +
                    (active ? 'bg-blue-50' : 'hover:bg-muted')
                  }>
                  <span className="shrink-0 rounded border border-border bg-muted/30"
                    style={{ width: previewSize + 16, height: previewSize + 16 }}>
                    {o.preview}
                  </span>
                  <span className="flex flex-1 flex-col leading-tight">
                    <span className={'text-base font-medium ' + (active ? 'text-blue-700' : 'text-foreground')}>{o.label}</span>
                    {o.sub && <span className="text-xs text-muted-foreground">{o.sub}</span>}
                  </span>
                  {active && <span className="text-blue-600">✓</span>}
                </button>
              )
            })}
          </div>
        </>,
        document.body,
      )}
    </div>
  )
}
/** 素材选择器：搜索关键词防抖（#91） */
const SEARCH_DEBOUNCE_MS = 300

/** 素材选择弹窗（#85/#87/#88/#89/#90/#91/#93/#389） */
function MaterialPicker({ open, onOpenChange, type, multi, initialSelected, existingIds, projectOrientation, onConfirm }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  type: 'video' | 'music'
  multi: boolean
  /**
   * 重新打开弹窗时回显的已选素材（#93）。
   * 父组件在外部缓存已选详情，每次打开弹窗时传入；cancel 关弹不传新值即可保留。
   */
  initialSelected?: Material[]
  /**
   * #389：当前项目已存在/已使用过的素材 ID 集合。仅用于在待选列表行做警示打勾,
   * 不拦截选择/确认（用户可正常挑选并重复使用）。
   */
  existingIds?: string[]
  /**
   * #440：项目画幅对应的 orientation 过滤。
   * - 'vertical'  竖屏项目（h > w），待选列表仅显示竖屏素材
   * - 'horizontal' 横屏项目（w > h），待选列表仅显示横屏素材
   * - null / undefined（方形或无 project）不过滤
   * 仅对 type==='video' 生效；BGM 不受此参数影响。
   */
  projectOrientation?: 'vertical' | 'horizontal' | null
  /**
   * 确认回调。
   * 参数:
   *   ids: 已选素材 ID（按选择顺序）
   *   materials: 已选素材的详情（按 ids 顺序，可能少于 ids——已选 ID 不在本次会话加载过则无详情）
   */
  onConfirm: (ids: string[], materials: Material[]) => void
}) {
  const [categories, setCategories] = useState<Category[]>([])
  const [activeCat, setActiveCat] = useState('')                 // 当前分类筛选（空=全部）
  const [page, setPage] = useState(1)                            // 当前页码（#89 服务端分页）
  const [pageList, setPageList] = useState<Material[]>([])       // 当前页素材
  const [pageTotal, setPageTotal] = useState(0)                  // 当前分类下素材总数（服务端返回）
  // 全部素材总数（#90）：仅在拉"全部分类"时更新，切到子分类不会变，保持固定显示
  const [allTotal, setAllTotal] = useState(0)
  const [selectedIds, setSelectedIds] = useState<string[]>([])   // 已选（保持选择顺序，跨分类也保留）
  // 已加载过的素材详情缓存：跨分类、跨页累积，确保已选列表在切走分类后仍能展示标题/时长
  const [loadedMap, setLoadedMap] = useState<Map<string, Material>>(new Map())
  const [loadingPage, setLoadingPage] = useState(false)
  // 搜索关键词（#91）：输入即时反馈，debounce 后才触发请求
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')

  // 打开弹窗：拉分类 + 第一页（全部分类）素材，并回显上次已选（#93）
  useEffect(() => {
    if (open) {
      const seed = initialSelected ?? []
      setSelectedIds(seed.map((m) => m.id))
      // 预填 detail 缓存，确保已选列表立即回显标题/时长
      setLoadedMap(new Map(seed.map((m) => [m.id, m])))
      setActiveCat('')
      setPage(1)
      setKeywordInput('')
      setKeyword('')
      materialApi.categories(type, projectOrientation).then(setCategories).catch((e) => console.warn('[Creation] 请求失败:', e))
      loadPage('', 1)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, type])

  /**
   * 拉取指定分类下指定页的素材（#89 服务端分页）。
   * @param catId 分类 ID（空=全部）
   * @param p 页码（1 起）
   * 详情统一追加到 loadedMap（不再 reset；打开弹窗时已显式清空缓存）
   */
  const loadPage = async (catId: string, p: number) => {
    setLoadingPage(true)
    try {
      const params: Record<string, string> = {
        type,
        page: String(p),
        page_size: String(PICKER_PAGE_SIZE),
      }
      if (catId) params.category_id = catId
      if (keyword) params.keyword = keyword
      // #440：项目画幅过滤（仅视频；方形或未指定则不过滤）
      if (type === 'video' && projectOrientation) params.orientation = projectOrientation
      const r = await materialApi.list(params)
      setPageList(r.list)
      setPageTotal(r.total)
      // 仅在拉"全部分类"时刷新全部素材总数（#90）
      if (!catId) setAllTotal(r.total)
      // 详情统一追加（打开弹窗已 setLoadedMap(new Map()) 清空）
      setLoadedMap((prev) => {
        const next = new Map(prev)
        for (const m of r.list) next.set(m.id, m)
        return next
      })
    } catch {
      // 失败保持原列表，不打断用户
    } finally {
      setLoadingPage(false)
    }
  }

  // 分类切换 → 重新拉第一页
  useEffect(() => {
    if (!open) return
    setPage(1)
    loadPage(activeCat, 1)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCat])

  // 页码变化 → 拉对应页
  useEffect(() => {
    if (!open) return
    loadPage(activeCat, page)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page])

  // 搜索关键词 debounce（#91）：输入停止 300ms 后生效
  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => setKeyword(keywordInput.trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [keywordInput, open])

  // 关键词变化 → 重拉第一页（不重置 loadedMap，保留已选 detail）
  useEffect(() => {
    if (!open) return
    setPage(1)
    loadPage(activeCat, 1)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword])

  const catName = (id: string) => {
    if (id === UNCATEGORIZED_ID) return '未分类'
    return categories.find((c) => c.id === id)?.name ?? ''
  }
  const childrenOf = (pid: string) => categories.filter((c) => c.parent_id === pid)

  // 当前页内未选的素材（分页只翻服务端返回的当前页，跨页全选通过翻页累积）
  const selectedSet = new Set(selectedIds)
  // #389：当前项目已用过的素材 ID 集合 → 待选行打灰勾，仅警示不拦截
  const existingSet = new Set(existingIds ?? [])
  const candidates = pageList.filter((m) => !selectedSet.has(m.id))
  // 已选项从跨页 detail 缓存里取，不受当前分类筛选影响（已选可能是其他分类的）
  const selectedItems = selectedIds
    .map((id) => loadedMap.get(id))
    .filter((m): m is Material => Boolean(m))

  // 分页（#89）：页码越界时自动回退到最后一页
  const totalPages = Math.max(1, Math.ceil(pageTotal / PICKER_PAGE_SIZE))
  const safePage = Math.min(page, totalPages)
  const pageCandidates = candidates

  /** 加入已选（单选模式先清空；超出上限则拒绝并提示） */
  const addMaterial = (id: string) => {
    if (selectedIds.includes(id)) return
    if (!multi) {
      setSelectedIds([id])
      return
    }
    if (selectedIds.length >= PICKER_MAX_SELECTED) {
      toast(`最多只能选择 ${PICKER_MAX_SELECTED} 个素材`, 'error')
      return
    }
    setSelectedIds([...selectedIds, id])
  }

  /**
   * 全选当前页待选素材（受上限约束）。
   * 注：服务端分页下"全选"仅作用于当前页，跨页需手动翻页再选。
   */
  const selectAllCandidates = () => {
    const next = multi ? [...selectedIds] : []
    let hitLimit = false
    for (const m of candidates) {
      if (next.length >= PICKER_MAX_SELECTED) { hitLimit = true; break }
      if (!next.includes(m.id)) next.push(m.id)
    }
    if (hitLimit) toast(`最多只能选择 ${PICKER_MAX_SELECTED} 个素材，已选满`, 'error')
    setSelectedIds(next)
  }
  /** 从已选移除 */
  const removeMaterial = (id: string) => setSelectedIds((prev) => prev.filter((x) => x !== id))

  /** 递归渲染分类树节点（含缩进与数量） */
  const renderCatNodes = (nodes: Category[], depth: number): React.ReactNode =>
    nodes.map((c) => (
      <div key={c.id}>
        <button type="button"
          className={cn('flex w-full items-center gap-1 rounded py-1 pr-1.5 text-left text-xs hover:bg-muted',
            activeCat === c.id && 'bg-blue-50 font-medium text-accent')}
          style={{ paddingLeft: depth * 12 + 6 }}
          onClick={() => setActiveCat(activeCat === c.id ? '' : c.id)}
          title={c.name}>
          <span className="min-w-0 flex-1 truncate">{c.name}</span>
          <span className="shrink-0 text-muted-foreground">{c.count}</span>
        </button>
        {renderCatNodes(childrenOf(c.id), depth + 1)}
      </div>
    ))

  const label = type === 'video' ? '视频' : '音乐'

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={`选择${label}`} width={900}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={selectedIds.length === 0}
            onClick={() => {
              // 确认时按 ids 顺序返回已选详情；丢失去掉跨分类从未加载过的 ID（理论上不会发生，因 ID 来自已加载页）
              const mats = selectedIds
                .map((id) => loadedMap.get(id))
                .filter((m): m is Material => Boolean(m))
              onConfirm(selectedIds, mats)
              onOpenChange(false)
            }}>
            确定（已选 {selectedIds.length}）
          </Button>
        </>
      }>
      <div className="flex gap-3" style={{ height: 440 }}>
        {/* 左：分类选择器（树形，名称右侧为素材数量） */}
        <div className="w-48 shrink-0 overflow-auto rounded-md border border-border bg-white p-1.5">
          <div className="mb-1 px-1.5 text-xs font-medium text-muted-foreground">分类</div>
          <button type="button"
            className={cn('flex w-full items-center rounded px-1.5 py-1 text-left text-xs hover:bg-muted',
              activeCat === '' && 'bg-blue-50 font-medium text-accent')}
            onClick={() => setActiveCat('')}>
            <span className="min-w-0 flex-1 truncate">全部{label}</span>
            {/* #90：固定显示所有素材总数，不随当前分类变化 */}
            <span className="shrink-0 text-muted-foreground">{allTotal}</span>
          </button>
          {renderCatNodes(childrenOf(''), 0)}
          {categories.length === 0 && (
            <div className="px-1.5 py-2 text-xs text-muted-foreground">暂无分类</div>
          )}
        </div>

        {/* 中：待选列表（#89 服务端分页） */}
        <div className="flex min-w-0 flex-1 flex-col rounded-md border border-border bg-white">
          {/* #91：搜索框 + 数量 + 全选（搜索输入 debounce 300ms 后生效） */}
          <div className="flex shrink-0 flex-col gap-1 border-b border-border px-2 py-1.5">
            {/* #440：项目画幅过滤提示（仅视频 + 有效 orientation 时显示） */}
            {type === 'video' && projectOrientation && (
              <div className="rounded border border-blue-200 bg-blue-50 px-2 py-1 text-[11px] text-blue-700">
                已按项目画幅过滤：仅显示
                {projectOrientation === 'vertical' ? '竖屏' : '横屏'}
                视频（方形项目不过滤）
              </div>
            )}
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium whitespace-nowrap">待选（共 {pageTotal} 条）</span>
              <button type="button" className="ml-auto text-xs text-accent disabled:text-muted-foreground"
                disabled={candidates.length === 0}
                onClick={selectAllCandidates}>全选当前页</button>
            </div>
            <input type="text" placeholder="搜索素材名称…" value={keywordInput}
              onChange={(e) => setKeywordInput(e.target.value)}
              className="h-7 w-full rounded-md border border-border bg-white px-2 text-xs" />
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {loadingPage && pageList.length === 0 ? (
              <div className="py-10 text-center text-xs text-muted-foreground">加载中…</div>
            ) : pageCandidates.map((m) => (
              <button key={m.id} type="button"
                className="flex w-full items-center gap-2 border-b border-border px-2 py-1.5 text-left text-xs last:border-0 hover:bg-muted/60"
                onClick={() => addMaterial(m.id)} title={m.title}>
                {/* #389：当前项目已用过的素材 → 标题前灰勾，hover 提示，不拦截勾选/确认 */}
                {existingSet.has(m.id) && (
                  <Tooltip content="已存在当前项目（仅警示，可正常选择）">
                    <span aria-label="已存在当前项目" className="shrink-0 text-muted-foreground/60">✓</span>
                  </Tooltip>
                )}
                <span className="min-w-0 flex-1 truncate">{m.title}</span>
                <span className="w-20 shrink-0 truncate text-muted-foreground">{catName(m.category_id)}</span>
                <span className="w-10 shrink-0 text-right text-muted-foreground">
                  {m.duration_ms ? `${(m.duration_ms / 1000).toFixed(0)}s` : '-'}
                </span>
                <span className="w-14 shrink-0 text-right text-muted-foreground">
                  {(m.file_size / 1048576).toFixed(1)}MB
                </span>
              </button>
            ))}
            {!loadingPage && candidates.length === 0 && pageTotal === 0 && (
              <div className="py-10 text-center text-xs text-muted-foreground">素材库暂无{label}</div>
            )}
            {!loadingPage && candidates.length === 0 && pageTotal > 0 && (
              <div className="py-10 text-center text-xs text-muted-foreground">当前页素材已全部加入已选</div>
            )}
          </div>
          {/* 待选列表翻页器（#87/#89：默认每页 1000 条） */}
          {pageTotal > 0 && (
            <div className="flex shrink-0 items-center gap-2 border-t border-border px-2 py-1.5 text-xs text-muted-foreground">
              <span>第 {safePage} / {totalPages} 页（每页 {PICKER_PAGE_SIZE} 条）</span>
              <div className="ml-auto flex items-center gap-1">
                <button type="button" className="h-6 rounded px-1.5 leading-none hover:bg-muted disabled:opacity-40"
                  disabled={safePage <= 1} onClick={() => setPage(safePage - 1)}>‹ 上一页</button>
                <button type="button" className="h-6 rounded px-1.5 leading-none hover:bg-muted disabled:opacity-40"
                  disabled={safePage >= totalPages} onClick={() => setPage(safePage + 1)}>下一页 ›</button>
              </div>
            </div>
          )}
        </div>

        {/* 右：已选列表 */}
        <div className="flex w-56 shrink-0 flex-col rounded-md border border-border bg-white">
          <div className="flex shrink-0 items-center gap-2 border-b border-border px-2 py-1.5">
            <span className="text-xs font-medium">已选（{selectedItems.length}/{PICKER_MAX_SELECTED}）</span>
            <button type="button" className="ml-auto text-xs text-muted-foreground disabled:opacity-50"
              disabled={selectedItems.length === 0}
              onClick={() => setSelectedIds([])}>清空所有</button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {selectedItems.map((m) => (
              <button key={m.id} type="button"
                className="flex w-full items-center gap-1.5 border-b border-border px-2 py-1.5 text-left text-xs last:border-0 hover:bg-muted/60"
                onClick={() => removeMaterial(m.id)} title={`${m.title}（点击移出）`}>
                <span className="min-w-0 flex-1 truncate">{m.title}</span>
                <span className="shrink-0 text-muted-foreground">✕</span>
              </button>
            ))}
            {selectedItems.length === 0 && (
              <div className="py-10 text-center text-xs text-muted-foreground">点击左侧素材加入</div>
            )}
          </div>
        </div>
      </div>
    </Dialog>
  )
}

/** 批量添加片段弹窗（含切割配置，#56 自动分配 / #85 不切割时手动选目标分镜 / #88 预判单分镜片段上限） */
function AddClipsDialog({ open, onOpenChange, projectId, shots, projectWidth, projectHeight, onDone }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  projectId: string
  shots: Shot[]
  /** #440：项目画幅（用于派生 orientation 过滤待选视频） */
  projectWidth?: number
  projectHeight?: number
  /** #409：返回 Promise，dialog 内 await 后再关弹窗，确保新 clip 进入 ProjectEditor state 后才显示 chip */
  onDone: () => Promise<void> | void
}) {
  const [materialIds, setMaterialIds] = useState<string[]>([])
  /** 已选素材详情（#88 用于按切割参数预判单分镜片段上限） */
  const [selectedMaterials, setSelectedMaterials] = useState<Material[]>([])
  const [cutMode, setCutMode] = useState<'whole' | 'fixed' | 'trim_after'>('fixed')
  // #390：切割秒数与最短片段默认 2 秒
  const [fixedSeconds, setFixedSeconds] = useState(2)
  const [trimHead, setTrimHead] = useState(0)
  const [trimTail, setTrimTail] = useState(0)
  const [minClip, setMinClip] = useState(2)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  // #85/#86：不切割时的目标分镜（单选，所选素材全部添加到该分镜）
  const [targetShotId, setTargetShotId] = useState('')

  // 打开弹窗或分镜变化时默认选中第 1 个分镜，避免残留过期分镜 ID
  useEffect(() => {
    if (open) {
      setTargetShotId(shots[0]?.id ?? '')
      // #97：每次打开都清空已选视频，避免上次取消/关闭的素材残留
      setMaterialIds([])
      setSelectedMaterials([])
    }
  }, [open, shots])

  // 仅在「不切割」且已选定目标分镜时才显式指定，否则走后端自动分配
  const explicitTarget = cutMode === 'whole' && Boolean(targetShotId)
  const canSubmit = Boolean(materialIds.length && projectId) &&
    !(cutMode === 'whole' && shots.length > 0 && !targetShotId)

  /**
   * 预估单个素材按当前切割参数切出的片段数（#88）。
   * 注：这是粗略上界估算，低于 min_clip_seconds 的段会被后端丢弃，预判时不扣减。
   */
  const segsOf = (m: Material): number => {
    if (cutMode === 'whole') return 1
    const dur = m.duration_ms ?? 0
    if (dur <= 0) return 1
    const avail = cutMode === 'trim_after' ? Math.max(0, dur - trimHead * 1000 - trimTail * 1000) : dur
    const step = Math.max(1, fixedSeconds * 1000)
    return Math.max(1, Math.ceil(avail / step))
  }
  /** 本次新增片段总数（所有素材段数之和） */
  const totalAdding = selectedMaterials.reduce((s, m) => s + segsOf(m), 0)

  /**
   * 预判单分镜片段上限（#88）。
   * 仅在「不切割 + 显式目标分镜」场景下做精确校验（其他场景由后端逐段校验兜底）。
   * 切割模式的自动分配分镜数 / 段数映射前端无法精确复现，留给后端。
   */
  const checkSegmentLimit = (): string | null => {
    if (!(cutMode === 'whole' && targetShotId)) return null
    const target = shots.find((s) => s.id === targetShotId)
    if (!target) return null
    const after = target.clip_count + materialIds.length   // 不切割：每素材 1 段
    if (after > MAX_CLIPS_PER_SHOT) {
      return `目标分镜「${target.name}」已有 ${target.clip_count} 段，本次将新增 ${materialIds.length} 段（共 ${after} 段），超过单分镜片段上限 ${MAX_CLIPS_PER_SHOT}。请减少所选素材或更换目标分镜。`
    }
    return null
  }

  const submit = async () => {
    if (!canSubmit) return
    const limitErr = checkSegmentLimit()
    if (limitErr) { toast(limitErr, 'error'); return }
    setBusy(true)
    try {
      const r = await creationApi.addClips({
        project_id: projectId,
        material_ids: materialIds,
        cut_mode: cutMode,
        fixed_seconds: fixedSeconds,
        trim_head_s: trimHead,
        trim_tail_s: trimTail,
        min_clip_seconds: minClip,
        target_shot_ids: explicitTarget ? [targetShotId] : undefined,
      })
      toast(
        `已添加 ${r.clip_count} 个片段（跳过 ${r.skipped}），后台切割任务 ${r.bg_task_id?.slice(0, 8) ?? '-'} 进行中`,
        'success',
      )
      setMaterialIds([])
      setSelectedMaterials([])
      // #409：先 await onDone（load 完成 → setDetail 触发 chip 重渲染），再关弹窗
      await onDone()
      onOpenChange(false)
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange} title="批量添加视频片段" width={640}
        footer={
          <>
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
            <Button size="sm" disabled={busy || !canSubmit} onClick={submit}>
              {busy ? '处理中…' : `添加（${materialIds.length} 个视频）`}
            </Button>
          </>
        }>
        <div className="space-y-3.5">
          <div className="flex items-center gap-2">
            {/* #92：素材文案统一改为视频，与弹窗标题"批量添加视频片段"一致 */}
            <span className="text-sm font-medium">视频：</span>
            <Button variant="outline" size="sm" onClick={() => setPickerOpen(true)}>
              {materialIds.length ? `已选 ${materialIds.length} 个视频` : '选择视频'}
            </Button>
            {/* #96：清空所选视频（N=0 时隐藏） */}
            {materialIds.length > 0 && (
              <Button variant="ghost" size="sm" className="!text-danger"
                onClick={() => { setMaterialIds([]); setSelectedMaterials([]) }}>清空</Button>
            )}
          </div>
          <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-700">
            {cutMode === 'whole'
              ? '分配规则：所选素材（整条不切割）将全部添加到下方选定的目标分镜；项目无分镜时按第 1 个视频自动建分镜。'
              : '分配规则：分镜为空时按第 1 个视频的分割段数自动建分镜；非空时按视频段数从大到小均衡分配到所有分镜（一段一分镜），超出分镜数的片段丢弃。'}
            {/* #88：预估本次新增片段数，帮助用户提前判断是否撞单分镜上限 */}
            {materialIds.length > 0 && (
              <div className="mt-1 text-blue-600">
                本次预计新增 <span className="font-semibold">{totalAdding}</span> 个片段
                {cutMode === 'whole' && explicitTarget && (() => {
                  const t = shots.find((s) => s.id === targetShotId)
                  return t ? `（目标分镜现有 ${t.clip_count}，处理后 ${t.clip_count + totalAdding}/${MAX_CLIPS_PER_SHOT}）` : ''
                })()}
              </div>
            )}
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">切割方式</label>
            <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm" value={cutMode}
              onChange={(e) => setCutMode(e.target.value as typeof cutMode)}>
              <option value="whole">不切割，整条使用</option>
              <option value="fixed">按固定时长切割</option>
              <option value="trim_after">去头尾后按固定时长切割</option>
            </select>
          </div>
          {/* #86/#94：不切割时选择目标分镜（单选，所选素材整条全部进入该分镜）；用下拉组件替代列表 */}
          {cutMode === 'whole' && (
            <div>
              <label className="mb-1 block text-sm font-medium">目标分镜（单选）</label>
              {shots.length === 0 ? (
                <div className="rounded-md border border-border bg-muted px-3 py-2 text-xs text-muted-foreground">
                  当前项目还没有分镜，将按第 1 个视频自动创建分镜并分配。
                </div>
              ) : (
                <>
                  <select
                    className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                    value={targetShotId}
                    onChange={(e) => setTargetShotId(e.target.value)}>
                    {shots.map((s, i) => (
                      <option key={s.id} value={s.id}>#{i + 1} {s.name}（{s.clip_count} 片段）</option>
                    ))}
                  </select>
                  <div className="mt-1 text-xs text-muted-foreground">
                    所选素材（整条不切割）将全部添加到该分镜。
                  </div>
                </>
              )}
            </div>
          )}
          {(cutMode === 'fixed' || cutMode === 'trim_after') && (
            <div className="grid grid-cols-4 gap-3">
              <div>
                <label className="mb-1 block text-xs font-medium">切割秒数</label>
                <input type="number" min={1} className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                  value={fixedSeconds} onChange={(e) => setFixedSeconds(Number(e.target.value))} />
              </div>
              {cutMode === 'trim_after' && (
                <>
                  <div>
                    <label className="mb-1 block text-xs font-medium">去头(秒)</label>
                    <input type="number" min={0} className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                      value={trimHead} onChange={(e) => setTrimHead(Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium">去尾(秒)</label>
                    <input type="number" min={0} className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                      value={trimTail} onChange={(e) => setTrimTail(Number(e.target.value))} />
                  </div>
                </>
              )}
              <div>
                <label className="mb-1 block text-xs font-medium">最短片段(秒)</label>
                <input type="number" min={1} className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                  value={minClip} onChange={(e) => setMinClip(Number(e.target.value))} />
              </div>
            </div>
          )}
        </div>
      </Dialog>
      <MaterialPicker open={pickerOpen} onOpenChange={setPickerOpen} type="video" multi
        initialSelected={selectedMaterials}
        // #389：聚合项目下所有分镜的素材 ID，picker 待选行已存在的标灰勾
        existingIds={(shots ?? []).flatMap((s) => s.clips.map((c) => c.material_id))}
        // #440：按项目画幅过滤待选视频（方形 / 1:1 传 null 不过滤）
        projectOrientation={(() => {
          const w = projectWidth ?? 0
          const h = projectHeight ?? 0
          if (w <= 0 || h <= 0 || w === h) return null
          return h > w ? 'vertical' : 'horizontal'
        })()}
        onConfirm={(ids, mats) => { setMaterialIds(ids); setSelectedMaterials(mats) }} />
    </>
  )
}

/** 片段首帧缩略图（data 相对路径 → 可访问 URL，异步解析）
 *
 * rev 为后端渲染版本号：镜像/重渲染后缩略图路径不变，靠 ?v=rev 破浏览器缓存，
 * 否则会一直显示镜像前的旧图（#59）。
 */
function ThumbImg({ rel, rev, alt }: { rel: string; rev?: number; alt?: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let alive = true
    fileUrl(rel).then((u) => { if (alive) setUrl(rev ? `${u}?v=${rev}` : u) })
    return () => { alive = false }
  }, [rel, rev])
  if (!url) return null
  return <img src={url} alt={alt} className="absolute inset-0 h-full w-full object-cover" draggable={false} />
}

/** 片段播放弹窗（#57：点击片段播放该片段视频） */
function ClipPlayDialog({ clip, open, onOpenChange }: {
  clip: Clip | null
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [src, setSrc] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    if (open && clip?.file_status === 'ready') {
      fileUrl(clip.file_path!).then((u) => { if (alive) setSrc(u) })
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [open, clip?.file_status])

  const status = clip?.file_status
  const loading = status === 'pending'

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={clip?.material_title ?? '片段预览'} width={520}
      footer={status === 'failed' ? (
        <Button size="sm" disabled={busy} onClick={async () => {
          if (!clip) return
          setBusy(true)
          try {
            await creationApi.renderClip(clip.id)
            toast('已重新渲染，稍后刷新', 'success')
            onOpenChange(false)
          } catch (e) { toast((e as Error).message, 'error') } finally { setBusy(false) }
        }}>重新渲染</Button>
      ) : undefined}>
      {status === 'failed' ? (
        <div className="py-10 text-center text-sm text-danger">
          片段渲染失败：{clip?.fail_reason || '未知原因'}
        </div>
      ) : loading ? (
        <div className="flex aspect-9/16 max-h-[70vh] items-center justify-center rounded-md bg-muted text-sm text-muted-foreground">
          片段切割中…
        </div>
      ) : src ? (
        <video src={src} className="mx-auto max-h-[70vh] w-full rounded-md bg-black" controls autoPlay
          onError={() => toast('视频加载失败（文件缺失或格式不支持）', 'error')} />
      ) : (
        <div className="flex aspect-9/16 max-h-[70vh] items-center justify-center rounded-md bg-muted text-sm text-muted-foreground">加载中…</div>
      )}
      {clip && (
        <div className="mt-2 text-center text-xs text-muted-foreground">
          {fmtMs(clip.clip_start_ms)} ~ {fmtMs(clip.clip_end_ms)}（{(Math.max(0, clip.clip_end_ms - clip.clip_start_ms) / 1000).toFixed(1)}s）
          {clip.mirrored ? ' · 已镜像' : ''}
        </div>
      )}
    </Dialog>
  )
}

/** 毫秒 → 秒文本（片段展示共用） */
function fmtMs(ms: number) { return `${(ms / 1000).toFixed(1)}s` }

/** #399：原视频预览图横向固定列表。
 * #403：批量删除走 manage 模式（与分镜卡片片段一致）—— 顶栏"管理"按钮进入管理模式，
 *      chip 出现勾选框 + 底部工具条"删除"。
 * 数据源：父级聚合 (material_id -> { clipIds, clipCount, first clip, sourceIndex })
 */
function MaterialChipsBar({
  agg, highlightMaterials, selectedMaterials, manageMode, deleting,
  onToggleHighlight, onToggleSelect, onAskDelete, onClearSelection, onEnterManage, onExitManage,
}: {
  agg: Map<string, { clipIds: Set<string>; clipCount: number; first: Clip | null; sourceIndex: number | null; spanMs: number; durationMs: number | null }>
  highlightMaterials: Set<string>
  selectedMaterials: Set<string>
  manageMode: boolean
  deleting: boolean
  onToggleHighlight: (materialId: string, additive: boolean) => void
  onToggleSelect: (materialId: string) => void
  onAskDelete: () => void
  onClearSelection: () => void
  onEnterManage: () => void
  onExitManage: () => void
}) {
  const items = useMemo(() => {
    const out: { mid: string; clipCount: number; first: Clip | null; sourceIndex: number | null; durationMs: number | null; spanMs: number }[] = []
    for (const [mid, info] of agg) out.push({ mid, clipCount: info.clipCount, first: info.first, sourceIndex: info.sourceIndex, durationMs: info.durationMs, spanMs: info.spanMs })
    // #新需求：按 sourceIndex（#402 序号）升序，缺失排最后；保证 chip 显示顺序与片段左上角序号一致
    out.sort((a, b) => {
      const sa = a.sourceIndex, sb = b.sourceIndex
      if (sa == null && sb == null) return 0
      if (sa == null) return 1
      if (sb == null) return -1
      return sa - sb
    })
    return out
  }, [agg])

  // #407：chip 横滚容器 ref（保留用于 ProjectEditor 顶层统一调度 wheel）
  const chipsBarRef = useRef<HTMLDivElement | null>(null)

  if (items.length === 0) {
    return (
      <div className="mb-3 shrink-0 rounded-md border border-dashed border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        暂无原视频，去素材库添加视频批量切割后会在这里展示
      </div>
    )
  }
  // #403：管理模式下全选/已选计数（与分镜管理模式一致）
  const allSelected = manageMode && selectedMaterials.size === items.length

  return (
    <div className="mb-3 shrink-0 rounded-md border border-border bg-white p-2">
      <div className="mb-1.5 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="text-sm font-semibold text-foreground">原视频（{items.length}）</span>
        <span>· 点选高亮对应片段</span>
        {/* #403：与管理模式一致 — 顶栏"管理/退出管理"按钮 */}
        <Button variant={manageMode ? 'primary' : 'outline'} size="sm" className="ml-auto"
          onClick={manageMode ? onExitManage : onEnterManage}>
          {manageMode ? '退出管理' : '管理'}
        </Button>
      </div>
      {/* #408：管理模式工具条上移至 chip 列表上方（与分镜卡管理模式工具条位置一致 — 在片段条之上） */}
      {manageMode && (
        <div className="mb-1.5 flex items-center gap-2 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-xs">
          <span className="text-blue-700">已选 {selectedMaterials.size} 个</span>
          <label className="ml-1 flex items-center gap-1">
            <input type="checkbox" checked={allSelected}
              onChange={() => onClearSelection()} />
            全选
          </label>
          {/* #408：与分镜卡管理模式工具条结构一致：批量操作按钮 + 右侧退出管理 */}
          <Button variant="outline" size="sm" className="!text-danger"
            disabled={deleting || !selectedMaterials.size} onClick={onAskDelete}>
            {deleting ? '删除中…' : '删除'}
          </Button>
          <Button variant="ghost" size="sm" onClick={onExitManage}>退出管理</Button>
        </div>
      )}
      {/* #407：chip 横滚容器 gap-1（≈0.25rem，紧凑），px-1 py-1 保留让出 ring 空间
            wheel 由 ProjectEditor 顶层统一接管（passive:false + elementFromPoint 判定 hover 下的 track） */}
      <div ref={chipsBarRef} data-chips-bar className="flex gap-1 overflow-x-auto px-1 py-1">
        {items.map(({ mid, clipCount, first, sourceIndex, durationMs, spanMs }) => (
          <MaterialChip
            key={mid}
            materialId={mid}
            clipCount={clipCount}
            sourceIndex={sourceIndex}
            // #404：优先用 JOIN 时长，缺则用切割总跨度（兜底）
            durationMs={durationMs ?? (spanMs > 0 ? spanMs : null)}
            coverRel={first?.material_cover_url ?? null}
            title={first?.material_title ?? ''}
            missing={first?.material_status !== 'normal'}
            highlighted={highlightMaterials.has(mid)}
            selected={selectedMaterials.has(mid)}
            manageMode={manageMode}
            onClick={(e) => {
              // #403：管理模式下点击 chip = 勾选/取消；否则点选高亮
              if (manageMode) { e.stopPropagation(); onToggleSelect(mid); return }
              onToggleHighlight(mid, e.shiftKey || e.ctrlKey || e.metaKey)
            }}
          />
        ))}
      </div>
    </div>
  )
}

/** 单张原视频预览卡（#399/#402/#403/#404）：
 * - #402 左上角 source_index 角标（与片段一致）
 * - #403 管理模式下显示勾选框；点击 chip = 勾选
 * - #404 右下角显示时长（来自 material.duration_ms），不显示标题（hover title）
 */
function MaterialChip({
  materialId, clipCount, sourceIndex, durationMs, coverRel, title, missing,
  highlighted, selected, manageMode, onClick,
}: {
  materialId: string
  clipCount: number
  sourceIndex: number | null
  durationMs: number | null
  coverRel: string | null
  title: string
  missing: boolean
  highlighted: boolean
  selected: boolean
  manageMode: boolean
  onClick: (e: React.MouseEvent) => void
}) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let alive = true
    if (coverRel) fileUrl(coverRel).then((u) => { if (alive) setUrl(u) })
    else setUrl('')
    return () => { alive = false }
  }, [coverRel])

  return (
    <button type="button"
      onClick={onClick}
      data-testid="material-chip"
      data-material-id={materialId}
      title={`${title}（${clipCount} 片段${durationMs ? ` · ${(durationMs / 1000).toFixed(1)}s` : ''}）`}
      className={cn(
        'group relative h-20 w-14 shrink-0 overflow-hidden rounded-md border bg-muted text-left',
        // #统一：高亮用 amber 与 clip 高亮同色，体现"父子映射"语义
        highlighted ? 'border-border ring-2 ring-red-400 bg-red-50' : 'border-border hover:bg-muted/60',
        // manageMode 选中用蓝边，与分镜片段管理模式一致
        selected && manageMode && 'border-blue-500 ring-2 ring-blue-400',
      )}
    >
      {url ? (
        <img src={url} alt={title} className="absolute inset-0 h-full w-full object-cover" draggable={false} />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center text-[10px] text-muted-foreground">
          {missing ? '缺失' : '无封面'}
        </div>
      )}
      {/* 缺失遮罩 */}
      {missing && <div className="absolute inset-0 bg-red-900/40" />}
      {/* #402：左上角序号（与片段 source_index 一致） */}
      {sourceIndex != null && (
        <div data-testid="material-source-index"
          className="absolute left-0 top-0 rounded-br bg-black/60 px-1 text-[12px] leading-tight text-white"
          title={`来源视频 #${sourceIndex}`}>#{sourceIndex}</div>
      )}
      {/* 片段数角标（右上） */}
      <div className="absolute right-0 top-0 rounded-bl bg-black/60 px-1 text-[10px] leading-tight text-white">
        {clipCount}
      </div>
      {/* #404：右下角时长（不显示标题；z-10 防被遮罩/序号/管理 checkbox 盖住；缺数据时显示 ?） */}
      <div className="absolute bottom-0 right-0 z-10 rounded-tl bg-black/60 px-1 text-[10px] leading-tight text-white">
        {durationMs != null && durationMs > 0 ? `${(durationMs / 1000).toFixed(1)}s` : '?'}
      </div>
      {/* #403：管理模式左上角勾选框（盖在 source_index 角标之上，优先时用） */}
      {manageMode && (
        <input type="checkbox"
          className="absolute left-0.5 top-0.5 z-10 h-3.5 w-3.5 cursor-pointer accent-blue-600"
          checked={selected}
          onChange={() => {}}
          onClick={(e) => { e.stopPropagation(); onClick(e) }}
        />
      )}
    </button>
  )
}

/** #380：顶部失败计数条 + 一键重试。
 * 遍历所有分镜的所有片段，统计 file_status='failed' 的 ID，一键 retry_render。
 */
function FailedBanner({ detail, onChanged }: { detail: Project | null; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const failedIds = (detail?.shots ?? []).flatMap((s) =>
    s.clips.filter((c) => c.file_status === 'failed').map((c) => c.id))
  if (failedIds.length === 0) return null
  const handleRetryAll = async () => {
    setBusy(true)
    try {
      const r = await creationApi.batchClips({
        clip_ids: failedIds, action: 'retry_render',
      })
      if (r.failed.length) toast(`部分失败：${r.failed.length} 个`, 'error')
      else toast(`已重新渲染 ${failedIds.length} 个片段`, 'success')
      onChanged()
    } catch (e) { toast((e as Error).message, 'error') }
    finally { setBusy(false) }
  }
  return (
    <div className="mb-3 flex shrink-0 items-center gap-2 rounded-md border border-danger/40 bg-red-50 px-3 py-2 text-xs text-danger">
      <span>有 <span className="font-semibold">{failedIds.length}</span> 个片段渲染失败</span>
      <Button size="sm" variant="outline" className="!text-danger" disabled={busy} onClick={handleRetryAll}>
        一键重新渲染
      </Button>
    </div>
  )
}

/** 分镜卡片（#57：标题置底、点击播放、分镜级管理批量操作；#69 序号；#399 高亮联动） */
function ShotCard({ shot, shots, index, onChanged, highlightClipIds }: {
  shot: Shot; shots: Shot[]; index: number; onChanged: () => void
  /** #399：受高亮的 clip id 集合（来自原视频预览图点选），命中卡片显示 amber 边框 */
  highlightClipIds?: Set<string>
}) {
  // #407：pointer 是否在该分镜卡内片段条上方 — 用 ref 而非 state（wheel 监听器读到最新值，无异步延迟）
  const trackHoverRef = useRef(false)
  // #407：ProjectEditor 调度高亮滚动用 ref（DOM 查 [data-shot-id] 已可定位，但保留 ref 便于本地滚动逻辑共用）
  const clipsTrackRef = useRef<HTMLDivElement | null>(null)
  const [manageMode, setManageMode] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [dragId, setDragId] = useState<string | null>(null)
  const [dropId, setDropId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [playClip, setPlayClip] = useState<Clip | null>(null)
  const [moveOpen, setMoveOpen] = useState(false)
  const [moveTarget, setMoveTarget] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const dragRef = useRef(false)
  const [prompt, promptDialog] = usePrompt()

  // #407：片段条横滚由 ProjectEditor 顶层统一接管（passive:false + elementFromPoint 判定 hover 下的 track），
  //      本地不再挂 listener，避免多 listener 互相干扰

  const durMs = (c: Clip) => Math.max(0, c.clip_end_ms - c.clip_start_ms)

  const toggleSel = (id: string) => {
    setSelected((prev) => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n })
  }
  const allChecked = shot.clips.length > 0 && selected.size === shot.clips.length
  const exitManage = () => { setManageMode(false); setSelected(new Set()) }

  /** 拖拽落点：把 dragId 移到 dropId 位置并重排（仅分镜内） */
  const doDrop = async (targetId: string) => {
    if (!dragId || dragId === targetId) { setDragId(null); setDropId(null); return }
    const ids = shot.clips.map((c) => c.id)
    const from = ids.indexOf(dragId)
    const to = ids.indexOf(targetId)
    if (from < 0 || to < 0) { setDragId(null); setDropId(null); return }
    ids.splice(from, 1)
    ids.splice(to, 0, dragId)
    setDragId(null); setDropId(null)
    try {
      await creationApi.reorderClips(shot.id, ids)
      onChanged()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  /** 批量操作统一入口（delete / mirror / move / retry_render） */
  const doBatch = async (action: 'delete' | 'mirror' | 'move' | 'retry_render', targetShotId?: string) => {
    if (!selected.size) return
    // #388：retry_render 入参只接受失败片段；空集直接提示用户,不发无意义请求
    let ids = [...selected]
    if (action === 'retry_render') {
      const failedSet = new Set(shot.clips.filter((c) => c.file_status === 'failed').map((c) => c.id))
      ids = ids.filter((id) => failedSet.has(id))
      if (ids.length === 0) { toast('所选片段中没有渲染失败的，无需重试', 'info'); return }
    }
    setBusy(true)
    try {
      const r = await creationApi.batchClips({
        clip_ids: ids, action, target_shot_id: targetShotId,
      })
      if (r.failed.length) toast(`部分失败：${r.failed.length} 个`, 'error')
      else toast(
        action === 'delete' ? '已删除'
        : action === 'mirror' ? '已镜像'
        : action === 'move' ? '已移动'
        : '已重新渲染',
        'success',
      )
      setSelected(new Set())
      setMoveOpen(false)
      setMoveTarget('')
      onChanged()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-testid="shot-card" className="rounded-lg border border-border bg-white p-3">
      <div className="mb-2 flex items-center gap-2">
        {/* #69：分镜序号，在名称前，从 1 开始 */}
        <span data-testid="shot-index" className="shrink-0 text-sm font-semibold text-accent">#{index + 1}</span>
        <span className="text-sm font-semibold">{shot.name}</span>
        <span className="text-xs text-muted-foreground">{shot.clip_count} 片段</span>
        <div className="ml-auto flex gap-1">
          <Button variant="outline" size="sm" disabled={shot.sort_order === 0}
            onClick={() => creationApi.moveShot(shot.id, 'up').then(onChanged)}>上移</Button>
          <Button variant="outline" size="sm" disabled={shot.sort_order === shots.length - 1}
            onClick={() => creationApi.moveShot(shot.id, 'down').then(onChanged)}>下移</Button>
          <Button variant="outline" size="sm" onClick={async () => {
            const v = await prompt('分镜名', shot.name)
            if (v && v !== shot.name) creationApi.renameShot(shot.id, v).then(onChanged)
          }}>重命名</Button>
          <Button variant="outline" size="sm" className="!text-danger"
            onClick={() => { if (window.confirm(`删除分镜「${shot.name}」及其全部片段？`)) creationApi.deleteShot(shot.id).then(onChanged) }}>
            删除
          </Button>
          <Button variant={manageMode ? 'primary' : 'outline'} size="sm"
            onClick={() => (manageMode ? exitManage() : setManageMode(true))}>
            {manageMode ? '退出管理' : '管理'}
          </Button>
        </div>
      </div>

      {/* 管理工具条 */}
      {manageMode && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-xs">
          <span className="text-blue-700">已选 {selected.size} 个</span>
          <label className="ml-1 flex items-center gap-1">
            <input type="checkbox" checked={allChecked}
              onChange={() => setSelected(allChecked ? new Set() : new Set(shot.clips.map((c) => c.id)))} />
            全选
          </label>
          <Button variant="outline" size="sm" disabled={busy || !selected.size} onClick={() => doBatch('mirror')}>镜像</Button>
          {/* #380：失败片段批量重渲染（已有 retry_render action） */}
          <Button variant="outline" size="sm" disabled={busy || !selected.size} onClick={() => doBatch('retry_render')}>重新渲染</Button>
          <Button variant="outline" size="sm" disabled={busy || !selected.size} onClick={() => setMoveOpen(true)}>移动到其他分镜</Button>
          <Button variant="outline" size="sm" className="!text-danger" disabled={busy || !selected.size}
            onClick={() => setDeleteOpen(true)}>删除</Button>
          <Button variant="ghost" size="sm" onClick={exitManage}>退出管理</Button>
        </div>
      )}

      {/* #407：片段条横向滚动容器 gap-1（≈0.25rem，紧凑），px-1 py-1 保留让出 ring 空间
            data-shot-id 用于 ProjectEditor 集中调度所有分镜片段条滚动
            wheel 通过 useEffect + native listener(passive:false) 接管，仅 hover 状态生效（用 ref 同步，避免 state 异步） */}
      <div data-shot-id={shot.id} ref={clipsTrackRef}
        onPointerEnter={() => { trackHoverRef.current = true }}
        onPointerLeave={() => { trackHoverRef.current = false }}
        className="flex gap-1 overflow-x-auto px-1 py-1">
        {shot.clips.map((c) => {
          const isSel = selected.has(c.id)
          const missing = c.material_status !== 'normal'
          const failed = c.file_status === 'failed'
          // #387：ready/未切割（已绑定素材但待切）= 可播放；切割中/失败/素材缺失 = 不可播放
          const cutting = !c.thumb_path && c.file_status === 'pending'
          const playable = !manageMode && !missing && !failed && !cutting
          // #399：原视频预览图点选后命中此 clip → amber 描边（不覆盖选中/拖拽高亮优先级）
          const highlighted = highlightClipIds?.has(c.id) ?? false
          return (
            <div
              key={c.id}
              data-testid="clip-card"
              data-mirrored={c.mirrored}
              data-highlighted-clip={highlighted ? 'true' : undefined}
              draggable={!manageMode}
              onDragStart={() => { dragRef.current = true; setDragId(c.id) }}
              onDragEnd={() => { setDragId(null); setDropId(null); setTimeout(() => { dragRef.current = false }, 0) }}
              onDragOver={(e) => { if (!manageMode) { e.preventDefault(); setDropId(c.id) } }}
              onDrop={() => !manageMode && doDrop(c.id)}
              onClick={() => {
                if (dragRef.current) return
                if (manageMode) toggleSel(c.id)
                // #387：切割中/失败/素材缺失时点击不打开播放弹窗
                else if (playable) setPlayClip(c)
              }}
              className={cn(
                'group relative h-44 w-24 shrink-0 overflow-hidden rounded-md border',
                playable && 'cursor-pointer',
                isSel ? 'border-blue-500 ring-2 ring-blue-400' : ((missing || failed) ? 'border-danger' : 'border-border'),
                dropId === c.id && dragId && dragId !== c.id && 'ring-2 ring-amber-400',
                dragId === c.id && 'opacity-50',
                // #400：高亮联动 — 用 ring + z-10 让 ring 浮在相邻卡片之上不被遮挡;
                // 父容器 padding(px-1) 留出 ring 空间,避免被 overflow 边界裁
                highlighted && !isSel && !(dropId === c.id && dragId && dragId !== c.id) && 'z-10 ring-2 ring-red-400',
              )}
            >
              {/* 首帧背景 */}
              {c.thumb_path
                ? <ThumbImg rel={c.thumb_path} rev={c.rev} alt={c.material_title ?? ''} />
                : <div className="absolute inset-0 flex items-center justify-center bg-muted text-[10px] text-muted-foreground">
                    {/* #380：渲染失败时 hover 显示具体原因（fail_reason）+ 单片段重试入口 */}
                    {failed ? (
                      <Tooltip content={c.fail_reason || '未知原因，点击卡片右侧 ↻ 重新渲染'}>
                        <span className="cursor-help text-danger">渲染失败</span>
                      </Tooltip>
                    ) : (
                      // #387：切割中提示 + 进度圈（ThumbImg 未到位前用转圈代替静态"处理中…"）
                      cutting && (
                        <div className="flex flex-col items-center gap-1 text-muted-foreground">
                          <svg className="h-4 w-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                            <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                            <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                          </svg>
                          <span>切割中…</span>
                        </div>
                      )
                    )}
                  </div>}
              {/* #380 + #387：失败卡片右上角悬浮重试按钮（不用开播放弹窗） */}
              {failed && !manageMode && (
                <button
                  type="button"
                  aria-label="重新渲染"
                  className="absolute right-1 top-1 z-10 flex h-6 w-6 items-center justify-center rounded-full bg-white/90 text-danger shadow ring-1 ring-danger/30 transition-opacity hover:bg-white group-hover:opacity-100"
                  onClick={async (e) => {
                    e.stopPropagation()
                    try {
                      await creationApi.renderClip(c.id)
                      toast('已重新渲染，稍后刷新', 'success')
                      onChanged()
                    } catch (err) { toast((err as Error).message, 'error') }
                  }}
                >↻</button>
              )}
              {missing && <div className="absolute inset-0 bg-red-900/40" />}
              {/* 来源视频序号（#60）：添加视频时该视频在项目内的累计序号 */}
              {c.source_index != null && (
                <div data-testid="clip-source-index"
                  className="absolute left-0 top-0 rounded-br bg-black/60 px-1 text-[12px] leading-tight text-white"
                  title={`来源视频 #${c.source_index}`}>#{c.source_index}</div>
              )}
              {c.mirrored === 1 && <div className="absolute right-0 top-0 rounded-bl bg-amber-500 px-1 text-[10px] text-white">镜像</div>}

              {/* 管理模式的左上多选框 */}
              {manageMode && (
                <input type="checkbox" className="absolute left-1 top-1 h-3.5 w-3.5 cursor-pointer"
                  checked={isSel} onChange={() => toggleSel(c.id)} onClick={(e) => e.stopPropagation()} />
              )}

              {/* #387：仅 playable（ready 状态）悬停显示播放预览；切割中/失败/素材缺失 不显示 */}
              {playable && (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-0 transition-opacity group-hover:opacity-100">
                  <span className="rounded-full bg-black/50 px-2 py-1 text-lg text-white">▶</span>
                </div>
              )}

              {/* 底部信息块：标题（1~2 行省略），下一行左侧时间范围、右侧时长 */}
              <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/75 to-transparent px-1 pb-1 pt-4">
                <div className="line-clamp-2 text-[12px] leading-tight text-white" title={c.material_title ?? ''}>
                  {c.material_title ?? '(素材缺失)'}
                </div>
                <div className="flex items-center justify-between gap-1 text-[12px] leading-tight">
                  <span className="truncate text-white/70">{fmtMs(c.clip_start_ms)} ~ {fmtMs(c.clip_end_ms)}</span>
                  <span className="shrink-0 text-white/90">{fmtMs(durMs(c))}</span>
                </div>
              </div>
            </div>
          )
        })}
        {shot.clips.length === 0 && (
          <div className="w-full py-3 text-center text-xs text-muted-foreground">暂无片段，用上方「添加视频」批量切割</div>
        )}
      </div>

      <ClipPlayDialog clip={playClip} open={!!playClip} onOpenChange={(v) => !v && setPlayClip(null)} />

      {/* 移动到其他分镜 */}
      <Dialog open={moveOpen} onOpenChange={setMoveOpen} title="移动片段到分镜" width={360}
        footer={
          <>
            <Button variant="outline" size="sm" onClick={() => setMoveOpen(false)}>取消</Button>
            <Button size="sm" disabled={!moveTarget || busy} onClick={() => doBatch('move', moveTarget)}>移动</Button>
          </>
        }>
        <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm" value={moveTarget}
          onChange={(e) => setMoveTarget(e.target.value)}>
          <option value="">选择目标分镜…</option>
          {shots.filter((s) => s.id !== shot.id).map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <div className="mt-2 text-xs text-muted-foreground">已选 {selected.size} 个片段将移动到目标分镜末尾。</div>
      </Dialog>

      {/* 批量删除确认 */}
      <ConfirmDialog open={deleteOpen} onOpenChange={setDeleteOpen} title="批量删除片段" danger
        content={`确定删除选中的 ${selected.size} 个片段？对应的片段文件将一并删除。`}
        onConfirm={() => doBatch('delete')} />

      {promptDialog}
    </div>
  )
}

/** 项目编辑视图（右侧抽屉式整页） */
function ProjectEditor({ projectId, onClose, resolutionPresets }: {
  projectId: string
  onClose: () => void
  /** #426：画幅预设（横竖屏/分辨率）—— CreationPage 传入，避免子组件重复拉取 */
  resolutionPresets: Array<{ key: string; w: number; h: number; label: string; platform?: string }>
}) {
  const [detail, setDetail] = useState<Project | null>(null)
  const [bgms, setBgms] = useState<BgmItem[]>([])
  const [dedup, setDedup] = useState<{ frame_drop: boolean; watermark: boolean; noise: boolean; strength: 'low' | 'mid' | 'high'; watermark_text_mode?: 'timestamp' | 'title' | 'custom'; watermark_text_custom?: string }>(
    { frame_drop: false, watermark: false, noise: false, strength: 'low', watermark_text_mode: 'timestamp', watermark_text_custom: '' }
  )
  const [bgmStrategy, setBgmStrategy] = useState<'order' | 'random'>('random')
  const [addClipsOpen, setAddClipsOpen] = useState(false)
  const [bgmPickerOpen, setBgmPickerOpen] = useState(false)
  const [insertOpen, setInsertOpen] = useState(false)
  const [insertBefore, setInsertBefore] = useState('')   // 目标分镜 ID（插入到其之前）
  // 项目级 BGM 音量拖动期间本地草稿，松手/失焦才落库
  const [bgmVolumeDraft, setBgmVolumeDraft] = useState<number | null>(null)
  // #406：右栏折叠状态（默认展开；分镜多时收起给分镜更多空间）
  const [rightCollapsed, setRightCollapsed] = useState(false)

  // #407：ProjectEditor 根 ref（包含 MaterialChipsBar + 分镜区，统一 wheel 拦截范围）
  const editorRootRef = useRef<HTMLDivElement | null>(null)

  // #399：高亮的素材 ID 集合（来自原视频预览图点选）；派生 highlightClipIds 透传到 ShotCard
  const [highlightMaterials, setHighlightMaterials] = useState<Set<string>>(new Set())
  /** 原视频预览图聚合：从所有分镜的 clips 按 material_id 去重得到 material_id → clip ids 的映射 */
  const materialAgg = useMemo(() => {
    // #404 兜底：素材时长 ms。
    // 优先级：material_duration_ms（后端 JOIN） > spanMs（按 clip 端点计算的总跨度）> null
    const m = new Map<string, { clipIds: Set<string>; clipCount: number; first: Clip | null; sourceIndex: number | null; spanMs: number; durationMs: number | null }>()
    if (!detail?.shots) return m
    for (const s of detail.shots) {
      for (const c of s.clips) {
        const cur = m.get(c.material_id) ?? {
          clipIds: new Set<string>(), clipCount: 0, first: c, sourceIndex: c.source_index ?? null,
          spanMs: 0, durationMs: null,
        }
        cur.clipIds.add(c.id)
        cur.clipCount += 1
        // 累加 span = 覆盖区间长度（兜底时长来源，与原视频真实长度近似）
        const seg = Math.max(0, (c.clip_end_ms ?? 0) - (c.clip_start_ms ?? 0))
        cur.spanMs += seg
        // 优先采用 JOIN 字段（首个非 null 值即可）
        if (cur.durationMs == null && c.material_duration_ms && c.material_duration_ms > 0) {
          cur.durationMs = c.material_duration_ms
        }
        m.set(c.material_id, cur)
      }
    }
    return m
  }, [detail]) // 整个 detail 引用变化即重算（addClips 后 load() 返回新 detail 对象）
  /** 当前高亮材料对应的全部 clip id（高亮联动 ShotCard 用） */
  const highlightClipIds = useMemo(() => {
    const s = new Set<string>()
    if (highlightMaterials.size === 0) return s
    for (const mid of highlightMaterials) {
      const agg = materialAgg.get(mid)
      if (agg) for (const cid of agg.clipIds) s.add(cid)
    }
    return s
  }, [highlightMaterials, materialAgg])

  // #407：所有分镜卡内片段条统一调度：高亮变化时遍历所有命中分镜的 track 滚动到首个高亮 clip（不动祖先滚动条）
  const shotsScrollRef = useRef<HTMLDivElement | null>(null)
  const prevHighlightRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const ids = highlightClipIds
    const prev = prevHighlightRef.current
    const same = prev.size === ids.size && [...ids].every((id) => prev.has(id))
    prevHighlightRef.current = new Set(ids)
    if (same) return
    if (ids.size === 0) return
    const root = shotsScrollRef.current
    if (!root) return
    // 遍历所有 track（按分镜出现顺序），每个 track 内找首个高亮 clip，水平居中滚动
    const tracks = Array.from(root.querySelectorAll<HTMLDivElement>('[data-shot-id]'))
    tracks.forEach((track) => {
      const target = track.querySelector<HTMLElement>('[data-highlighted-clip="true"]')
      if (!target) return
      const trackRect = track.getBoundingClientRect()
      const targetRect = target.getBoundingClientRect()
      const offsetLeft = targetRect.left - trackRect.left + track.scrollLeft
      const targetCenter = offsetLeft + targetRect.width / 2
      const desired = Math.max(0, targetCenter - track.clientWidth / 2)
      track.scrollTo({ left: desired, behavior: 'smooth' })
    })
  }, [highlightClipIds])

  // #407：window 级 capture 拦截 wheel（最暴力，避开所有 div 级 passive listener 干扰）。
  //      通过 elementFromPoint 判断 pointer 位置下的 track，命中则阻止默认 + 横向滚动；
  //      未命中则完全放行（让外层分镜区/页面正常纵向滚）。
  //      root 用 editorRootRef 覆盖整页（MaterialChipsBar 在 shotsScrollRef 外，必须扩范围）。
  useEffect(() => {
    const handler = (e: WheelEvent) => {
      if (e.deltaY === 0) return
      const el = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null
      if (!el) return
      const root = editorRootRef.current
      const track = el.closest<HTMLElement>('[data-chips-bar], [data-shot-id]')
      if (!track || !root || !root.contains(track)) return
      if (track.scrollWidth <= track.clientWidth) return
      e.preventDefault()
      e.stopImmediatePropagation()
      track.scrollLeft += e.deltaY
    }
    window.addEventListener('wheel', handler, { passive: false, capture: true })
    return () => window.removeEventListener('wheel', handler, { capture: true })
  }, [])

  // 选中删除的原视频 id 集合（独立于高亮态，避免互相干扰）
  const [selectedMaterials, setSelectedMaterials] = useState<Set<string>>(new Set())
  // #403：管理模式（与分镜片段管理模式一致）
  const [materialManageMode, setMaterialManageMode] = useState(false)
  const [deletingMaterials, setDeletingMaterials] = useState(false)
  const [confirmDeleteMaterials, setConfirmDeleteMaterials] = useState(false)

  const load = useCallback(async () => {
    // #409：返回 Promise，调用方可 await；确保 detail 已 setState 后再让调用方继续
    try {
      const t = await creationApi.getProject(projectId)
      setDetail(t)
      setBgmStrategy(t.bgm_strategy)
      try {
        setDedup({
          frame_drop: false, watermark: false, noise: false,
          strength: 'low', watermark_text_mode: 'timestamp',
          ...JSON.parse(t.dedup_rules_json || '{}'),
        })
      } catch (e) {
        // #P2-4：dedup_rules_json 格式错误时 toast 提示，不再静默 fallback
        toast(`项目去重规则解析失败：${(e as Error).message}`, 'error')
      }
      // #408：按当前 detail 的 pending 数初始化项目级 cutting 状态，
      //      确保进入详情页时立即显示/隐藏顶部警示条（无需等首次 5s tick）
      const pendingN = (t.shots ?? []).reduce(
        (n: number, s: Shot) =>
          n + s.clips.filter((c) => c.file_status === 'pending').length, 0)
      setProjectCutting(pendingN > 0)
    } catch { /* 静默忽略 */ }
    try { const bgms = await creationApi.listBgms(projectId); setBgms(bgms) } catch { /* 默认 */ }
  }, [projectId])
  // #409：useEffect 接受 void 返回，包裹 load 调用避免返回 Promise
  useEffect(() => { load() }, [load])

  /** 保存项目级 BGM 音量（拖动滑块松手/失焦后调用） */
  const saveBgmVolume = (volume: number) => {
    if (!detail) return
    const persisted = detail.bgm_volume ?? 1.0
    if (Math.abs(persisted - volume) < 0.005) return   // 无变化不发请求
    setDetail({ ...detail, bgm_volume: volume })
    creationApi.updateProject(projectId, { bgm_volume: volume })
      .catch((e) => { toast((e as Error).message, 'error'); load() })
  }

  // #408：项目级"分切处理中"状态；初次由 load() 按 pending 数初始化，轮询持续刷新
  const [projectCutting, setProjectCutting] = useState<boolean>(false)

  // 片段文件/缩略图异步渲染：差异轮询（#轮询优化），仅拉 rev 变化的 clip。
  // #408：触发条件改为 projectCutting（项目级"分切中"状态），固定 5s 间隔；
  //      复用原有 setDetail merge 逻辑，仅替换调度策略。
  // #408-fix：当 r.pending=0（分切完成）时，强制 reload 全量 detail，避免差分轮询漏更新导致 UI 仍显示"处理中"
  const maxRevRef = useRef(0)

  useEffect(() => {
    if (!detail) return
    if (!projectCutting) return  // 非分切中不轮询
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const tick = async () => {
      if (stopped) return
      try {
        const r = await creationApi.clipsStatus(projectId, maxRevRef.current)
        if (stopped) return
        maxRevRef.current = r.max_rev
        if (r.clips.length > 0) {
          // 增量 merge：按 id 更新到 detail.shots[].clips[]（同 #386 已实现）
          setDetail((prev) => {
            if (!prev || !prev.shots) return prev
            const upd = new Map(r.clips.map((c) => [c.id, c]))
            return {
              ...prev,
              shots: prev.shots.map((s) => ({
                ...s,
                clips: s.clips.map((c) => {
                  const u = upd.get(c.id)
                  if (!u) return c
                  return {
                    ...c,
                    file_path: u.file_path,
                    thumb_path: u.thumb_path,
                    file_status: u.file_status,
                    fail_reason: u.fail_reason,
                    rev: u.rev ?? c.rev ?? 0,
                  }
                }),
                clip_ready_count: s.clips.reduce((n, c) => {
                  const u = upd.get(c.id)
                  return n + ((u ? u.file_status : c.file_status) === 'ready' ? 1 : 0)
                }, 0),
                clip_pending_count: s.clips.reduce((n, c) => {
                  const u = upd.get(c.id)
                  return n + ((u ? u.file_status : c.file_status) === 'pending' ? 1 : 0)
                }, 0),
              })),
            }
          })
        }
        // #408-fix：分切完成（r.cutting=false → r.pending=0）时，差分 merge 可能漏更新（worker 完成时若 rev 增量恰好被 maxRev 跳过），
        //          强制 reload 全量 detail，保证 UI 状态一致；同时 load 末尾会再次评估 projectCutting=fasle，停轮询
        if (!r.cutting && !stopped) {
          load()
          return  // 不再排下一拍；cleanup 自然停
        }
        // 仍在分切中 → 同步 cutting 状态（基本不变，保持轮询）
        setProjectCutting(r.cutting)
      } catch { /* 静默,下次重试 */ }
      if (!stopped) timer = setTimeout(tick, 5000)
    }

    tick()  // 立即跑一次
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
    // 仅依赖 projectCutting 与 projectId（避免 setDetail 触发重启循环）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectCutting, projectId])

  if (!detail) return <div className="p-5 text-sm text-muted-foreground">加载中…</div>

  return (
    <div ref={editorRootRef} className="flex h-full">
      {/* 左：分镜列表（#66 顶部固定，仅列表区滚动） */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden p-4">
        <div className="mb-3 flex shrink-0 flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>← 返回列表</Button>
          {/* #116：项目详情页顶部进度摘要，项目名 + 分镜头 X/上限 + 已生成 X/可生成 X，数值加粗 */}
          <span className="text-sm text-muted-foreground">
            <span className="font-semibold text-foreground">{detail.title}</span>
            {' '}分镜头{' '}
            <span className="font-semibold text-foreground">{detail.shots?.length ?? 0}</span>
            /<span className="font-semibold text-foreground">50</span>
            {' '}已生成{' '}
            <span className="font-semibold text-foreground">{detail.generated_total ?? 0}</span>
            /<span className="font-semibold text-foreground">{detail.combination_legal_total ?? 0}</span>
          </span>
          {/* #408：项目分切处理中 → 顶部中间蓝色警示条（spinner + 提示文案） */}
          {projectCutting && (
            <span
              data-testid="cutting-banner"
              className="ml-2 flex items-center gap-1.5 rounded-md border border-blue-300 bg-blue-50 px-2.5 py-1 text-xs text-blue-700"
            >
              <svg className="h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
              </svg>
              正在分切视频，系统会自动刷新结果
            </span>
          )}
          {/* #65：添加视频 / 添加分镜 / 插入分镜 同一行，去掉「+」前缀 */}
          <div className="ml-auto flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setAddClipsOpen(true)}>添加视频</Button>
            <Button variant="outline" size="sm"
              onClick={async () => {
                try { await creationApi.addShot(projectId); load() } catch (e) { toast((e as Error).message, 'error') }
              }}>添加分镜</Button>
            <Button variant="outline" size="sm" onClick={() => setInsertOpen(true)}>插入分镜</Button>
          </div>
        </div>
        {/* #71：可生成为 0 时给出原因（倒推规则下只有"存在空分镜"会导致 0） */}
        {(() => {
          const shots = detail.shots ?? []
          if (shots.length === 0) return null
          const emptyCount = shots.filter((s) => s.clip_count === 0).length
          if (emptyCount > 0) {
            return (
              <div className="mb-3 shrink-0 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                有 {emptyCount} 个分镜没有片段，无法生成成品。请「添加视频」补充片段。
              </div>
            )
          }
          return null
        })()}
        {/* #399：原视频预览图横向固定列表（点选高亮对应片段 + 批量删除入口） */}
        <MaterialChipsBar
          agg={materialAgg}
          highlightMaterials={highlightMaterials}
          selectedMaterials={selectedMaterials}
          manageMode={materialManageMode}
          deleting={deletingMaterials}
          onToggleHighlight={(mid, additive) => {
            setHighlightMaterials((prev) => {
              if (additive) {
                const next = new Set(prev); if (next.has(mid)) next.delete(mid); else next.add(mid); return next
              }
              // 单点：互斥
              return prev.has(mid) && prev.size === 1 ? new Set() : new Set([mid])
            })
          }}
          onToggleSelect={(mid) => {
            setSelectedMaterials((prev) => {
              const next = new Set(prev); if (next.has(mid)) next.delete(mid); else next.add(mid); return next
            })
          }}
          onClearSelection={() => {
            // #403 全选/反选：manageMode 全选则清空，否则全选
            if (materialManageMode && selectedMaterials.size === materialAgg.size) {
              setSelectedMaterials(new Set())
            } else {
              setSelectedMaterials(new Set(materialAgg.keys()))
            }
          }}
          onAskDelete={() => setConfirmDeleteMaterials(true)}
          onEnterManage={() => setMaterialManageMode(true)}
          onExitManage={() => { setMaterialManageMode(false); setSelectedMaterials(new Set()) }}
        />
        {/* #380：顶部失败计数 + 一键重试（按当前所有 failed 片段 ID 批量 retry_render） */}
        <FailedBanner detail={detail} onChanged={load} />
        <div ref={shotsScrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto">
          {(detail.shots ?? []).map((s: Shot, i: number) => (
            <ShotCard key={s.id} shot={s} shots={detail.shots ?? []} index={i} onChanged={load}
              highlightClipIds={highlightClipIds} />
          ))}
          {(detail.shots ?? []).length === 0 && (
            <div className="py-8 text-center text-sm text-muted-foreground">暂无分镜，点击上方「添加分镜」，或直接「添加视频」自动按视频段数建分镜</div>
          )}
        </div>

        {/* #399：批量删除原视频对应片段的二次确认弹窗 */}
        <ConfirmDialog open={confirmDeleteMaterials} onOpenChange={setConfirmDeleteMaterials}
          title="批量删除原视频对应片段" danger
          content={(() => {
            const n = selectedMaterials.size
            const clipCount = [...selectedMaterials].reduce((s, mid) => s + (materialAgg.get(mid)?.clipCount ?? 0), 0)
            return `确定删除 ${n} 个原视频对应的全部 ${clipCount} 个片段？对应片段文件也会一并删除（素材库中的原视频保留，可重新添加）。`
          })()}
          onConfirm={async () => {
            const ids = [...selectedMaterials]
            if (ids.length === 0) { setConfirmDeleteMaterials(false); return }
            setConfirmDeleteMaterials(false)
            setDeletingMaterials(true)
            try {
              const r = await creationApi.deleteClipsByMaterials(projectId, ids)
              toast(`已删除 ${r.deleted} 个片段`, 'success')
              setSelectedMaterials(new Set())
              setHighlightMaterials(new Set())
              load()
            } catch (e) { toast((e as Error).message, 'error') }
            finally { setDeletingMaterials(false) }
          }} />
      </div>

      {/* 右：配置栏（#406 可折叠） */}
      <div className={cn(
        'relative shrink-0 bg-background-elev transition-all',
        // #406：收起时栏宽收到与按钮同档（≈ 按钮 w-3 = 12px），去掉 border-l 避免占用额外像素
        rightCollapsed ? 'w-4 border-border/0' : 'w-72 border-l border-border',
      )}>
        {/* #406：折叠/展开切换按钮 — 永远在右栏外缘、垂直居中。
            展开时贴右栏右边外侧（right: -12px），箭头« 指向左=收起方向；
            收起时贴窄条左边外侧（left: -12px），箭头» 指向右=展开方向。
            方向与"点它会发生的动作"一致，符合用户直觉。 */}
        <button type="button" onClick={() => setRightCollapsed(!rightCollapsed)}
          title={rightCollapsed ? '展开配置栏' : '收起配置栏'}
          aria-label={rightCollapsed ? '展开配置栏' : '收起配置栏'}
          className="absolute left-0 top-1/2 z-10 flex h-8 w-3 -translate-y-1/2 items-center justify-center rounded-r-md bg-muted text-muted-foreground shadow hover:bg-accent hover:text-white">
          {/* #406：箭头 = 内容移动方向。展开→点后内容向左收起 → ‹指左；
              收起→点后内容向右展开 → ›指右。位置永远贴右栏左外缘。 */}
          <span aria-hidden="true">{rightCollapsed ? '<' : '>'}</span>
        </button>
        {!rightCollapsed && (
        <div className="space-y-4 overflow-auto p-4">
        <div>
          {/* #98 + #99：背景音乐区，不列出已选；点击添加按钮 picker 内部回显已选 */}
          <div className="mb-2 text-sm font-semibold">背景音乐（{bgms.length}）</div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setBgmPickerOpen(true)}>+ 添加音乐</Button>
            {/* 清空所有 BGM（#91）：二次确认后逐条 remove */}
            {bgms.length > 0 && (
              <Button variant="ghost" size="sm" className="!text-danger"
                onClick={() => {
                  if (!window.confirm(`确定清空全部 ${bgms.length} 个背景音乐？`)) return
                  Promise.all(bgms.map((b) => creationApi.removeBgm(projectId, b.material_id)))
                    .then(() => { toast('背景音乐已清空', 'success'); load() })
                    .catch((e) => toast((e as Error).message, 'error'))
                }}>清空</Button>
            )}
            <select className="ml-auto h-7 rounded-md border border-border bg-white px-1 text-xs" value={bgmStrategy}
              onChange={(e) => creationApi.updateProject(projectId, { bgm_strategy: e.target.value }).then(load)}>
              <option value="random">随机使用</option>
              <option value="order">顺序使用</option>
            </select>
          </div>
        </div>
        <div>
          {/* #96 + #99：保留原音开关 + 项目级音量滑块；keep 与提示分两行 */}
          {detail && (
            <>
              <label className="mb-1 flex items-center gap-2 text-sm">
                <input type="checkbox" checked={Boolean(detail.keep_original_audio)}
                  onChange={(e) => {
                    const v = e.target.checked
                    setDetail({ ...detail, keep_original_audio: v })
                    creationApi.updateProject(projectId, { keep_original_audio: v })
                      .catch((e2) => { toast((e2 as Error).message, 'error'); load() })
                  }} />
                保留原视频声音
              </label>
              <div className="mb-2 text-xs text-muted-foreground">默认关闭，仅与背景音乐一起启用时叠加原音</div>
              <div className="mb-3 flex items-center gap-2 text-sm">
                <span className="shrink-0">音量</span>
                <input type="range" min={5} max={100} step={5}
                  value={Math.round((bgmVolumeDraft ?? detail.bgm_volume ?? 1.0) * 100)}
                  className="flex-1 accent-blue-600"
                  onChange={(e) => setBgmVolumeDraft(Number(e.target.value) / 100)}
                  onMouseUp={(e) => saveBgmVolume(Number((e.target as HTMLInputElement).value) / 100)}
                  onTouchEnd={(e) => saveBgmVolume(Number((e.target as HTMLInputElement).value) / 100)}
                  onBlur={(e) => saveBgmVolume(Number(e.target.value) / 100)} />
                <span className="w-10 shrink-0 text-right text-muted-foreground">
                  {Math.round((bgmVolumeDraft ?? detail.bgm_volume ?? 1.0) * 100)}%
                </span>
              </div>
            </>
          )}
          {/* #426：画幅 + 画面模式 */}
          {detail && (
            <div className="mb-3">
              <div className="mb-1 text-sm font-semibold">
                画幅（{detail.output_resolution || '1080x1920'}）
              </div>
              <div className="mb-2 flex items-center gap-2 text-xs">
                {/* 只读四边形展示当前画幅 */}
                <div className="flex h-12 items-center justify-center rounded-md border border-border bg-gray-50 px-3">
                  <div className="rounded border-2 border-blue-600 bg-blue-100"
                    style={{
                      aspectRatio: `${detail.width || 1080} / ${detail.height || 1920}`,
                      height: '32px',
                    }} />
                </div>
                <span className="text-muted-foreground">
                  {resolutionPresets.find((p) =>
                    p.w === (detail.width || 1080) && p.h === (detail.height || 1920))?.platform || ''}
                </span>
              </div>
              {/* 画面模式（#430 PreviewSelect） */}
              <div className="mb-1 text-xs font-medium text-muted-foreground">画面模式</div>
              <PreviewSelect value={detail.fit_mode || 'cover'}
                onChange={(k) => {
                  const fm = k as NonNullable<Project['fit_mode']>
                  setDetail({ ...detail, fit_mode: fm })
                  creationApi.updateProject(projectId, { fit_mode: fm })
                    .catch((e2) => { toast((e2 as Error).message, 'error'); load() })
                }}
                options={([
                  ['cover', '铺满裁剪', '默认，无黑边'],
                  ['contain', '居中补黑', '全内容可见'],
                  ['fill', '拉伸', '可能变形'],
                  ['blur_bg', '模糊背景', '高端风格'],
                ] as const).map(([k, label, tip]) => ({
                  value: k,
                  label,
                  sub: tip,
                  preview: fitModePreview(k),
                }))} />
            </div>
          )}
          <div className="mb-2 text-sm font-semibold">去重规则</div>
          {([
            ['frame_drop', '自动抽帧', '在 30 秒内随机丢弃 1~6 帧（按强度变化），肉眼几乎不可察，打破逐帧指纹。'],
            ['watermark', '动态水印', '在视频画面叠加半透明文字水印（位置/透明度随机），每条成品位置不同。'],
            ['noise', '动态干扰', '对画面整体做 ±1~3% 缩放、亮度偏移、边缘裁剪微扰，按强度放大。'],
          ] as const).map(([k, label, tip]) => (
            <div key={k}>
              <label className="flex items-center gap-1 py-1 text-sm">
                <input type="checkbox" checked={dedup[k]}
                  onChange={(e) => {
                    const next = { ...dedup, [k]: e.target.checked }
                    setDedup(next)
                    creationApi.updateProject(projectId, { dedup_rules: next }).catch((e) => console.warn('[Creation] 请求失败:', e))
                  }} />
                {label}
                <Tooltip content={tip}><span className="help-icon" aria-label="说明">ⓘ</span></Tooltip>
              </label>
              {/* 水印开关打开时，紧贴在下方显示内容组件（不开不显示） */}
              {k === 'watermark' && dedup.watermark && (
                <div className="mb-1 ml-6 space-y-1.5 text-sm">
                  <div className="flex items-center gap-2">
                    <span className="text-muted-foreground">水印内容：</span>
                    <select className="h-7 rounded-md border border-border bg-white px-1 text-xs"
                      value={dedup.watermark_text_mode ?? 'timestamp'}
                      onChange={(e) => {
                        const next = { ...dedup, watermark_text_mode: e.target.value as 'timestamp' | 'title' | 'custom' }
                        setDedup(next)
                        creationApi.updateProject(projectId, { dedup_rules: next }).catch((e) => console.warn('[Creation] 请求失败:', e))
                      }}>
                      <option value="timestamp">时间戳</option>
                      <option value="title">项目名称</option>
                      <option value="custom">自定义文本</option>
                    </select>
                    <Tooltip content="选择水印显示的内容来源。时间戳=显示本次生成时刻；项目名称=显示项目标题；自定义=使用下方输入框文本。">
                      <span className="help-icon" aria-label="说明">ⓘ</span>
                    </Tooltip>
                  </div>
                  {dedup.watermark_text_mode === 'custom' && (
                    <div className="flex items-center gap-2">
                      <span className="text-muted-foreground">自定义：</span>
                      <input className="h-7 flex-1 rounded-md border border-border bg-white px-2 text-xs"
                        maxLength={16}
                        placeholder="最多 16 字"
                        value={dedup.watermark_text_custom ?? ''}
                        onChange={(e) => {
                          const next = { ...dedup, watermark_text_custom: e.target.value }
                          setDedup(next)
                          creationApi.updateProject(projectId, { dedup_rules: next }).catch((e) => console.warn('[Creation] 请求失败:', e))
                        }} />
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          <div className="mt-1 flex items-center gap-2 text-sm">
            <span>强度：</span>
            <select className="h-7 rounded-md border border-border bg-white px-1 text-xs" value={dedup.strength}
              onChange={(e) => {
                const next = { ...dedup, strength: e.target.value as 'low' | 'mid' | 'high' }
                setDedup(next)
                creationApi.updateProject(projectId, { dedup_rules: next }).catch((e) => console.warn('[Creation] 请求失败:', e))
              }}>
              <option value="low">低</option>
              <option value="mid">中</option>
              <option value="high">高</option>
            </select>
            <Tooltip content="控制上面 3 个开关的扰动幅度：低=几乎不可察；中=仔细看能发现；高=一眼能看出变形/跳帧/水印。">
              <span className="help-icon" aria-label="说明">ⓘ</span>
            </Tooltip>
          </div>
        </div>
        </div>
        )}
      </div>

      <AddClipsDialog open={addClipsOpen} onOpenChange={setAddClipsOpen} projectId={projectId}
        shots={detail.shots ?? []} projectWidth={detail.width} projectHeight={detail.height} onDone={load} />
      {/* #91：点添加打开 picker → 内部回显当前已选 → 勾选后 diff 增删 */}
      <MaterialPicker open={bgmPickerOpen} onOpenChange={setBgmPickerOpen} type="music" multi
        initialSelected={bgms.map((b) => ({
          id: b.material_id,
          title: b.title ?? '',
          type: 'music',
          category_id: '',
          duration_ms: b.duration_ms ?? null,
          file_size: 0,
          source_type: 'pull',
          source_ref: null,
          file_md5: '',
          resolution: null,
          orientation: null,
          file_path: '',
          file_status: (b.file_status === 'missing' ? 'missing' : 'normal') as 'normal' | 'missing',
          ref_count: 0,
          create_time: '',
        } as Material))}
        onConfirm={async (ids) => {
          try {
            // #91：diff 增删（picker 内部状态即"目标全集"，提交时算差集）
            const currentIds = new Set(bgms.map((b) => b.material_id))
            const toAdd = ids.filter((id) => !currentIds.has(id))
            const toRemove = [...currentIds].filter((id) => !ids.includes(id))
            if (toAdd.length) await creationApi.addBgms(projectId, toAdd)
            for (const id of toRemove) await creationApi.removeBgm(projectId, id)
            setBgmPickerOpen(false)
            toast('BGM 已更新', 'success')
            load()
          } catch (e) { toast((e as Error).message, 'error') }
        }} />
      {/* #67：插入分镜（位置列表用「之前」，选第一个即插入到最前，无需单列「最前面」） */}
      <Dialog open={insertOpen} onOpenChange={setInsertOpen} title="插入分镜" width={380}
        footer={
          <>
            <Button variant="outline" size="sm" onClick={() => setInsertOpen(false)}>取消</Button>
            <Button size="sm" disabled={!insertBefore} onClick={async () => {
              try {
                await creationApi.addShot(projectId, '', undefined, insertBefore)
                setInsertOpen(false)
                setInsertBefore('')
                load()
              } catch (e) { toast((e as Error).message, 'error') }
            }}>插入</Button>
          </>
        }>
        <div className="space-y-2">
          <label className="block text-sm font-medium">插入到哪个分镜之前</label>
          {/* #70：默认不预选，需用户手动选择后才能插入 */}
          <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
            value={insertBefore} onChange={(e) => setInsertBefore(e.target.value)}>
            <option value="">请选择分镜…</option>
            {(detail.shots ?? []).map((s) => (
              <option key={s.id} value={s.id}>「{s.name}」之前</option>
            ))}
          </select>
          <div className="text-xs text-muted-foreground">选择第一个分镜即插入到最前面，其余分镜顺序自动后移。</div>
        </div>
      </Dialog>
    </div>
  )
}

// #414：标题/话题池原 TextPoolDialog 已被 ProjectIntrosDrawer（多行文本框）替代，组件删除。

/** 成品发布情况文案（#68） */
const PUBLISH_STATE: Record<string, { label: string; cls: string }> = {
  published: { label: '已发布', cls: 'text-ok' },
  scheduled: { label: '待发布', cls: 'text-accent' },
  waiting: { label: '待发布', cls: 'text-accent' },
  publishing: { label: '发布中', cls: 'text-accent' },
  suspended: { label: '已挂起', cls: 'text-warn' },
  failed: { label: '发布失败', cls: 'text-danger' },
  cancelled: { label: '已取消', cls: 'text-muted-foreground' },
  unpublished: { label: '未发布', cls: 'text-muted-foreground' },
}

/** 成品视频预览弹窗（#78：播放成品文件） */
function VideoPreviewDialog({ video, open, onOpenChange }: {
  video: GeneratedVideo | null
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [src, setSrc] = useState('')

  useEffect(() => {
    let alive = true
    if (open && video) fileUrl(video.file_path).then((u) => { if (alive) setSrc(u) })
    else setSrc('')
    return () => { alive = false }
  }, [open, video])

  if (!video) return null
  // #83：与素材库的视频浮窗播放保持一致的样式（无底部按钮与说明行）
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={video.file_path.split('/').pop() ?? '成品预览'} width={520}>
      {src ? (
        <video src={src} className="mx-auto max-h-[70vh] w-full rounded-md bg-black" controls autoPlay
          onError={() => toast('视频加载失败（文件缺失或格式不支持）', 'error')} />
      ) : (
        <div className="flex aspect-9/16 items-center justify-center rounded-md bg-muted text-muted-foreground">
          加载中…
        </div>
      )}
    </Dialog>
  )
}

/** 成品视频 Tab（F-04.10 / #68；#78 查询区移至页签行、新增预览按钮；#350 列表首列多选 + 列头全选） */
function VideosTab({ projectId, status, refreshTrigger, onLoaded, checkedIds, onCheckedChange }: {
  projectId: string
  status: string
  refreshTrigger?: number
  /** load 完成回调（用于父级按钮恢复 loading=false） */
  onLoaded?: () => void
  /** #350：勾选状态由父级控制，便于顶部删除按钮统一操作 */
  checkedIds: Set<string>
  onCheckedChange: (next: Set<string>) => void
}) {
  const [data, setData] = useState<{ list: GeneratedVideo[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [deleteTarget, setDeleteTarget] = useState<GeneratedVideo | null>(null)
  const [previewVideo, setPreviewVideo] = useState<GeneratedVideo | null>(null)

  // 筛选条件变化时回到第一页
  useEffect(() => { setPage(1) }, [projectId, status])

  const load = useCallback(() => {
    creationApi.listVideos({ project_id: projectId, status, page, page_size: pageSize })
      .then((r) => {
        setData({ list: r.list, total: r.total })
      }).catch((e) => console.warn('[Creation] 请求失败:', e))
      .finally(() => onLoaded?.())
  }, [projectId, status, page, pageSize, onLoaded])
  useEffect(load, [load])
  useEffect(() => { if (refreshTrigger) load() }, [refreshTrigger, load]) // eslint-disable-line react-hooks/exhaustive-deps

  const fmtDur = (ms: number | null) => {
    if (ms == null) return '-'
    const s = Math.round(ms / 1000)
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* #78：查询条件已移至页面顶部页签行，此处仅保留表格 */}
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              {/* #350/#353/#357：列头多选框（全选/取消全选当前页；#353 只勾选 status==='idle' 可删项；#357 无可删项时不视为全选） */}
              <th className="w-10 px-4 py-2.5">
                <input type="checkbox" className="h-4 w-4 accent-blue-600"
                  checked={(() => {
                    const deletable = data.list.filter((v) => v.status === 'idle')
                    return deletable.length > 0 && deletable.every((v) => checkedIds.has(v.id))
                  })()}
                  onChange={(e) => {
                    if (e.target.checked) onCheckedChange(new Set(data.list.filter((v) => v.status === 'idle').map((v) => v.id)))
                    else onCheckedChange(new Set())
                  }} />
              </th>
              <th className="px-4 py-2.5 font-medium">文件</th>
              <th className="px-4 py-2.5 font-medium">项目</th>
              <th className="px-4 py-2.5 font-medium">时长</th>
              <th className="px-4 py-2.5 font-medium">大小</th>
              <th className="px-4 py-2.5 font-medium">占用状态</th>
              <th className="px-4 py-2.5 font-medium">发布情况</th>
              <th className="px-4 py-2.5 font-medium">创建时间</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((v) => (
              <tr key={v.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                {/* #350：行首多选框；仅未占用可勾选（后端按 status 二次校验） */}
                <td className="px-4 py-2.5">
                  <input type="checkbox" className="h-4 w-4 accent-blue-600"
                    checked={checkedIds.has(v.id)}
                    disabled={v.status !== 'idle'}
                    onChange={(e) => {
                      const next = new Set(checkedIds)
                      if (e.target.checked) next.add(v.id)
                      else next.delete(v.id)
                      onCheckedChange(next)
                    }} />
                </td>
                <td className="max-w-56 truncate px-4 py-2.5 font-mono text-xs" title={v.file_path}>{v.file_path.split('/').pop()}</td>
                <td className="px-4 py-2.5">{v.project_title || '-'}</td>
                <td className="px-4 py-2.5">{fmtDur(v.duration_ms)}</td>
                <td className="px-4 py-2.5">{v.file_size ? `${(v.file_size / 1048576).toFixed(1)}MB` : '-'}</td>
                <td className="px-4 py-2.5"><StatusBadge status={v.status} /></td>
                <td className="px-4 py-2.5 text-xs">
                  {PUBLISH_STATE[v.publish_state ?? 'unpublished']?.label ?? '未发布'}
                  {v.publish_account && (
                    <div className="text-muted-foreground" title={v.publish_time ?? ''}>
                      {v.publish_account}
                      {v.publish_time ? ` · ${v.publish_time}` : ''}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{v.create_time}</td>
                <td className="px-4 py-2.5">
                  <div className="flex gap-1">
                    {/* #78：预览成品视频 */}
                    <Button variant="outline" size="sm" onClick={() => setPreviewVideo(v)}>预览</Button>
                    {window.electronAPI && (
                      <Button variant="outline" size="sm"
                        onClick={async () => {
                          try {
                            const r = await request<{ abs_path: string; exists: boolean }>(`/creation/videos/${v.id}/abs-path`)
                            if (r.exists) window.electronAPI!.openPath(r.abs_path)
                            else toast('文件不存在', 'error')
                          } catch (e) { toast((e as Error).message, 'error') }
                        }}>
                        定位
                      </Button>
                    )}
                    {v.status === 'idle' && (
                      <Button variant="outline" size="sm" className="!text-danger" onClick={() => setDeleteTarget(v)}>删除</Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {data.list.length === 0 && (
              <tr><td colSpan={9} className="px-4 py-12 text-center text-muted-foreground">暂无成品，去项目中生成</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
        onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
      <ConfirmDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)} title="删除成品" danger
        content={`确定删除成品「${deleteTarget?.file_path.split('/').pop()}」？删除后对应组合释放，可重新生成。`}
        onConfirm={async () => {
          if (!deleteTarget) return
          try {
            await creationApi.deleteVideo(deleteTarget.id)
            toast('已删除', 'success')
            load()
          } catch (e) { toast((e as Error).message, 'error') }
        }} />

      {/* #78：成品视频预览 */}
      <VideoPreviewDialog video={previewVideo} open={!!previewVideo}
        onOpenChange={(v) => !v && setPreviewVideo(null)} />
    </div>
  )
}

/** 创作中心页（9.3.4：项目列表 / 项目编辑 / 成品视频） */
export function CreationPage() {
  const [view, setView] = useState<'list' | 'edit'>('list')
  const [editingId, setEditingId] = useState('')
  const [tab, setTab] = useState<'projects' | 'videos'>('projects')
  const [data, setData] = useState<{ list: ProjectRow[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [deleteTarget, setDeleteTarget] = useState<ProjectRow | null>(null)
  // #384：项目页签正在删除状态（用于列表中心显示"正在删除…"提示）
  const [deleting, setDeleting] = useState(false)
  // #414：门店/标题话题右侧抽屉入口
  const [shopsDrawerId, setShopsDrawerId] = useState('')
  const [introDrawerId, setIntroDrawerId] = useState('')
  const [genTarget, setGenTarget] = useState<ProjectRow | null>(null)
  const [genCount, setGenCount] = useState(5)
  const [genBusy, setGenBusy] = useState(false)
  const [prompt, promptDialog] = usePrompt()
  // #78：成品视频查询条件提升到页面级，与顶部页签同一行显示
  const [videoProjects, setVideoProjects] = useState<ProjectRow[]>([])
  const [videoProjectId, setVideoProjectId] = useState('')
  const [videoStatus, setVideoStatus] = useState('')
  // 页面级刷新按钮 loading 状态：项目 tab 走下方 ProjectsTab 内 load，loading 由 onLoaded 回调关闭
  const [refreshing, setRefreshing] = useState(false)
  // #350：成品视频批量删除已选项（仅未发布可删，由后端按 status=occupied 校验）
  const [videosChecked, setVideosChecked] = useState<Set<string>>(new Set())
  /** 批量删除已选成品：并发调单条 deleteVideo，统计成功/失败 */
  const doBatchDeleteVideos = async () => {
    if (videosChecked.size === 0) return
    const ids = [...videosChecked]
    const results = await Promise.allSettled(ids.map((id) => creationApi.deleteVideo(id)))
    const ok = results.filter((r) => r.status === 'fulfilled').length
    const fail = results.length - ok
    if (ok > 0) toast(`已删除 ${ok} 个成品${fail > 0 ? `，${fail} 个失败` : ''}`, ok === results.length ? 'success' : 'info')
    else toast('删除失败', 'error')
    setVideosChecked(new Set())
    triggerRefresh()
  }

  const load = useCallback(() => {
    return creationApi.listProjects(page, pageSize)
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Creation] 请求失败:', e))
  }, [page, pageSize])

  // 父级刷新按钮触发（避免父级 useImperativeHandle 的复杂写法；项目 tab 内部也用相同模式）
  const [refreshTrigger, setRefreshTrigger] = useState(0)
  // 初次进入页面 / 切换 tab 时 setRefreshing(true) + 递增 trigger，让按钮显示"刷新中"
  useEffect(() => {
    if (view === 'list') {
      setRefreshing(true)
      setRefreshTrigger((t) => t + 1)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, tab])
  useEffect(() => {
    if (refreshTrigger && view === 'list' && tab === 'projects') {
      // 父级函数没有 onLoaded 子组件回调，load 完成后自己关 loading
      load().finally(() => setRefreshing(false))
    }
  }, [refreshTrigger]) // eslint-disable-line react-hooks/exhaustive-deps

  /** 通用刷新触发：同步 setLoading(true) + 递增 trigger，由对应 tab 的 load 完成后回调 onLoaded 关闭 */
  const triggerRefresh = () => {
    setRefreshing(true)
    setRefreshTrigger((t) => t + 1)
  }

  /** VideosTab load 完成回调（稳定引用，避免子组件 useCallback 依赖 onLoaded 死循环） */
  const onVideosLoaded = useCallback(() => setRefreshing(false), [])

  // 成品视频页签的项目筛选下拉数据源（切换页签时加载，保证项目增删后仍然最新）
  useEffect(() => {
    if (tab === 'videos') {
      creationApi.listProjects(1, 200).then((r) => setVideoProjects(r.list)).catch((e) => console.warn('[Creation] 请求失败:', e))
    }
  }, [tab])

  // #426：拉取画幅预设列表 + #428：新建项目弹窗 state（提升到 CreationPage，避免子组件闭包外引用 ReferenceError）
  const [resolutionPresets, setResolutionPresets] = useState<
    Array<{ key: string; w: number; h: number; label: string; platform?: string }>
  >([])
  const [createOpen, setCreateOpen] = useState(false)
  const [createTitle, setCreateTitle] = useState('')
  const [createSize, setCreateSize] = useState<{ w: number; h: number }>(
    { w: 1080, h: 1920 })
  const [createFitMode, setCreateFitMode] = useState<string>('cover')
  useEffect(() => {
    creationApi.resolutionPresets().then(setResolutionPresets).catch((e) => console.warn('[Creation] 请求失败:', e))
  }, [])

  if (view === 'edit') {
    return <ProjectEditor projectId={editingId} resolutionPresets={resolutionPresets}
      onClose={() => { setView('list'); load() }} />
  }

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4 flex shrink-0 flex-wrap items-center gap-4">
        <h1 className="flex h-8 items-center text-lg font-semibold">创作中心</h1>
        <div className="flex gap-4 text-sm">
          {([['projects', '项目'], ['videos', '成品视频']] as const).map(([k, l]) => (
            <button key={k} className={tab === k ? 'font-medium text-accent' : 'text-muted-foreground hover:text-foreground'}
              onClick={() => { setTab(k); triggerRefresh(); if (k === 'videos') setVideosChecked(new Set()) }}>{l}</button>
          ))}
        </div>
        {/* #350：成品视频筛选区放在刷新按钮左侧；去掉"共x条"与"已占用"说明；右侧加批量删除 */}
        {tab === 'videos' && (
          <div className="flex items-center gap-3">
            <select className="h-8 max-w-56 rounded-md border border-border bg-white px-2 text-sm"
              value={videoProjectId} onChange={(e) => setVideoProjectId(e.target.value)}>
              <option value="">全部项目</option>
              {videoProjects.map((t) => (
                <option key={t.id} value={t.id}>{t.title}</option>
              ))}
            </select>
            <select className="h-8 rounded-md border border-border bg-white px-2 text-sm" value={videoStatus}
              onChange={(e) => setVideoStatus(e.target.value)}>
              <option value="">全部占用状态</option>
              <option value="idle">未占用</option>
              <option value="occupied">已占用</option>
            </select>
          </div>
        )}
        <Button size="sm" variant="outline" disabled={refreshing} onClick={triggerRefresh}>
          {refreshing ? '刷新中…' : '刷新'}
        </Button>
        {tab === 'projects' ? (
          <Button size="sm" variant="outline" className="ml-auto"
            onClick={() => setCreateOpen(true)}>新建项目</Button>
        ) : (
          /* #350：顶部右侧批量删除按钮（仅未发布可删） */
          <div className="ml-auto flex items-center gap-2">
            {videosChecked.size > 0 && (
              <span className="text-sm text-muted-foreground">已选 {videosChecked.size} 项</span>
            )}
            <Button size="sm" variant="outline" className="!text-danger" disabled={videosChecked.size === 0}
              onClick={doBatchDeleteVideos}>
              删除
            </Button>
          </div>
        )}
      </div>

      {tab === 'projects' ? (
        <div className="flex min-h-0 flex-1 flex-col">
        <div className="relative min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
          {/* #384：删除期间在列表中心显示"正在删除…"，阻断重复点击 */}
          {deleting && (
            <div className="pointer-events-auto absolute inset-0 z-10 flex items-center justify-center bg-white/60 backdrop-blur-[1px]"
              role="status" aria-live="polite">
              <div className="flex items-center gap-3 rounded-md border border-border bg-white px-4 py-2.5 text-sm shadow-sm">
                <svg className="h-4 w-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                  <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                </svg>
                <span className="font-medium text-foreground">正在删除…</span>
              </div>
            </div>
          )}
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">标题</th>
                <th className="px-4 py-2.5 font-medium">分镜数</th>
                {/* #77：新增片段数（全部分镜的片段合计） */}
                <th className="px-4 py-2.5 font-medium">片段数</th>
                {/* #77：原「能生成总数」改名（组合上界） */}
                <th className="px-4 py-2.5 font-medium">可生成数</th>
                {/* #77：原「已生成/可生成」精简为已生成条数 */}
                <th className="px-4 py-2.5 font-medium">已生成数</th>
                <th className="px-4 py-2.5 font-medium">创建时间</th>
                <th className="px-4 py-2.5 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {data.list.map((t) => (
                <tr key={t.id}
                  className={cn('cursor-pointer border-b border-border last:border-0 hover:bg-muted/50',
                    /* #416：停用项目行整体灰显 */ t.status === 'disabled' && 'opacity-60')}
                  onClick={() => { setEditingId(t.id); setView('edit') }}>
                  <td className="px-4 py-2.5 font-medium">{t.title}</td>
                  <td className="px-4 py-2.5">{t.shot_count_real}</td>
                  <td className="px-4 py-2.5">{t.clip_count}</td>
                  <td className="px-4 py-2.5">{t.combination_total}</td>
                  <td className="px-4 py-2.5">{t.generated_total}</td>
                  <td className="px-4 py-2.5 text-xs text-muted-foreground">{t.create_time}</td>
                  <td className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                    <div className="flex gap-1">
                      {/* #416：停用/启用项目。停用后新建发布任务不能选该项目；不影响已有任务/成品。 */}
                      <Button variant="outline" size="sm" disabled={deleting}
                        onClick={async () => {
                          const next = (t.status === 'disabled') ? 'normal' : 'disabled'
                          try {
                            await publishIntroApi.setProjectStatus(t.id, next)
                            toast(next === 'disabled' ? `已停用「${t.title}」` : `已启用「${t.title}」`, 'success')
                            load()
                          } catch (e2) { toast((e2 as Error).message, 'error') }
                        }}>
                        {t.status === 'disabled' ? '启用' : '停用'}
                      </Button>
                      {/* #414：门店绑定、标题/话题多行文本框（覆盖原 TextPoolDialog）从 ManageTab 迁移到列表操作列 */}
                      <Button variant="outline" size="sm" onClick={() => setShopsDrawerId(t.id)}>门店</Button>
                      <Button variant="outline" size="sm" onClick={() => setIntroDrawerId(t.id)}>标题/话题</Button>
                      <Button variant="outline" size="sm"
                        onClick={() => {
                          // #410：本批生成数量上限 = 可生成数 - 已生成数，默认 5 但不超上限
                          const avail = Math.max(0, (t.combination_total || 0) - (t.generated_total || 0))
                          setGenTarget(t)
                          setGenCount(avail > 0 ? Math.min(5, avail) : 1)
                        }}>生成视频</Button>
                      <Button variant="outline" size="sm"
                        onClick={async () => {
                          const title = await prompt('新项目标题', `${t.title}_副本`)
                          if (title) {
                            try {
                              const nt = await creationApi.copyProject(t.id, title)
                              toast('已复制', 'success')
                              setEditingId(nt.id)
                              setView('edit')
                            } catch (e2) { toast((e2 as Error).message, 'error') }
                          }
                        }}>复制</Button>
                      <Button variant="outline" size="sm" className="!text-danger" disabled={deleting}
                        onClick={() => setDeleteTarget(t)}>删除</Button>
                    </div>
                  </td>
                </tr>
              ))}
              {data.list.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-12 text-center text-muted-foreground">暂无项目，点击「新建项目」</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
        </div>
      ) : (
        <VideosTab projectId={videoProjectId} status={videoStatus} refreshTrigger={refreshTrigger}
          checkedIds={videosChecked} onCheckedChange={setVideosChecked}
          onLoaded={onVideosLoaded} />
      )}

      {/* #414：门店绑定 / 标题话题富文本抽屉（项目列表操作列入口） */}
      {shopsDrawerId && (() => {
        const p = data.list.find((x) => x.id === shopsDrawerId)
        return p ? <ProjectShopsDrawer project={p} open
          onOpenChange={(v) => !v && setShopsDrawerId('')} /> : null
      })()}
      {introDrawerId && (() => {
        const p = data.list.find((x) => x.id === introDrawerId)
        return p ? <ProjectIntrosDrawer project={p} open
          onOpenChange={(v) => !v && setIntroDrawerId('')} /> : null
      })()}

      {/* #63：生成视频（列表入口，弹窗输入本批数量） */}
      {(() => {
        // #410：上限 = 可生成数 - 已生成数
        const legal = genTarget?.combination_total || 0
        const generated = genTarget?.generated_total || 0
        const available = Math.max(0, legal - generated)
        const overLimit = genCount > available
        return (
      <Dialog open={!!genTarget} onOpenChange={(v) => !v && setGenTarget(null)} title="生成视频" width={420}
        footer={
          <>
            <Button variant="outline" size="sm" onClick={() => setGenTarget(null)}>取消</Button>
            <Button size="sm" disabled={genBusy || available <= 0 || overLimit} onClick={async () => {
              if (!genTarget) return
              // #410：提交前再次夹紧，超限直接拒绝（兜底后端校验）
              if (genCount > available) {
                toast(`本批数量 ${genCount} 超过剩余可生成数 ${available}，请调整后再提交`, 'error')
                return
              }
              if (genCount < 1) { toast('本批数量至少 1 条', 'error'); return }
              setGenBusy(true)
              try {
                const r = await creationApi.generate(genTarget.id, genCount)
                toast(`生成任务已创建（${r.plan_count} 条），进度见任务队列`, 'success')
                setGenTarget(null)
                load()
              } catch (e) { toast((e as Error).message, 'error') } finally { setGenBusy(false) }
            }}>{genBusy ? '提交中…' : '生成'}</Button>
          </>
        }>
        <div className="space-y-2">
          <div className="text-sm">项目：<span className="font-medium">{genTarget?.title}</span></div>
          <div className="text-xs text-muted-foreground">
            已生成 <span className="font-medium text-foreground">{generated}</span> / 可生成 <span className="font-medium text-foreground">{legal}</span>，
            本批最多 <span className="font-medium text-foreground">{available}</span> 条
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm">本批生成数量：</span>
            <input type="number" min={1} max={available || 1} value={genCount}
              className="h-8 w-24 rounded-md border border-border bg-white px-2 text-sm"
              onChange={(e) => {
                const raw = Number(e.target.value)
                if (Number.isNaN(raw)) { setGenCount(1); return }
                // #410：onChange 内夹紧到 [1, available]
                setGenCount(Math.max(1, Math.min(raw, available || 1)))
              }} />
            <span className="text-xs text-muted-foreground">条</span>
          </div>
          {available <= 0 ? (
            <div className="text-xs text-danger">素材池已耗尽，无法继续生成；请先扩充素材或删除部分成品</div>
          ) : overLimit ? (
            <div className="text-xs text-danger">已超出本批上限 {available}，请调整数量</div>
          ) : (
            <div className="text-xs text-muted-foreground">
              按分镜与组合规则生成（同一成品内同源素材不重复）；本批数量上限 = 可生成数 − 已生成数
            </div>
          )}
        </div>
      </Dialog>
        )
      })()}

      <ConfirmDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)} title="删除项目" danger
        content={`确定删除项目「${deleteTarget?.title}」？成品文件可选择保留（删除后不可恢复项目配置）。`}
        onConfirm={async () => {
          if (!deleteTarget) return
          const targetId = deleteTarget.id
          setDeleteTarget(null)
          // #384：删除期间在列表中心显示"正在删除…"，避免用户重复点击造成歧义
          setDeleting(true)
          try {
            await creationApi.deleteProject(targetId, false)
            toast('已删除（成品文件保留）', 'success')
            load()
          } catch (e) { toast((e as Error).message, 'error') }
          finally { setDeleting(false) }
        }} />
      {promptDialog}
      {/* #428：新建项目弹窗——标题 + 画幅四边形选择 */}
      <Dialog open={createOpen} onOpenChange={(o) => {
        if (o) { setCreateTitle(`项目${Date.now() % 10000}`); setCreateSize({ w: 1080, h: 1920 }); setCreateFitMode('cover') }
        setCreateOpen(o)
      }} title="新建项目" width={520}
        footer={
          <>
            <Button variant="outline" size="sm" onClick={() => setCreateOpen(false)}>取消</Button>
            <Button size="sm" disabled={!createTitle.trim()} onClick={async () => {
              try {
                const t = await creationApi.createProject(createTitle.trim(), createSize.w, createSize.h, createFitMode)
                setCreateOpen(false)
                setEditingId(t.id); setView('edit')
              } catch (e) { toast((e as Error).message, 'error') }
            }}>创建</Button>
          </>
        }>
        <div className="space-y-4">
          <div>
            <div className="mb-1 text-base font-medium">项目标题</div>
            <input type="text" value={createTitle}
              onChange={(e) => setCreateTitle(e.target.value)}
              className="h-10 w-full rounded-md border border-border bg-white px-3 text-base"
              placeholder="请输入项目标题" />
          </div>
          <div>
            <div className="mb-2 text-base font-medium">画幅</div>
            <PreviewSelect value={`${createSize.w}x${createSize.h}`}
              onChange={(v) => {
                const [w, h] = v.split('x').map(Number)
                setCreateSize({ w, h })
              }}
              options={resolutionPresets.map((p) => ({
                value: `${p.w}x${p.h}`,
                label: p.label,
                sub: `${p.platform} · ${p.w}×${p.h}`,
                preview: resolutionPreview(p.w, p.h),
              }))} />
          </div>
          <div>
            <div className="mb-2 text-base font-medium">画面模式</div>
            <PreviewSelect value={createFitMode} onChange={setCreateFitMode}
              options={([
                ['cover', '铺满裁剪', '默认，无黑边'],
                ['contain', '居中补黑', '全内容可见'],
                ['fill', '拉伸', '可能变形'],
                ['blur_bg', '模糊背景', '高端风格'],
              ] as const).map(([k, label, tip]) => ({
                value: k,
                label,
                sub: tip,
                preview: fitModePreview(k),
              }))} />
          </div>
        </div>
      </Dialog>
    </div>
  )
}
