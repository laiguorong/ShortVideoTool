import { useCallback, useEffect, useRef, useState } from 'react'
import { materialApi } from '@/api/material'
import { selectionApi } from '@/api/selection'
import { STATUS_LABEL } from '@/components/shared/StatusBadge'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Pagination } from '@/components/shared/Pagination'

// 「未分类」虚拟分类 ID（与后端 UNCATEGORIZED_ID 保持一致）
const UNCATEGORIZED_ID = '-'

/** 任务类型 */
type TaskType = 'share' | 'upload' | 'pull' | 'shop_pull'

/** 任务主记录（分享导入 / 本地上传共用字段） */
interface TaskRecord {
  id: string
  category_id: string
  category_name?: string | null
  status: string
  total: number
  success_count: number
  failed_count: number
  message: string
  create_time: string
}

/** 任务明细项 */
interface TaskItemRecord {
  id: string
  status: string
  message: string
  retry_count: number
  material_id: string | null
  share_text?: string
  file_name?: string
  file_path?: string
}

/** 视频拉取执行日志 */
interface PullLogRecord {
  id: string
  task_id?: string         // 关联的视频拉取任务 ID（批量展示页有，单任务页可空）
  task_name?: string | null  // 关联任务名（被删时为 null）
  run_time: string
  new_count: number
  skip_count: number
  fail_reason: string | null
}

/** 格式化时间 */
function fmtTime(s: string) {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
}

// #443：失败原因截断到 20 个字符（按 code point 数，emoji/中英文都按 1 个字符算），
// 保留 title 悬浮看全文。
function truncateMsg(s: string, maxChars = 20): string {
  if (!s) return '-'
  const arr = Array.from(s)
  return arr.length > maxChars ? arr.slice(0, maxChars).join('') + '…' : s
}

export function TasksPage() {
  const [tab, setTab] = useState<TaskType>('share')
  const [shareTasks, setShareTasks] = useState<{ list: TaskRecord[]; total: number }>({ list: [], total: 0 })
  const [uploadTasks, setUploadTasks] = useState<{ list: TaskRecord[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [detail, setDetail] = useState<{ type: Exclude<TaskType, 'pull'>; task: TaskRecord } | null>(null)
  const [detailItems, setDetailItems] = useState<TaskItemRecord[]>([])
  const [detailLoading, setDetailLoading] = useState(false)
  // 拉取记录聚合列表（所有任务的执行日志，按时间倒序）
  const [allPullLogs, setAllPullLogs] = useState<{ list: PullLogRecord[]; total: number }>({ list: [], total: 0 })
  // #351：批量删除勾选（按当前 tab 切分；share/upload 任务主记录 id 与 pull 日志 id 命名空间互不冲突）
  const [shareChecked, setShareChecked] = useState<Set<string>>(new Set())
  const [uploadChecked, setUploadChecked] = useState<Set<string>>(new Set())
  const [pullChecked, setPullChecked] = useState<Set<string>>(new Set())
  // 任务 #408：门店拉取记录刷新计数（批量删除后 +1 触发 ShopPullLogsTab 重载）
  const [shopPullRefresh, setShopPullRefresh] = useState(0)
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  // #354：批量删除进行中（列表中心显示遮罩 + 提示）
  const [deleting, setDeleting] = useState(false)
  const [deletingCount, setDeletingCount] = useState(0)

  /** P0-1：请求序号 ref + 陈旧响应守卫，避免连续刷新时旧请求的 finally 提前关 loading */
  const reqIdRef = useRef(0)

  /** 按当前 tab 拉取数据（不含 setLoading，由调用方控制以避免 React 18 自动批处理吞掉 loading 态） */
  const load = useCallback(async () => {
    const myId = ++reqIdRef.current
    try {
      if (tab === 'share') {
        const shareRes = await materialApi.listShareImportTasks(page, pageSize)
        if (myId !== reqIdRef.current) return  // 陈旧响应丢弃
        setShareTasks(shareRes)
      } else if (tab === 'upload') {
        const uploadRes = await materialApi.listUploadTasks(page, pageSize)
        if (myId !== reqIdRef.current) return
        setUploadTasks(uploadRes)
      } else if (tab === 'pull') {
        const r = await materialApi.listAllPullLogs(page, pageSize)
        if (myId !== reqIdRef.current) return
        setAllPullLogs(r)
      }
    } catch (e) {
      // P0-2：toast 错误而非静默
      const msg = (e as { message?: string })?.message ?? '加载失败'
      toast(`加载任务列表失败：${msg}`, 'error')
    } finally {
      if (myId === reqIdRef.current) setLoading(false)
    }
  }, [tab, page, pageSize])

  /** 按钮点击触发：同步 setLoading(true) 后再调 load，避免 React 18 把 true/false 合并成一次渲染 */
  const doRefresh = () => {
    setLoading(true)
    void load()
  }

  /** 加载明细弹窗数据 */
  const loadDetail = async (type: Exclude<TaskType, 'pull'>, task: TaskRecord) => {
    setDetail({ type, task })
    setDetailLoading(true)
    try {
      const res =
        type === 'share'
          ? await materialApi.getShareImportTask(task.id)
          : await materialApi.getUploadTask(task.id)
      setDetailItems(res.items)
    } catch (e) {
      const msg = (e as { message?: string })?.message ?? '未知错误'
      toast(`加载明细失败：${msg}`, 'error')
    } finally {
      setDetailLoading(false)
    }
  }

  /** 重试全部失败项 */
  const retryAll = async (type: Exclude<TaskType, 'pull'>, taskId: string) => {
    try {
      if (type === 'share') {
        await materialApi.retryShareImportTask(taskId)
      } else {
        await materialApi.retryUploadTask(taskId)
      }
      toast('已重新加入队列', 'success')
      await load()
      if (detail?.task.id === taskId) {
        await loadDetail(type, detail.task)
      }
    } catch (e) {
      const msg = (e as { message?: string })?.message ?? '未知错误'
      toast(`重试失败：${msg}`, 'error')
    }
  }

  /** 重试单条失败项 */
  const retryOne = async (type: Exclude<TaskType, 'pull'>, taskId: string, itemId: string) => {
    try {
      if (type === 'share') {
        await materialApi.retryShareImportItem(taskId, itemId)
      } else {
        await materialApi.retryUploadItem(taskId, itemId)
      }
      toast('已重新加入队列', 'success')
      await load()
      if (detail?.task.id === taskId) {
        await loadDetail(type, detail.task)
      }
    } catch (e) {
      const msg = (e as { message?: string })?.message ?? '未知错误'
      toast(`重试失败：${msg}`, 'error')
    }
  }

  /**
   * #351：批量删除（按当前 tab 类型决定删哪类记录，并发调用单条 API 统计成功/失败）
   * - share: 分享导入任务
   * - upload: 本地上传任务
   * - pull: 素材拉取执行日志
   * - shop_pull: 门店拉取执行日志（任务 #408：复用单条删除接口）
   */
  const doBatchDelete = async () => {
    const ids = tab === 'share' ? [...shareChecked]
      : tab === 'upload' ? [...uploadChecked]
      : tab === 'pull' ? [...pullChecked]
      : tab === 'shop_pull' ? [...pullChecked]
      : []
    if (ids.length === 0) return
    const setter = tab === 'share' ? setShareChecked
      : tab === 'upload' ? setUploadChecked
      : setPullChecked
    setBatchDeleteOpen(false)
    setDeleting(true)
    setDeletingCount(ids.length)
    try {
      // shop_pull 走批量接口（一条请求删完所有），其余走并发单条
      if (tab === 'shop_pull') {
        const r = await selectionApi.batchDeleteLogs(ids)
        toast(`已删除 ${r.deleted} 条`, 'success')
        setter(new Set())
        // shop_pull 列表是组件内部 load，通过 bump shopPullRefresh 触发
        setShopPullRefresh((t) => t + 1)
        setDeleting(false)
        return
      }
      const results = await Promise.allSettled(ids.map((id) =>
        tab === 'share' ? materialApi.deleteShareImportTask(id)
        : tab === 'upload' ? materialApi.deleteUploadTask(id)
        : materialApi.deletePullLog(id),
      ))
      const ok = results.filter((r) => r.status === 'fulfilled').length
      const fail = results.length - ok
      if (ok > 0) toast(`已删除 ${ok} 条${fail > 0 ? `，${fail} 条失败` : ''}`, ok === results.length ? 'success' : 'info')
      else toast('删除失败', 'error')
      setter(new Set())
      await load()
      // 关闭明细弹窗（如果打开的恰好是已被删除的任务）
      if (detail && (tab === 'share' || tab === 'upload') && ids.includes(detail.task.id)) {
        setDetail(null)
      }
    } finally {
      setDeleting(false)
      setDeletingCount(0)
    }
  }

  /** 当前 tab 勾选数（顶部按钮 disabled 与已选展示用） */
  const currentCheckedSize = tab === 'share' ? shareChecked.size
    : tab === 'upload' ? uploadChecked.size
    : tab === 'pull' || tab === 'shop_pull' ? pullChecked.size
    : 0
  /** 当前 tab 已选 Set / setter（表格渲染用） */
  const currentChecked = tab === 'share' ? shareChecked
    : tab === 'upload' ? uploadChecked
    : tab === 'pull' || tab === 'shop_pull' ? pullChecked
    : (new Set() as Set<string>)
  const setCurrentChecked = tab === 'share' ? setShareChecked
    : tab === 'upload' ? setUploadChecked
    : tab === 'pull' ? setPullChecked
    : (_: Set<string>) => {}

  /** 加载视频拉取执行日志（拉取记录弹窗用；保留以备扩展） */
  // 已迁移到聚合列表：拉取记录 tab 直接展示所有任务的执行日志，无需单独弹窗

  // 单一 effect 仅挂载 load（与 MaterialsPage 风格一致；分类树已迁走，无须再加载 categories）
  useEffect(() => {
    doRefresh()
  }, [load])

  const currentTasks = tab === 'share' ? shareTasks : tab === 'upload' ? uploadTasks : null

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="flex h-8 items-center text-lg font-semibold">任务执行记录</h1>
        {/* 类型页签（样式与素材库一致：标题右侧文字页签） */}
        <div className="ml-2 flex gap-3 text-sm">
          {[
            { key: 'share', label: '分享链接导入' },
            { key: 'upload', label: '本地上传' },
            { key: 'pull', label: '素材拉取记录' },
            { key: 'shop_pull', label: '门店拉取记录' },
          ].map((t) => (
            <button
              key={t.key}
              onClick={() => { setTab(t.key as TaskType); setPage(1); setShareChecked(new Set()); setUploadChecked(new Set()); setPullChecked(new Set()) }}
              className={tab === t.key ? 'font-medium text-accent' : 'text-muted-foreground hover:text-foreground'}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* R321：创建拉取任务入口已迁至素材库-任务页签，这里仅作辅助查看 */}
        <Button size="sm" variant="outline" onClick={doRefresh} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </Button>
        {/* #351：四页签统一批量删除按钮（置右上） */}
        <div className="ml-auto flex items-center gap-2">
          {currentCheckedSize > 0 && (
            <span className="text-sm text-muted-foreground">已选 {currentCheckedSize} 项</span>
          )}
          <Button size="sm" variant="outline" className="!text-danger" disabled={currentCheckedSize === 0}
            onClick={() => setBatchDeleteOpen(true)}>删除</Button>
        </div>
      </div>

      {/* 任务 #407：门店拉取记录页签（#408 补批量删除） */}
      {tab === 'shop_pull' && (
        <ShopPullLogsTab
          page={page} pageSize={pageSize} onPageChange={setPage}
          refreshKey={shopPullRefresh}
          checked={pullChecked} onCheckedChange={setPullChecked}
        />
      )}

      {/* 分享 / 上传 任务列表（#358：无数据时也保留表头 + 翻页器，列表内显示"暂无"） */}
      {currentTasks && (
        <div className="flex min-h-0 flex-1 flex-col">
          {/* #354：批量删除遮罩父容器（relative 让覆盖层绝对定位居中） */}
          <div className="relative min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
              {deleting && (
                /* #354：列表中心显示"正在删除…"（半透明遮罩 + spinner + 文字） */
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/60">
                  <div className="flex items-center gap-2 rounded-md bg-white/90 px-4 py-2 text-sm text-muted-foreground shadow">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
                    <span>正在删除 {deletingCount} 条…</span>
                  </div>
                </div>
              )}
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    {/* #351：列头多选（全选/取消全选当前页） */}
                    <th className="w-10 px-4 py-2.5">
                      <input type="checkbox" className="h-4 w-4 accent-blue-600"
                        checked={currentTasks.list.length > 0 && currentTasks.list.every((t) => currentChecked.has(t.id))}
                        onChange={(e) => {
                          if (e.target.checked) setCurrentChecked(new Set(currentTasks.list.map((t) => t.id)))
                          else setCurrentChecked(new Set())
                        }} />
                    </th>
                    <th className="px-4 py-2.5 font-medium">创建时间</th>
                    <th className="px-4 py-2.5 font-medium">目标分类</th>
                    <th className="w-24 px-4 py-2.5 font-medium">状态</th>
                    <th className="w-52 px-4 py-2.5 font-medium">进度</th>
                    <th className="w-48 px-4 py-2.5 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {currentTasks.list.map((task) => (
                    <tr key={task.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                      {/* #351：行首多选框 */}
                      <td className="px-4 py-2.5">
                        <input type="checkbox" className="h-4 w-4 accent-blue-600"
                          checked={currentChecked.has(task.id)}
                          onChange={(e) => {
                            const next = new Set(currentChecked)
                            if (e.target.checked) next.add(task.id)
                            else next.delete(task.id)
                            setCurrentChecked(next)
                          }} />
                      </td>
                      <td className="px-4 py-2.5 text-xs text-muted-foreground">{fmtTime(task.create_time)}</td>
                      <td className="px-4 py-2.5">
                          {task.category_name
                            || (task.category_id === UNCATEGORIZED_ID ? '未分类' : '-')}
                        </td>
                      <td className="px-4 py-2.5"><StatusBadge status={task.status} /></td>
                      <td className="px-4 py-2.5 text-xs text-muted-foreground">
                          成功 {task.success_count} / <span className={task.failed_count > 0 ? 'text-danger font-medium' : ''}>失败</span> <span className={task.failed_count > 0 ? 'text-danger font-medium' : ''}>{task.failed_count}</span> / 总计 {task.total}
                        </td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1">
                          <Button size="sm" variant="outline" onClick={() => loadDetail(tab as Exclude<TaskType, 'pull'>, task)}>
                            记录
                          </Button>
                          {(task.status === 'completed' || task.status === 'failed') && task.failed_count > 0 && (
                            <Button size="sm" variant="outline" onClick={() => retryAll(tab as Exclude<TaskType, 'pull'>, task.id)}>
                              重试
                            </Button>
                          )}
                          {/* #355：行内删除按钮去掉，统一走顶部批量删除 */}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {/* #358：无数据时占位行（保留表头 + 翻页器） */}
                  {currentTasks.list.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-sm text-muted-foreground">
                        暂无{tab === 'share' ? '分享导入' : '本地上传'}任务
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {/* 翻页器固定底部（表格区独立滚动） */}
            <div className="px-4">
              <Pagination page={page} pageSize={pageSize} total={currentTasks.total} onChange={setPage}
                onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} />
            </div>
          </div>
      )}

      {/* 拉取记录列表（所有任务的执行记录聚合；#358：无数据也保留表头 + 翻页器） */}
      {tab === 'pull' && (
        <div className="flex min-h-0 flex-1 flex-col">
          {/* #354：批量删除遮罩父容器（relative 让覆盖层绝对定位居中） */}
          <div className="relative min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
              {deleting && (
                /* #354：列表中心显示"正在删除…"（半透明遮罩 + spinner + 文字） */
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/60">
                  <div className="flex items-center gap-2 rounded-md bg-white/90 px-4 py-2 text-sm text-muted-foreground shadow">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
                    <span>正在删除 {deletingCount} 条…</span>
                  </div>
                </div>
              )}
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    {/* #351：列头多选 */}
                    <th className="w-10 px-4 py-2.5">
                      <input type="checkbox" className="h-4 w-4 accent-blue-600"
                        checked={allPullLogs.list.length > 0 && allPullLogs.list.every((l) => currentChecked.has(l.id))}
                        onChange={(e) => {
                          if (e.target.checked) setCurrentChecked(new Set(allPullLogs.list.map((l) => l.id)))
                          else setCurrentChecked(new Set())
                        }} />
                    </th>
                    <th className="px-4 py-2.5 font-medium">执行时间</th>
                    <th className="px-4 py-2.5 font-medium">任务名</th>
                    <th className="w-20 px-4 py-2.5 font-medium">入库</th>
                    <th className="w-20 px-4 py-2.5 font-medium">拦截</th>
                    <th className="px-4 py-2.5 font-medium">结果</th>
                  </tr>
                </thead>
                <tbody>
                  {allPullLogs.list.map((log) => (
                    <tr key={log.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                      {/* #351：行首多选框 */}
                      <td className="px-4 py-2.5">
                        <input type="checkbox" className="h-4 w-4 accent-blue-600"
                          checked={currentChecked.has(log.id)}
                          onChange={(e) => {
                            const next = new Set(currentChecked)
                            if (e.target.checked) next.add(log.id)
                            else next.delete(log.id)
                            setCurrentChecked(next)
                          }} />
                      </td>
                      <td className="px-4 py-2.5 text-xs text-muted-foreground">{fmtTime(log.run_time)}</td>
                      <td className="px-4 py-2.5">
                        {log.task_name || '-'}
                      </td>
                      <td className="px-4 py-2.5">{log.new_count}</td>
                      <td className="px-4 py-2.5">{log.skip_count}</td>
                      <td className={`px-4 py-2.5 ${(log.fail_reason || '完成') !== '完成' ? 'text-danger' : 'text-muted-foreground'}`}>
                        {log.fail_reason || '完成'}
                      </td>
                    </tr>
                  ))}
                  {/* #358：无数据时占位行（保留表头 + 翻页器） */}
                  {allPullLogs.list.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-sm text-muted-foreground">
                        暂无素材拉取记录
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="px-4">
              <Pagination page={page} pageSize={pageSize} total={allPullLogs.total} onChange={setPage}
                onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} />
            </div>
          </div>
      )}

      {/* 记录明细弹窗 */}
      <Dialog
        open={!!detail}
        onOpenChange={(open) => !open && setDetail(null)}
        title={`${detail?.type === 'share' ? '分享导入' : '本地上传'}记录`}
        width={900}
        height={520}
        footer={
          detail && (
            <>
              <Button size="sm" variant="outline" onClick={() => setDetail(null)}>
                关闭
              </Button>
              {(detail.task.status === 'completed' || detail.task.status === 'failed') &&
                detail.task.failed_count > 0 && (
                  <Button size="sm" variant="primary" onClick={() => retryAll(detail.type, detail.task.id)}>
                    重试全部失败项
                  </Button>
                )}
            </>
          )
        }
      >
        {detailLoading ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">加载中…</div>
        ) : detailItems.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">暂无记录</div>
        ) : (
          <div className="flex h-full flex-col">
            {/* 表格区滚动（内容统一左对齐；撑满弹窗内容区） */}
            <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    {/* 分享文本/文件名列自适应宽度；其余列固定宽度 */}
                    <th className="px-4 py-2.5 font-medium">{detail?.type === 'share' ? '分享文本' : '文件名'}</th>
                    <th className="w-20 px-4 py-2.5 font-medium">状态</th>
                    <th className="w-64 px-4 py-2.5 font-medium">失败原因 / 消息</th>
                    <th className="w-20 px-4 py-2.5 font-medium">重试次数</th>
                    <th className="w-24 px-4 py-2.5 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {detailItems.map((item) => (
                    <tr key={item.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                      <td className="max-w-0 truncate px-4 py-2.5"
                        title={detail?.type === 'share' ? item.share_text : item.file_path}>
                        {detail?.type === 'share' ? item.share_text : item.file_name}
                      </td>
                      <td className="px-4 py-2.5">
                        <StatusBadge status={item.status} />
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">
                        {/* 消息与状态默认文案相同或为空时不显示（避免与状态徽章冗余） */}
                        {!item.message || STATUS_LABEL[item.status] === item.message ? (
                          <span className="text-xs">--</span>
                        ) : (
                          <span className="block truncate" title={item.message}>{truncateMsg(item.message)}</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5">{item.retry_count}</td>
                      <td className="px-4 py-2.5">
                        {item.status === 'failed' && !item.message?.startsWith('已存在') ? (
                          <Button size="sm" variant="outline" onClick={() => retryOne(detail!.type, detail!.task.id, item.id)}>
                            重试
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">--</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Dialog>

      {/* #351：批量删除确认（按当前 tab 区分记录类型） */}
      <ConfirmDialog
        open={batchDeleteOpen}
        onOpenChange={setBatchDeleteOpen}
        title="批量删除"
        danger
        content={`确定删除已选 ${currentCheckedSize} 条${
          tab === 'share' ? '分享导入任务'
          : tab === 'upload' ? '本地上传任务'
          : tab === 'pull' ? '素材拉取执行日志'
          : '门店拉取执行日志'
        }？已入库素材保留。`}
        onConfirm={doBatchDelete}
      />
    </div>
  )
}


/** ============ 门店拉取记录页签（任务 #407 + #408 批量删除）============ */

function ShopPullLogsTab({
  page, pageSize, onPageChange,
  refreshKey, checked, onCheckedChange,
}: {
  page: number
  pageSize: number
  onPageChange: (p: number) => void
  /** 父级控制刷新（批量删除后 +1） */
  refreshKey: number
  /** 多选状态：复用父级 pullChecked，与素材页签共享批量删除按钮 */
  checked: Set<string>
  onCheckedChange: (next: Set<string>) => void
}) {
  const [data, setData] = useState<{ list: (import('@/api/selection').ShopPullLog & { task_name: string | null })[]; total: number }>({ list: [], total: 0 })
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    selectionApi.allLogs(page, pageSize)
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Tasks] 请求失败:', e))
      .finally(() => setLoading(false))
  }, [page, pageSize])

  useEffect(load, [load])
  // refreshKey 变化时强制重载
  useEffect(load, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // 切页 / 数据变化时清理已选（避免跨页残留）
  useEffect(() => {
    if (checked.size === 0) return
    const visible = new Set(data.list.map((l) => l.id))
    const next = new Set<string>()
    for (const id of checked) if (visible.has(id)) next.add(id)
    if (next.size !== checked.size) onCheckedChange(next)
  }, [data.list]) // eslint-disable-line react-hooks/exhaustive-deps

  const allChecked = data.list.length > 0 && data.list.every((l) => checked.has(l.id))

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="w-10 px-4 py-2.5">
                <input type="checkbox" className="h-4 w-4 accent-blue-600"
                  checked={allChecked}
                  onChange={(e) => {
                    const next = new Set(checked)
                    if (e.target.checked) for (const l of data.list) next.add(l.id)
                    else for (const l of data.list) next.delete(l.id)
                    onCheckedChange(next)
                  }} />
              </th>
              <th className="px-4 py-2.5 font-medium">执行时间</th>
              <th className="px-4 py-2.5 font-medium">任务名</th>
              <th className="w-20 px-4 py-2.5 text-right font-medium">页数</th>
              <th className="w-20 px-4 py-2.5 text-right font-medium">新增</th>
              <th className="w-20 px-4 py-2.5 text-right font-medium">更新</th>
              <th className="px-4 py-2.5 font-medium">失败原因</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((l) => (
              <tr key={l.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                <td className="px-4 py-2.5">
                  <input type="checkbox" className="h-4 w-4 accent-blue-600"
                    checked={checked.has(l.id)}
                    onChange={(e) => {
                      const next = new Set(checked)
                      if (e.target.checked) next.add(l.id)
                      else next.delete(l.id)
                      onCheckedChange(next)
                    }} />
                </td>
                <td className="px-4 py-2.5 text-xs">{fmtTime(l.run_time)}</td>
                <td className="px-4 py-2.5">{l.task_name || <span className="text-muted-foreground">已删除任务</span>}</td>
                <td className="px-4 py-2.5 text-right">{l.pages_done}</td>
                <td className="px-4 py-2.5 text-right text-ok">{l.new_count}</td>
                <td className="px-4 py-2.5 text-right">{l.update_count}</td>
                <td className="px-4 py-2.5 text-xs text-danger">{l.fail_reason || '-'}</td>
              </tr>
            ))}
            {data.list.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-12 text-center text-muted-foreground">
                {loading ? '加载中…' : '暂无门店拉取记录'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={onPageChange} /></div>
    </div>
  )
}
