import { useCallback, useEffect, useRef, useState } from 'react'
import { materialApi, type Category, type VideoPullTask } from '@/api/material'
import { accountApi, type AvailableAccount } from '@/api/account'
import type { IntervalConfig } from '@/api/selection'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { Pagination } from '@/components/shared/Pagination'
import { StatusBadge, STATUS_LABEL } from '@/components/shared/StatusBadge'
import { toast } from '@/components/ui/toast'

/** 账号状态码 → 中文（与选品页一致，复用 STATUS_LABEL 单一来源） */
const accountStatusLabel = (status: string): string => STATUS_LABEL[status] || status

// 系统固定"未分类"分类 ID（虚拟节点，后端 list API 注入）
const UNCATEGORIZED_ID = '-'

/** 表单字段容器：定义在模块顶部（不在组件函数体内）以避免每次 render 时引用变化，
 *  防止 React 按组件 type 不匹配而卸载 children（含 input），导致输入框焦点丢失。 */
function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-sm font-medium">{label}</label>
      {children}
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

/** 分类树节点（前端组装，含完整路径） */
interface TreeNode extends Category {
  children: TreeNode[]
  path: string
}

/** 平铺分类组树 */
function buildTree(rows: Category[]): TreeNode[] {
  const map = new Map<string, TreeNode>()
  rows.forEach((r) => map.set(r.id, { ...r, children: [], path: r.name }))
  const roots: TreeNode[] = []
  map.forEach((node) => {
    if (node.parent_id && map.has(node.parent_id)) {
      const parent = map.get(node.parent_id)!
      parent.children.push(node)
      node.path = `${parent.path}/${node.name}`
    } else {
      roots.push(node)
    }
  })
  return roots
}

/** 树转平铺（下拉选择用，带层级前缀） */
function flattenTree(nodes: TreeNode[], depth = 0): { node: TreeNode; depth: number }[] {
  const out: { node: TreeNode; depth: number }[] = []
  nodes.forEach((n) => {
    out.push({ node: n, depth })
    out.push(...flattenTree(n.children, depth + 1))
  })
  return out
}

/** 创建/编辑视频拉取任务弹窗 */
export function PullTaskDialog({ open, onOpenChange, categories, editTarget, onDone }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  categories: Category[]
  editTarget: VideoPullTask | null
  onDone: () => void
}) {
  const [accounts, setAccounts] = useState<AvailableAccount[]>([])
  const [name, setName] = useState('')
  const [accountId, setAccountId] = useState('')
  const [categoryId, setCategoryId] = useState('')
  // 拉取条件：搜索词 / 关键词
  const [keyword, setKeyword] = useState('')
  const [titleRegex, setTitleRegex] = useState('')
  const [shopName, setShopName] = useState('')
  const [orientation, setOrientation] = useState('')
  // 排序依据：0=综合 / 1=最多点赞 / 2=最新发布（默认 2）
  const [sortType, setSortType] = useState<0 | 1 | 2>(2)
  // 拉取条件：时间/时长/数量（档位化）
  const [publishRange, setPublishRange] = useState<'any' | '1d' | '7d' | '180d'>('any')
  const [durationRange, setDurationRange] = useState<'any' | 'lt1m' | '1to5m' | 'gt5m'>('any')
  const [maxCount, setMaxCount] = useState(100)
  // 拉取条件：行为开关
  const [fetchBgm, setFetchBgm] = useState(false)
  const [filterSubtitle, setFilterSubtitle] = useState(false)
  const [filterFace, setFilterFace] = useState(false)
  // 调度间隔
  const [intervalType, setIntervalType] = useState<'minute' | 'hour' | 'daily'>('hour')
  const [intervalValue, setIntervalValue] = useState(6)
  const [intervalTime, setIntervalTime] = useState('09:00')
  const [busy, setBusy] = useState(false)

  // v122/#356：守卫仅在 open 一直 true 且 editTarget 引用未变时跳过回填；
  // 加 ref 比较避免父组件轮询期间 useEffect 重跑覆盖用户输入。
  // #356：editTarget 引用变化（同 id 但对象已刷新）强制回填，保证再次打开编辑看到最新数据。
  const lastInitRef = useRef<{ open: boolean; targetId: string | null; ref: VideoPullTask | null | undefined }>(
    { open: false, targetId: null, ref: undefined },
  )
  useEffect(() => {
    if (!open) {
      lastInitRef.current = { open: false, targetId: null, ref: undefined }
      return
    }
    const targetId = editTarget?.id ?? null
    const last = lastInitRef.current
    if (last.open && last.targetId === targetId && last.ref === editTarget) {
      // 弹窗已初始化过且目标引用未变，跳过重复回填
      return
    }
    lastInitRef.current = { open: true, targetId, ref: editTarget }
    accountApi.available().then((r) => {
      setAccounts(r.list)
      setAccountId((prev) => prev || r.list[0]?.id || '')
    }).catch((e) => console.warn('[PullTask] 请求失败:', e))
    if (editTarget) {
      setName(editTarget.task_name)
      setAccountId(editTarget.account_id)
      // 编辑模式下若历史 category_id='-'（防御性，正常不应出现），归位到第一个可选分类
      setCategoryId(editTarget.category_id === UNCATEGORIZED_ID ? '' : editTarget.category_id)
      try {
        const c = JSON.parse(editTarget.conditions_json)
        setKeyword(c.keyword || '')
        setTitleRegex(c.title_regex || '')
        setShopName(c.shop_name || '')
        setOrientation(c.orientation || '')
        // 排序依据：缺省/越界按 2=最新发布兜底（与后端 Pydantic 默认对齐）
        const st = Number(c.sort_type)
        setSortType(st === 0 || st === 1 || st === 2 ? st : 2)
        // 发布时间档位：旧版 publish_after/publish_before 不再读取
        setPublishRange(c.publish_range || 'any')
        setDurationRange(c.duration_range || 'any')
        const mc = Number(c.max_count)
        setMaxCount(Number.isFinite(mc) && mc > 0 ? Math.min(1000, Math.max(1, mc)) : 100)
        setFetchBgm(!!c.fetch_bgm)  // 缺省 false，v121 改为按需勾选
        setFilterSubtitle(!!c.filter_subtitle)
        setFilterFace(!!c.filter_face)
      } catch { /* ignore */ }
      try {
        const iv = JSON.parse(editTarget.interval_config)
        setIntervalType(iv.type || 'hour')
        setIntervalValue(iv.value || 6)
        setIntervalTime(iv.time || '09:00')
      } catch { /* ignore */ }
    } else {
      // 新建任务：所有字段设默认值
      setName(''); setKeyword(''); setTitleRegex('')
      setShopName(''); setOrientation('')
      setSortType(2)
      setPublishRange('any'); setDurationRange('any')
      setMaxCount(100)
      setFetchBgm(false); setFilterSubtitle(false); setFilterFace(false)
      setIntervalType('hour'); setIntervalValue(6); setIntervalTime('09:00')
    }
  }, [open, editTarget])

  useEffect(() => {
    // 默认选第一个分类（含"未分类"虚拟分类作为合法入库目标）
    const selectable = categories
    if (open && !categoryId && selectable[0]) setCategoryId(selectable[0].id)
  }, [open, categories, categoryId])

  const submit = async () => {
    if (!name.trim() || !accountId || !categoryId) {
      toast('请填写任务名、账号与入库分类', 'error')
      return
    }
    if (intervalType === 'minute' && intervalValue < 10) {
      toast('最小间隔 10 分钟', 'error')
      return
    }
    if (maxCount < 1 || maxCount > 1000) {
      toast('最大数量需在 1-1000 之间', 'error')
      return
    }
    setBusy(true)
    const interval: IntervalConfig = intervalType === 'daily'
      ? { type: 'daily', time: intervalTime }
      : { type: intervalType, value: intervalValue }
    const body = {
      task_name: name.trim(),
      conditions: {
        keyword: keyword.trim(),
        title_regex: titleRegex.trim(),
        shop_name: shopName.trim(),
        orientation,
        sort_type: sortType,
        publish_range: publishRange,
        duration_range: durationRange,
        max_count: maxCount,
        fetch_bgm: fetchBgm,
        filter_subtitle: filterSubtitle,
        filter_face: filterFace,
      },
      account_id: accountId,
      category_id: categoryId,
      interval,
    }
    try {
      if (editTarget) {
        await materialApi.updatePullTask(editTarget.id, body)
        toast('任务已更新（下一轮生效）', 'success')
      } else {
        await materialApi.createPullTask(body)
        toast('任务已创建并启用', 'success')
      }
      onOpenChange(false)
      onDone()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const inputCls = 'h-9 w-full rounded-md border border-border bg-white px-3 text-sm'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}
      title={editTarget ? '编辑拉取任务' : '创建素材拉取任务'} width={680}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy} onClick={submit}>
            {busy ? '提交中…' : editTarget ? '保存' : '创建并启用'}
          </Button>
        </>
      }>
      <div className="space-y-3.5">
        <div className="grid grid-cols-3 gap-3">
          <Field label="任务名称">
            <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)}
              placeholder="如：火锅探店素材" />
          </Field>
          <Field label="拉取账号">
            <select className={inputCls} value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              <option value="">请选择已登录账号</option>
              {accounts.length === 0 && (
                <option value="" disabled>暂无可用账号（请先在账号管理页登录）</option>
              )}
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.remark || a.nickname}（{accountStatusLabel(a.status)}）
                </option>
              ))}
            </select>
          </Field>
          <Field label="入库分类">
            <select className={inputCls} value={categoryId} onChange={(e) => setCategoryId(e.target.value)}>
              {flattenTree(buildTree(categories.filter((c) => c.type === 'video'))).map(({ node, depth }) => (
                <option key={node.id} value={node.id}>{'　'.repeat(depth)}{node.name}</option>
              ))}
            </select>
          </Field>
        </div>
        <div className="rounded-md border border-border bg-muted/30 p-3 space-y-3">
          <div className="text-sm font-semibold">拉取条件（留空 = 不限）</div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="关键词" hint="平台搜索主关键词">
              <input className={inputCls} value={keyword} onChange={(e) => setKeyword(e.target.value)}
                placeholder="如：火锅探店" />
            </Field>
            <Field label="标题正则" hint="对搜索结果标题二次过滤，如：探店|测评">
              <input className={inputCls} value={titleRegex} onChange={(e) => setTitleRegex(e.target.value)} />
            </Field>
            <Field label="画面方向">
              <select className={inputCls} value={orientation} onChange={(e) => setOrientation(e.target.value)}>
                <option value="">不限</option>
                <option value="vertical">竖屏</option>
                <option value="horizontal">横屏</option>
              </select>
            </Field>
            <Field label="门店名称" hint="如：海底捞">
              <input className={inputCls} value={shopName} onChange={(e) => setShopName(e.target.value)} />
            </Field>
            <Field label="排序依据" hint="抖音搜索结果排序方式">
              <select className={inputCls} value={sortType}
                onChange={(e) => setSortType(Number(e.target.value) as 0 | 1 | 2)}>
                <option value={0}>综合排序</option>
                <option value={1}>最多点赞</option>
                <option value={2}>最新发布</option>
              </select>
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Field label="发布时间">
              <select className={inputCls} value={publishRange} onChange={(e) => setPublishRange(e.target.value as any)}>
                <option value="any">不限</option>
                <option value="1d">一天内</option>
                <option value="7d">一周内</option>
                <option value="180d">半年内</option>
              </select>
            </Field>
            <Field label="视频时长">
              <select className={inputCls} value={durationRange} onChange={(e) => setDurationRange(e.target.value as any)}>
                <option value="any">不限</option>
                <option value="lt1m">1 分钟以下</option>
                <option value="1to5m">1 - 5 分钟</option>
                <option value="gt5m">5 分钟以上</option>
              </select>
            </Field>
            <Field label="最大数量" hint="1-1000；达到上限任务自动停用">
              <input type="number" className={inputCls} value={maxCount}
                min={1} max={1000}
                onChange={(e) => setMaxCount(Math.min(1000, Math.max(1, Number(e.target.value) || 0)))} />
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-3 pt-1">
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" className="h-4 w-4" checked={fetchBgm}
                onChange={(e) => setFetchBgm(e.target.checked)} />
              <span>拉取背景音乐</span>
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" className="h-4 w-4" checked={filterSubtitle}
                onChange={(e) => setFilterSubtitle(e.target.checked)} />
              <span>过滤有字幕</span>
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" className="h-4 w-4" checked={filterFace}
                onChange={(e) => setFilterFace(e.target.checked)} />
              <span>过滤主播人脸</span>
            </label>
          </div>
          {(filterSubtitle || filterFace) && (
            <div className="text-xs text-muted-foreground">
              提示：开启过滤后会下载视频并抽帧检测字幕/人脸，可能显著拉长每条入库耗时；检测依赖缺失时默认放行。
            </div>
          )}
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium">执行时间间隔</label>
          <div className="flex items-center gap-2">
            <select className="h-9 rounded-md border border-border bg-white px-2 text-sm"
              value={intervalType} onChange={(e) => setIntervalType(e.target.value as any)}>
              <option value="minute">每 N 分钟</option>
              <option value="hour">每 N 小时</option>
              <option value="daily">每天固定时刻</option>
            </select>
            {intervalType === 'daily' ? (
              <input type="time" className="h-9 rounded-md border border-border bg-white px-3 text-sm"
                value={intervalTime} onChange={(e) => setIntervalTime(e.target.value)} />
            ) : (
              <input type="number" min={intervalType === 'minute' ? 10 : 1}
                className="h-9 w-24 rounded-md border border-border bg-white px-3 text-sm"
                value={intervalValue}
                onChange={(e) => setIntervalValue(Number(e.target.value))} />
            )}
          </div>
        </div>
      </div>
    </Dialog>
  )
}

/** 拉取任务列表（表格 + 启停/执行/编辑/删除/记录） */
export function PullTaskPanel({ onEdit, onCreated, onShowLogs, refreshTrigger, onLoadingChange }: {
  onEdit: (t: VideoPullTask) => void
  onCreated: () => void
  onShowLogs?: (t: VideoPullTask) => void
  /** 外部触发的刷新信号（递增即重跑 load） */
  refreshTrigger?: number
  /** load 状态变化通知（用于父级按钮 loading 显示） */
  onLoadingChange?: (loading: boolean) => void
}) {
  const [tasks, setTasks] = useState<{ list: VideoPullTask[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [deleteTarget, setDeleteTarget] = useState<VideoPullTask | null>(null)

  const load = useCallback(() => {
    onLoadingChange?.(true)
    return materialApi.pullTasks(page, pageSize)
      .then((r) => setTasks({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[PullTask] 请求失败:', e))
      .finally(() => onLoadingChange?.(false))
  }, [page, pageSize, onLoadingChange])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (refreshTrigger) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTrigger])
  // #任务队列进度可见：每 5 秒轮询拉一次，让 current_run.progress 实时刷新
  // （task_service 内存中的 bg_task 实例进度更新通过 list 接口注入）
  useEffect(() => {
    const timer = setInterval(() => load(), 5000)
    return () => clearInterval(timer)
  }, [load])

  const condSummary = (json: string) => {
    try {
      const c = JSON.parse(json)
      const parts: string[] = []
      if (c.keyword) parts.push(`关键词:${c.keyword}`)
      if (c.title_regex) parts.push(`标题正则:${c.title_regex}`)
      if (c.orientation) parts.push(c.orientation === 'vertical' ? '竖屏' : '横屏')
      if (c.shop_name) parts.push(`门店:${c.shop_name}`)
      // 发布时间档位
      const prMap: Record<string, string> = { any: '不限', '1d': '一天内', '7d': '一周内', '180d': '半年内' }
      const pr = c.publish_range || 'any'
      if (pr !== 'any') parts.push(`发布:${prMap[pr] || pr}`)
      // 时长档位
      const drMap: Record<string, string> = { any: '不限', lt1m: '1分钟内', '1to5m': '1-5分钟', gt5m: '5分钟以上' }
      const dr = c.duration_range || 'any'
      if (dr !== 'any') parts.push(`时长:${drMap[dr] || dr}`)
      // 数量与开关
      const mc = Number(c.max_count)
      if (Number.isFinite(mc) && mc > 0) parts.push(`上限${mc}`)
      if (c.fetch_bgm === false) parts.push('不拉BGM')
      if (c.filter_subtitle) parts.push('过滤字幕')
      if (c.filter_face) parts.push('过滤人脸')
      return parts.join(' · ') || '不限'
    } catch {
      return '-'
    }
  }
  const intervalText = (json: string) => {
    try {
      const cfg = JSON.parse(json)
      return cfg.type === 'daily' ? `每天 ${cfg.time}`
        : `每 ${cfg.value} ${cfg.type === 'minute' ? '分钟' : '小时'}`
    } catch {
      return '-'
    }
  }

  const doRun = async (t: VideoPullTask) => {
    try {
      await materialApi.runPullTask(t.id)
      toast('拉取已开始，可稍后刷新查看入库结果', 'success')
      setTimeout(load, 10000)
      onCreated()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">任务名</th>
              <th className="px-4 py-2.5 font-medium">拉取条件</th>
              <th className="px-4 py-2.5 font-medium">账号</th>
              <th className="px-4 py-2.5 font-medium">入库分类</th>
              <th className="px-4 py-2.5 font-medium">间隔</th>
              <th className="px-4 py-2.5 font-medium">状态</th>
              <th className="px-4 py-2.5 font-medium">最近执行</th>
              <th className="px-4 py-2.5 font-medium">下次执行</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.list.map((t) => (
              <tr key={t.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                <td className="px-4 py-2.5 font-medium">{t.task_name}</td>
                <td className="max-w-52 truncate px-4 py-2.5 text-xs text-muted-foreground"
                  title={condSummary(t.conditions_json)}>
                  {condSummary(t.conditions_json)}
                </td>
                <td className="px-4 py-2.5">{t.account_nickname || '-'}</td>
                <td className="px-4 py-2.5">
                  {t.category_name
                      || (t.category_id === UNCATEGORIZED_ID ? '未分类' : '-')}
                </td>
                <td className="px-4 py-2.5">{intervalText(t.interval_config)}</td>
                <td className="px-4 py-2.5">
                  <div className="flex flex-col gap-0.5">
                    <StatusBadge status={t.status} />
                    {/* v22 阶梯进度：第 N 轮 / 累计 X 条（用户决策：点"重新启用"才清零） */}
                    <span className="text-[10px] text-muted-foreground">
                      第 {(t.pull_round ?? 0) + 1} 轮 · 累计 {t.total_pulled ?? 0} 条
                    </span>
                    {/* #任务队列进度列：执行中展示 task_service 内存实时进度；waiting/running 显示，未跑不显示 */}
                    {t.current_run && t.current_run.status === 'running' && (
                      <span className="text-[10px] text-accent" title={t.current_run.message || ''}>
                        ⚙ {t.current_run.progress || '执行中...'}
                      </span>
                    )}
                    {t.current_run && t.current_run.status === 'waiting' && (
                      <span className="text-[10px] text-muted-foreground">⏳ 排队中</span>
                    )}
                  </div>
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{t.last_run_time || '-'}</td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{t.next_run_time || '-'}</td>
                <td className="px-4 py-2.5">
                  <div className="flex gap-1">
                    {onShowLogs && <Button variant="outline" size="sm" onClick={() => onShowLogs(t)}>拉取记录</Button>}
                    <Button variant="outline" size="sm" onClick={() => doRun(t)}>执行</Button>
                    <Button variant="outline" size="sm" onClick={() => onEdit(t)}>编辑</Button>
                    {t.status === 'disabled' && t.stop_reason === 'auto_max' ? (
                      // v22 达 max_count 自动停用：显示"重新启用"按钮，清零阶梯
                      <Button variant="outline" size="sm"
                        className="!text-accent"
                        onClick={async () => {
                          try {
                            await materialApi.restartPullTask(t.id)
                            toast('已重新启用，从第 1 轮开始拉取', 'success')
                            load()
                          } catch (e) { toast((e as Error).message, 'error') }
                        }}>
                        重新启用
                      </Button>
                    ) : (
                      // 手动停用 / 启用中：保留普通 toggle 语义
                      <Button variant="outline" size="sm"
                        onClick={async () => {
                          try {
                            await materialApi.togglePullTask(t.id, t.status !== 'enabled')
                            load()
                          } catch (e) { toast((e as Error).message, 'error') }
                        }}>
                        {t.status === 'enabled' ? '停用' : '启用'}
                      </Button>
                    )}
                    <Button variant="outline" size="sm" className="!text-danger" onClick={() => setDeleteTarget(t)}>删除</Button>
                  </div>
                </td>
              </tr>
            ))}
            {tasks.list.length === 0 && (
              <tr><td colSpan={9} className="px-4 py-12 text-center text-sm text-muted-foreground">
                暂无拉取任务，点击创建按钮自定义条件
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4">
        <Pagination page={page} pageSize={pageSize} total={tasks.total} onChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} />
      </div>
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(v) => !v && setDeleteTarget(null)}
        title="删除拉取任务"
        danger
        content={`确定删除任务「${deleteTarget?.task_name}」及其执行日志？已入库素材保留。`}
        onConfirm={async () => {
          if (!deleteTarget) return
          try {
            await materialApi.deletePullTask(deleteTarget.id)
            toast('已删除', 'success')
            load()
          } catch (e) { toast((e as Error).message, 'error') }
        }}
      />
    </div>
  )
}
