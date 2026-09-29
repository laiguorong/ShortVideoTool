import { useCallback, useEffect, useState } from 'react'
import {
  selectionApi,
  type Shop,
  type ShopPullTask,
  type IntervalConfig,
} from '@/api/selection'
import { accountApi, type AvailableAccount } from '@/api/account'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { StatusBadge, STATUS_LABEL } from '@/components/shared/StatusBadge'
import { Pagination } from '@/components/shared/Pagination'
import { toast } from '@/components/ui/toast'


/** 账号状态码 → 中文（#P1-7：复用 STATUS_LABEL 单一来源；之前 local 表写"失效"和 badge "Cookie 失效"不一致） */
const accountStatusLabel = (status: string): string =>
  STATUS_LABEL[status] || status


/** ============ 创建/编辑任务弹窗（任务 #407 合并）============ */
function CreateTaskDialog({ open, onOpenChange, onCreated, editTarget }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: () => void
  /** 编辑模式：传入任务 → 提交走 updateTask，否则 createTask */
  editTarget?: ShopPullTask | null
}) {
  const [accounts, setAccounts] = useState<AvailableAccount[]>([])
  const [name, setName] = useState('')
  const [keyword, setKeyword] = useState('')
  const [accountId, setAccountId] = useState('')
  const [shopNamePattern, setShopNamePattern] = useState('')
  const [cityPattern, setCityPattern] = useState('')
  const [intervalType, setIntervalType] = useState<'minute' | 'hour' | 'daily'>('hour')
  const [intervalValue, setIntervalValue] = useState(6)
  const [intervalTime, setIntervalTime] = useState('09:00')
  const [loading, setLoading] = useState(false)
  const isEdit = !!editTarget

  useEffect(() => {
    if (!open) return
    accountApi.available().then((r) => {
      setAccounts(r.list)
      // 编辑模式：从 editTarget 预填；否则默认首个账号
      if (editTarget) {
        setName(editTarget.task_name)
        setKeyword(editTarget.keyword)
        setAccountId(editTarget.account_id)
        setShopNamePattern(editTarget.shop_name_pattern || '')
        setCityPattern(editTarget.city_pattern || '')
        const iv = editTarget.interval
        setIntervalType((iv.type as typeof intervalType) || 'hour')
        if (iv.type === 'daily') setIntervalTime(iv.time || '09:00')
        else setIntervalValue(iv.value || 6)
      } else if (r.list[0]) {
        setAccountId(r.list[0].id)
        // 重置其它字段（防止上次打开残留）
        setName(''); setKeyword('')
        setShopNamePattern(''); setCityPattern('')
        setIntervalType('hour'); setIntervalValue(6); setIntervalTime('09:00')
      }
    }).catch((e) => console.warn('[Selection] 请求失败:', e))
  }, [open, editTarget])

  const submit = async () => {
    if (!name.trim()) return toast('请填写任务名', 'error')
    if (!accountId) return toast('请选择拉取账号', 'error')
    if (!keyword.trim()) return toast('请填写搜索关键词', 'error')
    if (intervalType === 'minute' && intervalValue < 10) {
      return toast('最小间隔 10 分钟（防风控）', 'error')
    }
    setLoading(true)
    try {
      const interval: IntervalConfig =
        intervalType === 'daily'
          ? { type: 'daily', time: intervalTime }
          : { type: intervalType, value: intervalValue }
      const body = {
        task_name: name.trim(),
        keyword: keyword.trim(),
        cities: [] as string[],
        account_id: accountId,
        interval,
        shop_name_pattern: shopNamePattern.trim(),
        city_pattern: cityPattern.trim(),
      }
      if (editTarget) {
        await selectionApi.updateTask(editTarget.id, body)
        toast('任务已更新', 'success')
      } else {
        await selectionApi.createTask(body)
        toast('任务已创建并启用', 'success')
      }
      onOpenChange(false)
      if (!editTarget) {
        setName(''); setKeyword('')
        setShopNamePattern(''); setCityPattern('')
      }
      onCreated()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? `编辑任务：${editTarget?.task_name}` : '创建选品拉取任务'}
      width={620}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={loading} onClick={submit}>
            {loading ? '保存中…' : (isEdit ? '保存' : '创建并启用')}
          </Button>
        </>
      }
    >
      <div className="space-y-3.5">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-sm font-medium">任务名称 <span className="text-destructive">*</span></label>
            <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm"
              value={name} onChange={(e) => setName(e.target.value)}
              placeholder="如：高佣金美食选品" />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">拉取账号 <span className="text-destructive">*</span></label>
            <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
              value={accountId} onChange={(e) => setAccountId(e.target.value)}>
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
          </div>
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium">
            搜索关键词 <span className="text-destructive">*</span>
          </label>
          <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm"
            value={keyword} onChange={(e) => setKeyword(e.target.value)}
            placeholder="如：黑眼熊寿司" />
          <p className="mt-1 text-xs text-muted-foreground">
            仅拉取带佣金的 CPS 门店并自动获取详情，无需额外配置。
          </p>
        </div>
        {/* 任务 #407：正则过滤（空 = 不过滤） */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-sm font-medium">门店正则（可选）</label>
            <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm font-mono"
              value={shopNamePattern} onChange={(e) => setShopNamePattern(e.target.value)}
              placeholder="如：^.*?火锅.*?$" />
            <p className="mt-1 text-xs text-muted-foreground">匹配门店名，空=全部。</p>
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">城市正则（可选）</label>
            <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm font-mono"
              value={cityPattern} onChange={(e) => setCityPattern(e.target.value)}
              placeholder="如：^.*?北京.*?$" />
            <p className="mt-1 text-xs text-muted-foreground">匹配 POI 城市（中文），空=全部。</p>
          </div>
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium">执行时间间隔</label>
          <div className="flex items-center gap-2">
            <select className="h-9 rounded-md border border-border bg-white px-2 text-sm"
              value={intervalType}
              onChange={(e) => setIntervalType(e.target.value as typeof intervalType)}>
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


/** ============ 任务管理 Tab ============ */
function TaskTab({ refreshTrigger, onLoadingChange, onEdit }: {
  refreshTrigger?: number
  onLoadingChange?: (loading: boolean) => void
  /** 任务编辑回调（#407：操作列点编辑后由父级弹 CreateTaskDialog 编辑模式） */
  onEdit?: (task: ShopPullTask) => void
}) {
  const [data, setData] = useState<{ list: ShopPullTask[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [deleteTarget, setDeleteTarget] = useState<ShopPullTask | null>(null)
  // 账号列表（用于"账号"列显示备注名，#407）
  const [accounts, setAccounts] = useState<Map<string, { remark: string | null; nickname: string | null }>>(new Map())

  const load = useCallback(() => {
    onLoadingChange?.(true)
    selectionApi.listTasks(page, pageSize)
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Selection] 请求失败:', e))
      .finally(() => onLoadingChange?.(false))
  }, [page, pageSize, onLoadingChange])

  const loadAccounts = useCallback(() => {
    accountApi.available()
      .then((r) => {
        const m = new Map<string, { remark: string | null; nickname: string | null }>()
        for (const a of r.list) m.set(a.id, { remark: a.remark, nickname: a.nickname })
        setAccounts(m)
      })
      .catch((e) => console.warn('[Selection] 请求失败:', e))
  }, [])

  useEffect(load, [load])
  useEffect(loadAccounts, [loadAccounts])
  useEffect(() => { if (refreshTrigger) load() }, [refreshTrigger]) // eslint-disable-line react-hooks/exhaustive-deps

  /** 账号备注展示：有 remark 用 remark，否则 nickname，再否则 ID 前 8 位 */
  const accountLabel = (id: string): string => {
    const a = accounts.get(id)
    if (a?.remark) return a.remark
    if (a?.nickname) return a.nickname
    return id.slice(0, 8) + '…'
  }

  /** 拉取条件拼接（任务 #407）：关键词 + 店名正则 + 城市正则 */
  const conditionsLabel = (t: ShopPullTask): string => {
    const parts = [t.keyword]
    if (t.shop_name_pattern) parts.push(`店名/${t.shop_name_pattern}`)
    if (t.city_pattern) parts.push(`城市/${t.city_pattern}`)
    return parts.join(' · ')
  }

  const doRun = async (t: ShopPullTask) => {
    try {
      await selectionApi.runTask(t.id)
      toast('门店拉取已开始，结果见执行记录', 'success')
      setTimeout(load, 2000)
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const doToggle = async (t: ShopPullTask) => {
    try {
      await selectionApi.toggleTask(t.id, t.status !== 'enabled')
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const intervalText = (cfg: IntervalConfig): string => {
    if (cfg.type === 'daily') return `每天 ${cfg.time || '09:00'}`
    if (cfg.value) return `每 ${cfg.value} ${cfg.type === 'minute' ? '分钟' : '小时'}`
    return '-'
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
              <th className="px-4 py-2.5 font-medium">间隔</th>
              <th className="px-4 py-2.5 font-medium">状态</th>
              <th className="px-4 py-2.5 font-medium">最近执行</th>
              <th className="px-4 py-2.5 font-medium">下次执行</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((t) => (
              <tr key={t.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                <td className="px-4 py-2.5 font-medium">{t.task_name}</td>
                <td className="px-4 py-2.5 text-xs font-mono" title={conditionsLabel(t)}>
                  <span className="line-clamp-2">{conditionsLabel(t)}</span>
                </td>
                <td className="px-4 py-2.5 text-xs">{accountLabel(t.account_id)}</td>
                <td className="px-4 py-2.5 text-xs">{intervalText(t.interval)}</td>
                <td className="px-4 py-2.5"><StatusBadge status={t.status} /></td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{t.last_run_time || '-'}</td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{t.next_run_time || '-'}</td>
                <td className="px-4 py-2.5">
                  <div className="flex gap-1">
                    <Button variant="outline" size="sm" onClick={() => doRun(t)}>执行</Button>
                    <Button variant="outline" size="sm" onClick={() => onEdit?.(t)}>编辑</Button>
                    <Button variant="outline" size="sm" onClick={() => doToggle(t)}>
                      {t.status === 'enabled' ? '停用' : '启用'}
                    </Button>
                    <Button variant="outline" size="sm" className="!text-danger" onClick={() => setDeleteTarget(t)}>
                      删除
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {data.list.length === 0 && (
              <tr><td colSpan={8} className="px-4 py-12 text-center text-muted-foreground">
                暂无任务，点击右上「创建拉取任务」
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
        onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(v) => !v && setDeleteTarget(null)}
        title="删除任务"
        danger
        content={`确定删除任务「${deleteTarget?.task_name}」？已入库门店保留。`}
        onConfirm={async () => {
          if (!deleteTarget) return
          try {
            await selectionApi.deleteTask(deleteTarget.id)
            toast('已删除', 'success')
            load()
          } catch (e) { toast((e as Error).message, 'error') }
        }}
      />
    </div>
  )
}


/** ============ 门店库 Tab ============ */
function ShopTab({
  refreshTrigger, onLoadingChange, filterState,
}: {
  refreshTrigger?: number
  onLoadingChange?: (loading: boolean) => void
  /** 父级控制的筛选/排序 state（让 filterBar 能在 header 行与 Tab 同行） */
  filterState: {
    keyword: string; setKeyword: (v: string) => void
    province: string; setProvince: (v: string) => void
    city: string; setCity: (v: string) => void
    category: string; setCategory: (v: string) => void
    sort: string; setSort: (v: string) => void
    /** 有佣金：'' 全部 / 'yes' 仅佣金门店 / 'no' 仅无佣金 */
    hasCommission: string; setHasCommission: (v: string) => void
    /** 已加库：'' 全部 / 'yes' 仅已加库 / 'no' 仅未加库 */
    isAdded: string; setIsAdded: (v: string) => void
    page: number
    setPage: (n: number) => void
    reload: () => void
    provinces: string[]
    cities: string[]
    categories: string[]
    SORTS: [string, string][]
  }
}) {
  const [data, setData] = useState<{ list: Shop[]; total: number }>({ list: [], total: 0 })
  const [pageSize, setPageSize] = useState(20)

  const {
    keyword, province, city, category, sort,
    hasCommission, isAdded,
    page, setPage,
    reload,
  } = filterState

  const load = useCallback(() => {
    onLoadingChange?.(true)
    selectionApi.listShops({
      keyword: keyword || undefined,
      category: category || undefined,
      city: city || undefined,
      province: province || undefined,
      is_cps_only: hasCommission === 'yes' ? true : hasCommission === 'no' ? false : undefined,
      is_added_only: isAdded === 'yes' ? true : isAdded === 'no' ? false : undefined,
      sort,
      page,
      page_size: pageSize,
    })
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Selection] 请求失败:', e))
      .finally(() => onLoadingChange?.(false))
  }, [keyword, category, city, province, hasCommission, isAdded, sort, page, pageSize, onLoadingChange])

  useEffect(load, [load])
  useEffect(() => { if (refreshTrigger) load() }, [refreshTrigger, load]) // eslint-disable-line react-hooks/exhaustive-deps

  /** 一级分类：从 category_full 截 "美食/日韩料理/寿司" 的第一段。
   *  category（码）传给后端做精确筛选，前端展示用 category_full。 */
  const l1Category = (s: Shop): string => {
    const full = s.category_full || ''
    return full.split('/')[0] || s.category || '-'
  }

  /** 佣金率格式化（take_rate 万分之 → %）：
   *  - min == max：单值
   *  - 都有且不等：min~max
   *  - 兜底 take_rate_avg / commission_rate
   *  - 数字规则：去掉为 0 的小数位，保留 1 位非 0 小数（"3" 而非 "3.00"，"3.5" 而非 "3.50"）
   *  - 返回 { text, minPct }，minPct 用于"≥3% 绿色"判定 */
  const fmtPct = (v: number): string => {
    if (!Number.isFinite(v)) return '-'
    if (Math.abs(v) < 1e-9) return '0'
    const rounded = Math.round(v * 10) / 10  // 保留 1 位小数
    return Number.isInteger(rounded) ? `${rounded}` : `${rounded.toFixed(1)}`
  }

  const rateText = (s: Shop): { text: string; minPct: number | null } => {
    const fromWan = (w: number) => w / 100
    if (s.take_rate_min != null && s.take_rate_max != null) {
      const min = fromWan(s.take_rate_min)
      const max = fromWan(s.take_rate_max)
      const minS = fmtPct(min)
      const maxS = fmtPct(max)
      return {
        text: minS === maxS ? `${minS}%` : `${minS} ~ ${maxS}%`,
        minPct: min,
      }
    }
    if (s.take_rate_avg != null) {
      const v = fromWan(s.take_rate_avg)
      return { text: `${fmtPct(v)}%`, minPct: v }
    }
    if (s.commission_rate != null) {
      const v = s.commission_rate * 100
      return { text: `${fmtPct(v)}%`, minPct: v }
    }
    return { text: '-', minPct: null }
  }

  /** 切换加库标记 */
  const doToggleAdded = async (s: Shop) => {
    const willAdd = s.is_added_to_library !== 1
    try {
      await selectionApi.toggleAdded(s.id, willAdd)
      toast(willAdd ? '已加库' : '已取消加库', 'success')
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  /** 完整日期时间 "2026-09-19 12:34:56" → "2026-09-19 12:34:56" */
  const shortTime = (s: string | null): string => (s ? s.slice(0, 19) : '-')

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-3 py-2.5 text-left font-medium">城市</th>
              <th className="px-3 py-2.5 text-left font-medium">分类</th>
              <th className="px-3 py-2.5 text-left font-medium">店名</th>
              <th className="px-3 py-2.5 text-left font-medium">商品</th>
              <th className="px-3 py-2.5 text-left font-medium">有佣金</th>
              <th className="px-3 py-2.5 text-left font-medium">佣金率</th>
              <th className="px-3 py-2.5 text-left font-medium">TOP SPU</th>
              <th className="px-3 py-2.5 text-left font-medium">创建时间</th>
              <th className="px-3 py-2.5 text-left font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((s) => {
              const rt = rateText(s)
              const productMatch = s.cps_spu_count > 0 && s.cps_spu_count === s.spu_count
              const isHighRate = rt.minPct != null && rt.minPct >= 3
              return (
                <tr key={s.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                  <td className="px-3 py-2 text-left text-xs">
                    <div className="font-medium">{s.city || '-'}</div>
                    {s.province && <div className="text-xs text-muted-foreground">{s.province}</div>}
                  </td>
                  <td className="px-3 py-2 text-left text-xs">{l1Category(s)}</td>
                  <td className="px-3 py-2 text-left font-medium">
                    <div>
                      {s.name}
                      {s.detail_fetched === 1 && (
                        <span className="ml-1 text-xs text-blue-500" title="已拉详情">📊</span>
                      )}
                    </div>
                    {s.address && (
                      <div className="text-xs text-muted-foreground">{s.address}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-left text-xs">
                    {productMatch ? (
                      <span className="inline-block rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700">
                        {s.cps_spu_count} / {s.spu_count}
                      </span>
                    ) : (
                      <span className="inline-block rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                        {s.cps_spu_count} / {s.spu_count}
                      </span>
                    )}
                    {s.delivery_spu_count > 0 && (
                      <div className="text-xs text-muted-foreground">配{s.delivery_spu_count}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-left text-xs">
                    {s.is_cps === 1
                      ? <span className="text-base font-medium text-accent" title="有佣金">✓</span>
                      : <span className="text-muted-foreground/40" title="无佣金">–</span>}
                  </td>
                  <td className="px-3 py-2 text-left text-xs">
                    {isHighRate ? (
                      <span className="inline-block rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700">
                        {rt.text}
                      </span>
                    ) : (
                      <span className="inline-block rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                        {rt.text}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-left text-xs">
                    {s.top_spu_name ? (
                      <div className="truncate max-w-[12rem]" title={s.top_spu_name}>{s.top_spu_name}</div>
                    ) : '-'}
                  </td>
                  <td className="px-3 py-2 text-left text-xs text-muted-foreground">{shortTime(s.create_time)}</td>
                  <td className="px-3 py-2 text-left">
                    <Button
                      size="sm"
                      variant={s.is_added_to_library === 1 ? undefined : 'outline'}
                      className={s.is_added_to_library === 1
                        ? '!bg-green-100 !text-green-700 hover:!bg-green-200 border-0'
                        : ''}
                      onClick={() => doToggleAdded(s)}
                    >
                      {s.is_added_to_library === 1 ? '已加库' : '加库'}
                    </Button>
                  </td>
                </tr>
              )
            })}
            {data.list.length === 0 && (
              <tr><td colSpan={8} className="px-4 py-12 text-center text-muted-foreground">
                暂无门店，请先创建拉取任务
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
        onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
    </div>
  )
}


/** ============ 选品中心页面 ============ */
export function SelectionPage() {
  const [tab, setTab] = useState<'shops' | 'tasks'>('shops')
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<ShopPullTask | null>(null)
  const [refreshTrigger, setRefreshTrigger] = useState(0)
  const [refreshing, setRefreshing] = useState(false)

  // 门店库筛选 / 排序 state（提到页面层，便于工具栏与 Tab 同行渲染）
  const [keyword, setKeyword] = useState('')
  const [sort, setSort] = useState('create_time')
  const [province, setProvince] = useState('')
  const [city, setCity] = useState('')
  const [category, setCategory] = useState('')
  const [hasCommission, setHasCommission] = useState('')
  const [isAdded, setIsAdded] = useState('')
  const [shopPage, setShopPage] = useState(1)
  const [provinces, setProvinces] = useState<string[]>([])
  const [cities, setCities] = useState<string[]>([])
  const [categories, setCategories] = useState<string[]>([])

  useEffect(() => {
    selectionApi.fetchProvinces().then((r) => setProvinces(r.provinces)).catch((e) => console.warn('[Selection] 请求失败:', e))
    selectionApi.fetchCities().then((r) => setCities(r.cities)).catch((e) => console.warn('[Selection] 请求失败:', e))
    selectionApi.fetchCategories().then((r) => setCategories(r.categories)).catch((e) => console.warn('[Selection] 请求失败:', e))
  }, [])

  const SORTS: [string, string][] = [
    ['cps_spu_count', '佣金商品数'],
    ['spu_count', '商品数'],
    ['commission_rate', '佣金率'],
    ['city', '城市'],
    ['create_time', '创建时间'],
  ]

  const triggerRefresh = () => {
    setRefreshing(true)
    setRefreshTrigger((t) => t + 1)
  }

  /** 筛选变化触发刷新（与"刷新"按钮共用 triggerRefresh，按钮才能转"刷新中…"） */
  const applyFilter = (mutator: () => void) => {
    mutator()
    triggerRefresh()
  }

  /** 筛选/排序工具栏（与 Tab 同行展示） */
  const shopFilterBar = (
    <div className="flex flex-wrap items-center gap-2">
      <input className="h-8 w-56 rounded-md border border-border bg-white px-2.5 text-sm"
        placeholder="搜索店名"
        value={keyword}
        onChange={(e) => applyFilter(() => { setKeyword(e.target.value); setShopPage(1) })} />

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={province} onChange={(e) => applyFilter(() => { setProvince(e.target.value); setCity(''); setShopPage(1) })}>
        <option value="">全部省份</option>
        {provinces.map((p) => <option key={p} value={p}>{p}</option>)}
      </select>

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={city} onChange={(e) => applyFilter(() => { setCity(e.target.value); setShopPage(1) })}>
        <option value="">全部城市</option>
        {cities.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={category} onChange={(e) => applyFilter(() => { setCategory(e.target.value); setShopPage(1) })}>
        <option value="">全部分类</option>
        {categories.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={hasCommission} onChange={(e) => applyFilter(() => { setHasCommission(e.target.value); setShopPage(1) })}>
        <option value="">有佣金（全部）</option>
        <option value="yes">仅佣金门店</option>
        <option value="no">仅无佣金</option>
      </select>

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={isAdded} onChange={(e) => applyFilter(() => { setIsAdded(e.target.value); setShopPage(1) })}>
        <option value="">加库（全部）</option>
        <option value="yes">仅已加库</option>
        <option value="no">仅未加库</option>
      </select>

      <select className="h-8 rounded-md border border-border bg-white px-2 text-sm"
        value={sort} onChange={(e) => applyFilter(() => setSort(e.target.value))}>
        {SORTS.map(([v, l]) => <option key={v} value={v}>按{l}排序</option>)}
      </select>
    </div>
  )

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4 flex shrink-0 flex-wrap items-center gap-4">
        <h1 className="flex h-8 items-center text-lg font-semibold">选品中心</h1>
        <div className="flex gap-4 text-sm">
          {([['shops', '门店'], ['tasks', '任务']] as const).map(([k, l]) => (
            <button key={k}
              className={tab === k ? 'font-medium text-accent' : 'text-muted-foreground hover:text-foreground'}
              onClick={() => { setTab(k); triggerRefresh() }}>{l}</button>
          ))}
        </div>
        {/* shops tab：筛选条紧跟 Tab，刷新按钮紧跟筛选条 */}
        {tab === 'shops' && (
          <>
            <div>{shopFilterBar}</div>
            <Button size="sm" variant="outline" disabled={refreshing} onClick={() => triggerRefresh()}>
              {refreshing ? '刷新中…' : '刷新'}
            </Button>
          </>
        )}
        {/* tasks tab：刷新按钮紧跟 Tab，创建任务靠右 */}
        {tab === 'tasks' && (
          <>
            <Button size="sm" variant="outline" disabled={refreshing} onClick={() => triggerRefresh()}>
              {refreshing ? '刷新中…' : '刷新'}
            </Button>
            <Button size="sm" variant="outline" className="ml-auto" onClick={() => setCreateOpen(true)}>
              创建任务
            </Button>
          </>
        )}
      </div>
      {tab === 'shops' ? (
        <ShopTab
          refreshTrigger={refreshTrigger}
          onLoadingChange={setRefreshing}
          filterState={{
            keyword, setKeyword, province, setProvince, city, setCity,
            category, setCategory, sort, setSort,
            hasCommission, setHasCommission, isAdded, setIsAdded,
            page: shopPage,
            setPage: setShopPage,
            reload: triggerRefresh,
            provinces, cities, categories, SORTS,
          }}
        />
      ) : (
        <TaskTab refreshTrigger={refreshTrigger} onLoadingChange={setRefreshing}
          onEdit={(t) => { setEditTarget(t); setCreateOpen(true) }} />
      )}
      <CreateTaskDialog
        open={createOpen}
        onOpenChange={(v) => { setCreateOpen(v); if (!v) setEditTarget(null) }}
        editTarget={editTarget}
        onCreated={() => { triggerRefresh() }} />
    </div>
  )
}