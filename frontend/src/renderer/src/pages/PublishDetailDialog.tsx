/** 发布任务只读详情对话框（#413）。含复制入口。 */
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Tooltip } from '@/components/ui/tooltip'
import { toast } from '@/components/ui/toast'
import { publishApi, type PublishItem, type PublishTask } from '@/api/publish'

interface Props {
  open: boolean
  onOpenChange: (v: boolean) => void
  taskId: string
  /** #453：复制时把 payload 抛给父组件，由父组件开 wizard 回显；不再走旧 onDuplicated(task_id) */
  onDuplicatePayload?: (payload: Omit<import('@/api/publish').DraftRequest, 'task_id'>) => void
}

export function PublishDetailDialog({ open, onOpenChange, taskId, onDuplicatePayload }: Props) {
  const [task, setTask] = useState<PublishTask | null>(null)
  const [items, setItems] = useState<PublishItem[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open || !taskId) return
    setLoading(true)
    publishApi.getTaskDetail(taskId)
      .then((r) => {
        const { items: its, ...rest } = r
        setTask(rest as PublishTask)
        setItems(its ?? [])
      })
      .catch((e) => toast((e as Error).message, 'error'))
      .finally(() => setLoading(false))
  }, [open, taskId])

  const onDuplicate = async () => {
    setBusy(true)
    try {
      const r = await publishApi.duplicateTask(taskId)
      // #453：把 payload 抛给父组件 → 父组件开 wizard 回显；本弹窗关闭
      onDuplicatePayload?.(r.payload)
      onOpenChange(false)
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const failedCount = items.filter((i) => i.status === 'failed').length
  const retryable = !!task && task.status !== 'cancelled' && failedCount > 0

  const onRetry = async () => {
    if (!retryable) return
    setBusy(true)
    try {
      const r = await publishApi.retryTask(taskId)
      toast(`重试任务：复活 ${r.retried} 条 / 关闭 ${r.closed} 条（plan_time 不足2h 已关闭）`, 'success')
      // 重新加载详情
      const refreshed = await publishApi.getTaskDetail(taskId)
      const { items: its, ...rest } = refreshed
      setTask(rest as PublishTask)
      setItems(its ?? [])
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="任务详情（只读）" width={960}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>关闭</Button>
          <Button size="sm"
            disabled={busy || !retryable}
            title={!retryable ? '无失败明细或任务已取消' : `复活 plan_time 距今 ≥2h 的失败明细，<2h 的将永久关闭`}
            onClick={onRetry}>
            {busy ? '重试中…' : '重试任务'}
          </Button>
          <Button size="sm"
            disabled={busy || !task}
            onClick={onDuplicate}>
            {busy ? '复制中…' : '复制'}
          </Button>
        </>
      }>
      {loading && <div className="py-8 text-center text-sm text-muted-foreground">加载中…</div>}
      {!loading && task && (
        <div className="space-y-3 text-sm">
          <div className="grid grid-cols-2 gap-2 rounded-md bg-muted/40 p-3">
            <div><span className="text-muted-foreground">任务名：</span>{task.task_name}</div>
            <div><span className="text-muted-foreground">状态：</span><StatusBadge status={task.status} /></div>
            <div><span className="text-muted-foreground">起始：</span><span className="font-mono">{task.start_time}</span></div>
            <div><span className="text-muted-foreground">结束：</span><span className="font-mono">{task.end_time ?? '-'}</span></div>
            <div><span className="text-muted-foreground">每天上限：</span>
              {task.daily_limit_mode === 'global' ? `全局 ${task.daily_limit_global ?? '-'}`
                : '分账号'}</div>
            <div>
              <span className="text-muted-foreground">计算模式：</span>
              {task.schedule_mode === 'fixed'
                ? `固定间隔 ${task.fixed_interval_min ?? 10} 分钟`
                : `均衡间隔 ${task.balanced_step_min ?? task.same_project_interval_min ?? 60} 分钟`}
            </div>
            <div><span className="text-muted-foreground">自主声明：</span>
              {task.declaration === 'ai_generated' ? '内容由 AI 生成' : '无需添加'}</div>
            <div><span className="text-muted-foreground">允许下载：</span>{task.allow_download ? '允许' : '不允许'}</div>
            <div><span className="text-muted-foreground">明细进度：</span>
              成功 {task.success_count_real} / 失败 {task.fail_count_real} / 总 {task.item_total}</div>
          </div>

          <div>
            <div className="mb-1 text-xs text-muted-foreground">发布明细（共 {items.length} 条，只读）</div>
            <div className="max-h-80 overflow-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted">
                  <tr className="text-left">
                    <th className="px-2 py-1.5 w-10">#</th>
                    <th className="px-2 py-1.5">计划时间</th>
                    <th className="px-2 py-1.5">账号</th>
                    <th className="px-2 py-1.5">视频</th>
                    <th className="px-2 py-1.5">门店</th>
                    <th className="px-2 py-1.5">简介</th>
                    <th className="px-2 py-1.5">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it, idx) => (
                    <tr key={it.id} className="border-t border-border">
                      <td className="px-2 py-1.5 text-muted-foreground">{idx + 1}</td>
                      <td className="px-2 py-1.5 font-mono">{it.plan_time}</td>
                      <td className="px-2 py-1.5">{it.account_remark || it.account_nickname || it.account_id}</td>
                      <td className="max-w-52 truncate px-2 py-1.5" title={it.video_title || it.video_path || ''}>
                        {it.video_title || it.video_path
                          ? (it.video_title || (it.video_path?.split(/[\\/]/).pop() ?? '-'))
                          : '-'}
                      </td>
                      <td className="px-2 py-1.5">{it.shop_name || it.shop_id}</td>
                      <td className="max-w-40 truncate px-2 py-1.5" title={it.intro_snapshot ?? ''}>
                        {it.intro_snapshot ?? '-'}
                      </td>
                      <td className="px-2 py-1.5">
                        <span className="inline-flex items-center gap-1">
                          <StatusBadge status={it.status} />
                          {it.fail_reason && (
                            <Tooltip content={it.fail_reason}>
                              <span className="help-icon inline-flex h-5 w-5 cursor-help items-center justify-center rounded text-[13px] leading-none text-danger hover:bg-danger/10">
                                ⚠
                              </span>
                            </Tooltip>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {items.length === 0 && (
                    <tr><td colSpan={7} className="px-2 py-4 text-center text-muted-foreground">暂无明细</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </Dialog>
  )
}