/** #414：项目门店绑定右侧抽屉。
 * - 顶部「查找门店」输入框：输入名称 + 回车 → 调后端 listShops(keyword=..., page=1) 远程搜索
 *   默认每页 20 条、按城市排序；底部「加载更多门店」按钮翻页
 * - 下部「已绑定门店」列表（按 sort_order）：每项右侧「× 移除」按钮
 * - 每项操作独立发请求，无批量保存按钮
 * - 取消最后一家（移除后只剩 0 条）弹 confirm 二次确认
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { selectionApi, type Shop } from '@/api/selection'
import { publishIntroApi, type ProjectShopItem } from '@/api/publish_intro'

interface Props {
  project: { id: string; title: string } | null
  open: boolean
  onOpenChange: (v: boolean) => void
}

/** 查找的每页大小（与后端默认 20 对齐） */
const SEARCH_PAGE_SIZE = 20

export function ProjectShopsDrawer({ project, open, onOpenChange }: Props) {
  /** 远程搜索结果（按 page 累计追加） */
  const [searchList, setSearchList] = useState<Shop[]>([])
  /** 当前搜索的 keyword（用于判断是否还有下一页） */
  const [searchKeyword, setSearchKeyword] = useState('')
  /** 当前搜索已加载到第几页 */
  const [searchPage, setSearchPage] = useState(0)
  /** 总条数（后端返回） */
  const [searchTotal, setSearchTotal] = useState(0)
  /** 当前已绑定门店（按 sort_order） */
  const [bound, setBound] = useState<ProjectShopItem[]>([])
  /** 查找输入框 */
  const [query, setQuery] = useState('')
  /** 单项操作中（添加/移除）的 shop_id */
  const [busyId, setBusyId] = useState<string | null>(null)
  /** 全局操作锁（UI 渲染用）：网络慢时防止并发 add/remove 导致 sort_order 冲突 */
  const [busy, setBusy] = useState(false)
  const [confirmRemoveLast, setConfirmRemoveLast] = useState(false)
  const [pendingRemoveShopId, setPendingRemoveShopId] = useState<string | null>(null)
  /** ref 锁（同步访问）：state 异步更新，连点两次都会读到旧值导致锁失效 */
  const busyRef = useRef(false)
  /** 搜索请求序号：每次新搜索递增，旧请求晚返回时通过序号丢弃避免覆盖 */
  const searchReqId = useRef(0)
  /** 搜索中 / 加载更多中 */
  const [searching, setSearching] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadingBound, setLoadingBound] = useState(false)

  /** 全局操作锁：ref 同步检查 + state 用于 UI 渲染。
   * 网络慢时只允许一个 add/remove 进行中，避免基于「旧 bound」并发提交
   * 导致 sort_order 冲突（后到的请求赢，丢失前者的改动）。 */
  const withLock = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    if (busyRef.current) return undefined
    busyRef.current = true
    setBusy(true)
    try { return await fn() }
    finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  /** 记录上一次拉取的项目 id，避免同项目重开时清空 bound 造成「暂无绑定」闪烁 */
  const lastLoadedProjectIdRef = useRef<string | null>(null)
  /** 全局 unmount / 抽屉关闭守卫：防止异步 setState 触发 React 警告 */
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  // 打开时：拉已绑定门店（搜索结果按回车触发）
  useEffect(() => {
    if (!open || !project) return
    const projectId = project.id
    setQuery('')
    setSearchList([])
    setSearchKeyword('')
    setSearchPage(0)
    setSearchTotal(0)
    // 仅切换到不同项目时才清空 bound，避免同项目重开造成「暂无绑定」闪烁
    if (lastLoadedProjectIdRef.current !== projectId) {
      setBound([])
      lastLoadedProjectIdRef.current = projectId
    }
    setLoadingBound(true)
    let alive = true
    publishIntroApi.listProjectShops(projectId).then((b) => {
      if (alive && aliveRef.current) setBound(b)
    }).catch((e) => {
      if (alive && aliveRef.current) toast((e as Error).message, 'error')
    }).finally(() => { if (alive && aliveRef.current) setLoadingBound(false) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, project?.id])

  const boundIds = useMemo(() => new Set(bound.map((x) => x.shop_id)), [bound])
  /** 是否还有更多结果可加载（按 total 估算，搜索结果未填满总条数时） */
  const hasMore = searchList.length < searchTotal

  /** 触发搜索（回车或点按钮） */
  const triggerSearch = async () => {
    const q = query.trim()
    if (!q) return
    const reqId = ++searchReqId.current
    setSearchKeyword(q)
    setSearching(true)
    try {
      const r = await selectionApi.listShops({
        keyword: q,
        page: 1,
        page_size: SEARCH_PAGE_SIZE,
        sort: 'city',
        is_cps_only: true,
        is_added_only: true,
      })
      // 旧请求晚返回时丢弃，避免覆盖新结果
      if (reqId !== searchReqId.current) return
      setSearchList(r.list)
      setSearchTotal(r.total)
      setSearchPage(r.page)
    } catch (e) {
      if (reqId === searchReqId.current) toast((e as Error).message, 'error')
    } finally {
      if (reqId === searchReqId.current) setSearching(false)
    }
  }

  /** 加载更多（下一页） */
  const loadingMoreRef = useRef(false)
  const loadMore = async () => {
    if (loadingMoreRef.current || !searchKeyword) return
    loadingMoreRef.current = true
    setLoadingMore(true)
    const reqId = searchReqId.current
    const nextPage = searchPage + 1
    try {
      const r = await selectionApi.listShops({
        keyword: searchKeyword,
        page: nextPage,
        page_size: SEARCH_PAGE_SIZE,
        sort: 'city',
        is_cps_only: true,
        is_added_only: true,
      })
      if (reqId !== searchReqId.current) return
      setSearchList((prev) => {
        const existing = new Set(prev.map((x) => x.id))
        return [...prev, ...r.list.filter((x) => !existing.has(x.id))]
      })
      setSearchTotal(r.total)
      setSearchPage(r.page)
    } catch (e) {
      if (reqId === searchReqId.current) toast((e as Error).message, 'error')
    } finally {
      loadingMoreRef.current = false
      if (reqId === searchReqId.current) setLoadingMore(false)
    }
  }

  /** 回车键触发搜索 */
  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      void triggerSearch()
    }
  }

  /** 清空查找输入 + 重置搜索结果。自增序号丢弃在途请求，避免旧结果覆盖 */
  const clearQuery = () => {
    searchReqId.current++
    setQuery('')
    setSearchList([])
    setSearchKeyword('')
    setSearchPage(0)
    setSearchTotal(0)
  }

  /** 添加：append 到末尾 + 发请求（带全局并发锁）。
   *  bound 快照在 lock 内读取，确保基于「最新 bound」提交。
   *  alive 守卫避免抽屉关闭后 setState 触发 React 警告。 */
  const addShop = (shopId: string) => withLock(async () => {
    if (!project) return
    setBusyId(shopId)
    try {
      const next = [...bound.map((x) => x.shop_id), shopId]
      await publishIntroApi.setProjectShops(project.id, next)
      const refreshed = await publishIntroApi.listProjectShops(project.id)
      if (!aliveRef.current) return
      setBound(refreshed)
      toast('已添加', 'success')
    } catch (e) {
      if (aliveRef.current) toast((e as Error).message, 'error')
    } finally {
      if (aliveRef.current) setBusyId(null)
    }
  })

  /** 移除：弹 confirm 在 lock 外做（confirm 阻塞不应持有锁）
   *  弹 confirm 时也设 busy 占位（防 confirm 期间用户连点别的"移除"按钮并发穿透） */
  const removeShop = (shopId: string) => {
    if (busyRef.current) return
    if (bound.length === 1) {
      // 弹 ConfirmDialog（避免最后 1 个门店被静默移除导致后续无法发布）
      setBusyId(shopId)  // 占位 — confirm 关闭时（onConfirm 或 onOpenChange=false）会清
      setPendingRemoveShopId(shopId)
      setConfirmRemoveLast(true)
      return
    }
    doRemove(shopId)
  }

  /** 实际执行移除（被 removeShop / ConfirmDialog onConfirm 复用）
   *  withLock + busy 守卫：先检查再 setBusy，避免与其它 in-flight 移除并发 */
  const doRemove = (shopId: string) => {
    return withLock(async () => {
      if (!project) return
      setBusyId(shopId)
      try {
        const next = bound.filter((x) => x.shop_id !== shopId).map((x) => x.shop_id)
        await publishIntroApi.setProjectShops(project.id, next)
        const refreshed = await publishIntroApi.listProjectShops(project.id)
        if (!aliveRef.current) return
        setBound(refreshed)
        toast('已移除', 'success')
      } catch (e) {
        if (aliveRef.current) toast((e as Error).message, 'error')
      } finally {
        if (aliveRef.current) setBusyId(null)
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} side="right" width={480}
      title={project ? `门店绑定 · ${project.title}` : '门店绑定'}
      footer={
        <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>关闭</Button>
      }>
      {/* ===== 顶部：查找门店 ===== */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="font-medium text-foreground">查找门店</span>
          <span className="text-muted-foreground">
            {loadingBound ? '加载已绑…' :
              searching ? '搜索中…' :
              searchKeyword ? `共 ${searchTotal} 家，已显示 ${searchList.length}` : ''}
          </span>
        </div>
        <div className="flex gap-2">
          <div className="relative flex-1">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入门店名称 + 回车…"
              className="w-full rounded-md border border-border bg-white px-2 py-1.5 pr-8 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
            {/* 清空图标：仅当输入框有内容时显示 */}
            {query && (
              <button
                type="button"
                onClick={clearQuery}
                title="清空"
                aria-label="清空查找内容"
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            )}
          </div>
          <Button size="sm" onClick={triggerSearch} disabled={searching || !query.trim()}>
            查找
          </Button>
        </div>
        {searching ? (
          <div className="text-xs text-muted-foreground">搜索中…</div>
        ) : !searchKeyword ? (
          <div className="text-xs text-muted-foreground">
            输入门店名称 + 回车 / 点「查找」，按城市排序，每页 20 条
          </div>
        ) : searchList.length === 0 ? (
          <div className="rounded border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
            没有匹配「{searchKeyword}」的门店
          </div>
        ) : (
          <div className="space-y-2">
            <div className="max-h-[40vh] min-h-0 space-y-1 overflow-auto rounded border border-border p-2">
              {searchList.map((s) => {
                const isBound = boundIds.has(s.id)
                return (
                  <div key={s.id}
                    className={cn(
                      'flex items-center gap-2 rounded px-1 py-1 text-sm',
                      isBound && 'pointer-events-none bg-muted/40 text-muted-foreground',
                      !isBound && 'hover:bg-muted/40',
                    )}>
                    <span className="flex-1 truncate">{s.name}</span>
                    {s.city && <span className="text-xs text-muted-foreground">{s.city}</span>}
                    <Button size="sm" variant="outline"
                      disabled={busy || busyId === s.id || isBound}
                      onClick={() => addShop(s.id)}>
                      {isBound ? '已绑定' : busyId === s.id ? '…' : '+ 添加'}
                    </Button>
                  </div>
                )
              })}
              {/* 「加载更多」按钮：作为列表最后一项，与列表项顶部加细线分隔 */}
              {hasMore && (
                <>
                  <div className="my-1 border-t border-border" />
                  <button type="button" disabled={loadingMore} onClick={loadMore}
                    className="flex w-full items-center justify-center gap-2 rounded px-1 py-2 text-sm font-medium text-accent hover:bg-muted/40 disabled:opacity-50">
                    {loadingMore ? '加载中…' : `加载更多门店（还有 ${searchTotal - searchList.length} 家）`}
                  </button>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ===== 下：已绑定门店 ===== */}
      <div className="mt-4 space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="font-medium text-foreground">已绑定门店</span>
          <span className="text-muted-foreground">共 {bound.length} 家</span>
        </div>
        {bound.length === 0 ? (
          <div className="rounded border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
            暂无绑定，请在上方查找并添加
          </div>
        ) : (
          <div className="max-h-[40vh] min-h-0 space-y-1 overflow-auto rounded border border-border p-2">
            {bound.map((b, idx) => {
              const id = b.shop_id
              return (
                <div key={id}
                  className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-muted/40">
                  <span
                    style={{
                      display: 'inline-block',
                      minWidth: 28,
                      padding: '2px 6px',
                      borderRadius: 4,
                      background: '#2563eb',
                      color: '#fff',
                      fontSize: 12,
                      fontWeight: 600,
                      textAlign: 'center',
                      lineHeight: '16px',
                    }}
                  >
                    #{idx + 1}
                  </span>
                  <span className="flex-1 truncate">{b.shop_name ?? '(门店已删除)'}</span>
                  {b.city && <span className="text-xs text-muted-foreground">{b.city}</span>}
                  <Button size="sm" variant="ghost" disabled={busy || busyId === id}
                    onClick={() => removeShop(id)}
                    className="text-red-600 hover:bg-red-50 hover:text-red-700">
                    {busyId === id ? '…' : '× 移除'}
                  </Button>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 最后 1 个门店移除确认（避免静默移除导致后续无法发布）
        - onOpenChange 关闭时清 pending ID（避免下次弹 confirm 看到 stale 值）
        - onConfirm 真正执行前再做一次 bound.find 校验（防 bound 已被并发改动） */}
      <ConfirmDialog
        open={confirmRemoveLast}
        onOpenChange={(v) => { setConfirmRemoveLast(v); if (!v) setPendingRemoveShopId(null) }}
        title="移除最后 1 个门店" danger
        content="当前项目仅剩最后 1 个门店绑定，移除后将无法发布，确认？"
        onConfirm={() => {
          const id = pendingRemoveShopId
          setConfirmRemoveLast(false)
          setPendingRemoveShopId(null)
          // bound 已变（含并发删过）则跳过 — 用户看到 UI 与预期不符会重试
          if (id && bound.some((x) => x.shop_id === id)) doRemove(id)
          else toast('门店列表已变更，请刷新重试', 'info')
        }} />
    </Dialog>
  )
}
