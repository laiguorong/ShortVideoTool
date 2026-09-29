import { useCallback, useEffect, useState } from 'react'
import { publishApi, type PublishTask, type DraftRequest } from '@/api/publish'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/dialog'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Pagination } from '@/components/shared/Pagination'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { PublishWizardDialog } from './PublishWizardDialog'
import { PublishDetailDialog } from './PublishDetailDialog'

/** 行内 spinner（loading 圆环动画，CSS keyframes 跟随全局样式） */
function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="处理中"
      className={cn('inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent', className)}
    />
  )
}

/** 进度列——发布中心列表只显示成功 / 总（用时在「任务队列」tab 看实时） */
function ProgressCell({ task }: { task: PublishTask }) {
  return (
    <td className="px-4 py-2.5 whitespace-nowrap">
      成功 {task.success_count_real} / 总 {task.item_total}
    </td>
  )
}

/** 操作类型（每个 (task.id, action) 独立 busy 状态） */
type TaskAction = 'start' | 'edit' | 'pause' | 'resume' | 'cancel' | 'delete' | 'duplicate' | 'detail' | 'restart' | 'retry'

/** 发布中心页（9.3.5 + #413 + #454） */
export function PublishPage() {
  const [data, setData] = useState<{ list: PublishTask[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [createOpen, setCreateOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [detailId, setDetailId] = useState<string>('')
  // C3：状态过滤
  const [statusFilter, setStatusFilter] = useState<string>('')
  // #418：编辑草稿任务时携带 task_id 进 wizard
  const [editingDraftId, setEditingDraftId] = useState<string | null>(null)
  // #453：复制任务携带等价 DraftRequest 进 wizard（不直接落库）
  const [duplicatingPayload, setDuplicatingPayload] = useState<Omit<DraftRequest, 'task_id'> | null>(null)
  // 操作独立 busy 跟踪：busyMap[`${taskId}:${action}`] = true
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({})
  // 二次确认框（合并 start/pause/resume/cancel/delete 五态；按 type 分发文案）
  const [confirmTarget, setConfirmTarget] = useState<{ type: 'start' | 'pause' | 'resume' | 'cancel' | 'delete' | 'restart' | 'retry'; target: PublishTask } | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    publishApi.listTasks(page, pageSize, statusFilter || undefined)
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Publish] 请求失败:', e))
      .finally(() => setLoading(false))
  }, [page, pageSize, statusFilter])
  useEffect(() => { setPage(1) }, [statusFilter])
  useEffect(load, [load])

  /** 设置 (taskId, action) busy 状态；自动执行 + 自动清除（含异常） */
  const runAction = useCallback(async (
    taskId: string,
    action: TaskAction,
    fn: () => Promise<unknown>,
    successMsg?: string,
  ) => {
    const key = `${taskId}:${action}`
    setBusyMap((m) => ({ ...m, [key]: true }))
    try {
      await fn()
      if (successMsg) toast(successMsg, 'success')
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusyMap((m) => {
        const n = { ...m }
        delete n[key]
        return n
      })
    }
  }, [load])

  const isBusy = (taskId: string, action: TaskAction) => !!busyMap[`${taskId}:${action}`]
  // 任意操作忙时锁住整行的复制按钮（避免重复派发同一任务的 wizard）
  const isAnyBusy = (taskId: string) =>
    Object.keys(busyMap).some((k) => k.startsWith(`${taskId}:`))

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="flex h-8 items-center text-lg font-semibold">发布中心</h1>
        <span className="text-sm text-muted-foreground">共 {data.total} 个任务</span>
        <Button size="sm" variant="outline" onClick={load} disabled={loading}>
          {loading ? <Spinner /> : null}
          {loading ? '刷新中…' : '刷新'}
        </Button>
        {/* C3：状态过滤 */}
        <select className="h-8 rounded-md border border-border bg-background-elev px-2 text-sm"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="draft">草稿</option>
          <option value="running">运行中</option>
          <option value="paused">已暂停</option>
          <option value="finished">已完成</option>
          <option value="cancelled">已取消</option>
        </select>
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => setCreateOpen(true)}>新建发布任务</Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">任务名</th>
              <th className="px-4 py-2.5 font-medium">状态</th>
              <th className="px-4 py-2.5 font-medium">进度</th>
              <th className="px-4 py-2.5 font-medium">创建时间</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((t) => {
              const busy = isAnyBusy(t.id)
              return (
                <tr key={t.id} className={cn('border-b border-border hover:bg-muted/50',
                  // #413 审查 #22：草稿态视觉标记（淡色 + 草稿徽标）
                  t.status === 'draft' && 'bg-muted/30 text-muted-foreground')}>
                  <td className="px-4 py-2.5 font-medium">{t.task_name}</td>
                  <td className="px-4 py-2.5"><StatusBadge status={t.display_status || t.status} /></td>
                  <ProgressCell task={t} />
                  <td className="px-4 py-2.5 font-mono text-xs">{t.create_time}</td>
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {/* #418：草稿任务——直接启动 + 编辑 */}
                      {t.status === 'draft' && (
                        <Button size="sm"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'start', target: t })}>
                          {isBusy(t.id, 'start') ? <><Spinner />启动中…</> : '启动'}
                        </Button>
                      )}
                      {t.status === 'draft' && (
                        <Button variant="outline" size="sm"
                          disabled={busy}
                          onClick={() => {
                            setEditingDraftId(t.id)
                            setCreateOpen(true)
                          }}>编辑</Button>
                      )}
                      {t.status === 'running' && (
                        <Button variant="outline" size="sm"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'pause', target: t })}>
                          {isBusy(t.id, 'pause') ? <><Spinner />暂停中…</> : '暂停'}
                        </Button>
                      )}
                      {t.status === 'paused' && (
                        <Button variant="outline" size="sm"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'resume', target: t })}>
                          {isBusy(t.id, 'resume') ? <><Spinner />恢复中…</> : '恢复'}
                        </Button>
                      )}
                      {(t.status === 'running' || t.status === 'paused') && (
                        <Button variant="outline" size="sm" className="!text-danger"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'cancel', target: t })}>
                          {isBusy(t.id, 'cancel') ? <><Spinner />取消中…</> : '取消'}
                        </Button>
                      )}
                      {/* #优化：cancelled 任务可重启——明细回 waiting 重新发布 */}
                      {t.status === 'cancelled' && (
                        <Button variant="outline" size="sm"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'restart', target: t })}>
                          {isBusy(t.id, 'restart') ? <><Spinner />重启中…</> : '重启'}
                        </Button>
                      )}
                      {/* #优化：finished 含失败明细 → 重试按钮（详情页已有，列表快捷入口） */}
                      {(t.status === 'finished' && (t.fail_count_real || 0) > 0) && (
                        <Button variant="outline" size="sm"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'retry', target: t })}>
                          {isBusy(t.id, 'retry') ? <><Spinner />重试中…</> : '重试'}
                        </Button>
                      )}
                      {/* 删除（draft / finished / cancelled 三态可删）—— 用 ConfirmDialog 二次确认 */}
                      {(t.status === 'finished' || t.status === 'cancelled' || t.status === 'draft') && (
                        <Button variant="outline" size="sm" className="!text-danger"
                          disabled={busy}
                          onClick={() => setConfirmTarget({ type: 'delete', target: t })}>
                          {isBusy(t.id, 'delete') ? <><Spinner />删除中…</> : '删除'}
                        </Button>
                      )}
                      <Button variant="outline" size="sm"
                        disabled={busy}
                        onClick={() => setDetailId(t.id)}>查看</Button>
                      <Button variant="outline" size="sm"
                        disabled={busy}
                        onClick={async () => {
                          // 复制按钮单独走 runAction（即使 busy 也要能触发 wizard 关弹窗，这里只锁 spinner）
                          const key = `${t.id}:duplicate`
                          setBusyMap((m) => ({ ...m, [key]: true }))
                          try {
                            const r = await publishApi.duplicateTask(t.id)
                            setDuplicatingPayload(r.payload)
                            setCreateOpen(true)
                            // 不调 load()（wizard 确认时再 load）
                          } catch (e) {
                            toast((e as Error).message, 'error')
                          } finally {
                            setBusyMap((m) => { const n = { ...m }; delete n[key]; return n })
                          }
                        }}>
                        {isBusy(t.id, 'duplicate') ? <><Spinner />复制中…</> : '复制'}
                      </Button>
                    </div>
                  </td>
                </tr>
              )
            })}
            {data.list.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-12 text-center text-muted-foreground">暂无任务，点击「创建发布任务」</td></tr>
            )}
          </tbody>
        </table>
        </div>
        <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
      </div>

      <PublishWizardDialog open={createOpen} onOpenChange={(v) => {
        setCreateOpen(v)
        if (!v) { setEditingDraftId(null); setDuplicatingPayload(null) }
      }} onCreated={load} editingTaskId={editingDraftId} initialPayload={duplicatingPayload} />
      <PublishDetailDialog open={!!detailId} onOpenChange={(v) => !v && setDetailId('')}
        taskId={detailId} onDuplicatePayload={(payload) => {
          setDuplicatingPayload(payload)
          setCreateOpen(true)
        }} />
      <ConfirmDialog
        open={!!confirmTarget}
        onOpenChange={(v) => !v && setConfirmTarget(null)}
        title={
          confirmTarget?.type === 'start' ? '启动任务'
          : confirmTarget?.type === 'pause' ? '暂停任务'
          : confirmTarget?.type === 'resume' ? '恢复任务'
          : confirmTarget?.type === 'cancel' ? '取消任务'
          : confirmTarget?.type === 'delete' ? '删除任务'
          : confirmTarget?.type === 'restart' ? '重启任务'
          : confirmTarget?.type === 'retry' ? '重试任务'
          : ''
        }
        danger={confirmTarget?.type !== 'resume'}
        content={
          confirmTarget?.type === 'start'
            ? `确定启动任务「${confirmTarget.target.task_name}」？启动后所有明细将立即派发并占用成品。`
            : confirmTarget?.type === 'pause'
            ? `确定暂停任务「${confirmTarget.target.task_name}」？暂停后已派发明细继续执行，未派发明细等待恢复。`
            : confirmTarget?.type === 'resume'
            ? `确定恢复任务「${confirmTarget.target.task_name}」？恢复后等待明细继续派发。`
            : confirmTarget?.type === 'cancel'
            ? `确定取消任务「${confirmTarget.target.task_name}」？未执行明细将取消并释放成品占用。`
            : confirmTarget?.type === 'restart'
            ? `确定重启任务「${confirmTarget.target.task_name}」？已取消/失败的明细将重新派发（成功明细不动，避免重复发布）。`
            : confirmTarget?.type === 'retry'
            ? `确定重试任务「${confirmTarget.target.task_name}」？失败明细按 plan_time 阈值分流：≥2h 复活，<2h 永久关闭。`
            : confirmTarget?.type === 'delete'
            ? (confirmTarget.target.status === 'draft'
                ? `确定删除草稿任务「${confirmTarget.target.task_name}」？删除后无法恢复。`
                : `确定删除任务「${confirmTarget.target.task_name}」？发布记录永久保留。`)
            : ''
        }
        onConfirm={async () => {
          if (!confirmTarget) return
          const ct = confirmTarget
          setConfirmTarget(null)
          if (ct.type === 'start') {
            await runAction(ct.target.id, 'start', async () => {
              const r = await publishApi.confirmTask(ct.target.id)
              toast(`已启动：${r.item_count} 条待发布`, 'success')
            })
          } else if (ct.type === 'pause') {
            await runAction(ct.target.id, 'pause', () => publishApi.toggleTask(ct.target.id, true))
          } else if (ct.type === 'resume') {
            await runAction(ct.target.id, 'resume', () => publishApi.toggleTask(ct.target.id, false))
          } else if (ct.type === 'cancel') {
            await runAction(ct.target.id, 'cancel', () => publishApi.cancelTask(ct.target.id))
          } else if (ct.type === 'restart') {
            await runAction(ct.target.id, 'restart', async () => {
              const r = await publishApi.restartTask(ct.target.id)
              toast(`已重启：${r.reset} 条复活 / ${r.dispatched} 条已派发`, 'success')
            })
          } else if (ct.type === 'retry') {
            await runAction(ct.target.id, 'retry', async () => {
              const r = await publishApi.retryTask(ct.target.id)
              toast(`已重试：${r.retried} 条复活 / ${r.closed} 条关闭`, 'success')
            })
          } else if (ct.type === 'delete') {
            await runAction(ct.target.id, 'delete', () => publishApi.deleteTask(ct.target.id))
          }
        }} />
    </div>
  )
}
