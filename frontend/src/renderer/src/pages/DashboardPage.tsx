import { useEffect, useState } from 'react'
import { dashboardApi, type DashboardSummary } from '@/api/dashboard'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/useAppStore'

/** 工作台（9.1.2：指标卡 + 待处理事项） */
export function DashboardPage() {
  const [data, setData] = useState<DashboardSummary | null>(null)
  const setPage = useAppStore((s) => s.setPage)

  const load = () => dashboardApi.dashboard().then(setData).catch((e) => console.warn('[Dashboard] 请求失败:', e))
  useEffect(() => {
    load()
    const t = window.setInterval(load, 10000)
    return () => window.clearInterval(t)
  }, [])

  if (!data) return <div className="p-5 text-sm text-muted-foreground">加载中…</div>

  const cards = [
    { label: '账号', value: data.cards.account_count, sub: data.cards.account_invalid ? `⚠ ${data.cards.account_invalid} 失效` : '全部正常', page: 'accounts' as const },
    { label: '门店', value: data.cards.shop_total, sub: `今日新增 ${data.cards.shop_new_today}`, page: 'selection' as const },
    { label: '素材', value: data.cards.material_total, sub: `今日新增 ${data.cards.material_new_today}`, page: 'materials' as const },
    { label: '可用成品', value: data.cards.video_idle, sub: `已占用 ${data.cards.video_occupied}`, page: 'creation' as const },
  ]

  return (
    <div className="p-5">
      {/* 指标卡 */}
      <div className="grid grid-cols-4 gap-4">
        {cards.map((c) => (
          <button key={c.label} className="rounded-lg border border-border bg-background-elev p-4 text-left transition-colors hover:border-accent"
            onClick={() => setPage(c.page)}>
            <div className="text-xs text-muted-foreground">{c.label}</div>
            <div className="mt-1 text-2xl font-semibold">{c.value}</div>
            <div className={`mt-1 text-xs ${c.sub.startsWith('⚠') ? 'text-danger' : 'text-muted-foreground'}`}>{c.sub}</div>
          </button>
        ))}
      </div>

      {/* 待处理事项 */}
      <div className="mt-5 rounded-lg border border-border bg-background-elev p-4">
        <div className="mb-3 text-sm font-semibold">待处理事项</div>
        <div className="space-y-2">
          {data.todo.invalid_accounts.map((a) => (
            <div key={a.id} className="flex items-center gap-3 rounded-md bg-red-50 px-3 py-2 text-sm">
              <span className="text-danger">● 账号 {a.remark || a.nickname} Cookie 失效</span>
              {/* 跳转账号页走 ReloginDialog（粘贴 Cookie 重登）；原 openDouyinLogin 已删除 */}
              <Button variant="outline" size="sm" className="ml-auto"
                onClick={() => setPage('accounts')}>重新登录</Button>
            </div>
          ))}
          {data.todo.running_publish_tasks.map((t) => (
            <div key={t.id} className="flex items-center gap-3 rounded-md bg-blue-50 px-3 py-2 text-sm">
              <span className="text-accent">● 发布任务「{t.task_name}」执行中（{t.start_time} 开始）</span>
              <Button variant="outline" size="sm" className="ml-auto" onClick={() => setPage('publish')}>查看</Button>
            </div>
          ))}
          {data.todo.invalid_accounts.length === 0 && data.todo.running_publish_tasks.length === 0 && (
            <div className="py-4 text-center text-sm text-muted-foreground">暂无待处理事项</div>
          )}
        </div>
      </div>
    </div>
  )
}
