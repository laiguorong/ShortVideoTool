import { useCallback, useEffect, useState } from 'react'
import { settingApi, taskApi, type AppSettings, type HealthCheckResult } from '@/api/setting'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ConfirmDialog } from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { Pagination } from '@/components/shared/Pagination'
import { useReloadOnVisible } from '@/hooks/useReloadOnVisible'
import { useAppStore } from '@/store/useAppStore'

/** 设置页（界面交互设计 9.3.8）：Tab 分区 */
const TABS = ['通用', '任务队列', '数据目录', '备份恢复', '通知中心'] as const
type Tab = (typeof TABS)[number]

// #data-dir-choice：默认数据目录探测结果类型 + 加载 helper
// 抽成 module-level：未来"恢复默认"按钮复用，无需在 DataDirTab 内复制异步逻辑
type DefaultDataDirHint = { disk: string; abs_path: string }
async function loadDefaultDataDirHint(): Promise<DefaultDataDirHint | null> {
  try {
    return (await window.electronAPI?.defaultDataDirHint()) ?? null
  } catch {
    return null
  }
}

/** 通用配置 Tab */
function GeneralTab() {
  const [settings, setSettings] = useState<AppSettings | null>(null)

  useEffect(() => {
    settingApi.get().then(setSettings).catch((e) => toast((e as Error).message, 'error'))
  }, [])

  if (!settings) return <div className="p-4 text-sm text-muted-foreground">加载中…</div>

  /** 保存单个配置项（热加载） */
  const save = async (updates: Partial<AppSettings>) => {
    try {
      const saved = await settingApi.update(updates)
      setSettings(saved)
      toast('已保存并生效', 'success')
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="max-w-xl space-y-5 p-5">
      {([
        ['account_check_hours', '账号登录态检测周期（小时）', 1, 24],
        ['log_retention_days', '日志保留天数', 7, 90],
        ['temp_retention_days', '缓存清理阈值（天）', 1, 90],
        ['pull_base_pages', '视频拉取每轮基础页数（v22 阶梯）', 10, 500],
      ] as const).map(([key, label, min, max]) => (
        <div key={key} className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">{label}</div>
            {key === 'pull_base_pages' && (
              <div className="mt-0.5 text-xs text-muted-foreground">
                第 1 轮 = 此值；之后每轮减 10 页，最低 10 页兜底
              </div>
            )}
          </div>
          <input
            type="number"
            className="h-8 w-28 rounded-md border border-border bg-white px-2 text-sm"
            defaultValue={settings[key] as number}
            min={min}
            max={max}
            onBlur={(e) => {
              const v = Number(e.target.value)
              if (v !== settings[key] && Number.isFinite(v)) save({ [key]: v } as Partial<AppSettings>)
            }}
          />
        </div>
      ))}

      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-medium">显示浏览器窗口（调试）</div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            开启后视频详情/门店拉取/账号检测补抓会显示浏览器窗口；账号登录窗固定显示不受影响
          </div>
        </div>
        <input
          type="checkbox"
          className="h-4 w-4"
          checked={!!settings.browser_show_window}
          onChange={(e) => save({ browser_show_window: e.target.checked })}
        />
      </div>
    </div>
  )
}

/** 任务队列 Tab */
function TaskQueueTab() {
  const [tasks, setTasks] = useState<ReturnType<typeof taskApi.list> extends Promise<infer T> ? T : never>([] as never)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const load = () => taskApi.list().then(setTasks).catch((e) => console.warn('[Settings] 请求失败:', e))
  useEffect(() => {
    load()
    const t = window.setInterval(load, 3000)
    return () => window.clearInterval(t)
  }, [])

  // 当前页数据（#79：空态按「当前页」判断，避免页码越界时表体空白且无提示）
  const pagedTasks = tasks.slice((page - 1) * pageSize, page * pageSize)

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-3 flex shrink-0 items-center gap-3">
        <h2 className="text-sm font-semibold">后台任务（3 秒自动刷新）</h2>
      </div>
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2 font-medium">类型</th>
              <th className="px-4 py-2 font-medium">名称</th>
              <th className="px-4 py-2 font-medium">状态</th>
              <th className="px-4 py-2 font-medium">进度</th>
              <th className="px-4 py-2 font-medium">信息</th>
              <th className="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {pagedTasks.map((t) => (
              <tr key={t.task_id} className="border-b border-border last:border-0">
                <td className="px-4 py-2">{t.type_label}</td>
                <td className="px-4 py-2">{t.name}</td>
                <td className="px-4 py-2"><StatusBadge status={t.status} /></td>
                <td className="px-4 py-2">{t.progress || '-'}</td>
                <td className="max-w-52 truncate px-4 py-2 text-xs text-muted-foreground" title={t.message}>{t.message || '-'}</td>
                <td className="px-4 py-2">
                  {(t.status === 'waiting' || t.status === 'running') && (
                    <Button variant="outline" size="sm" className="!text-danger"
                      onClick={() => taskApi.cancel(t.task_id).then(load).catch((e) => toast((e as Error).message, 'error'))}>
                      取消
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {pagedTasks.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-10 text-center text-muted-foreground">暂无后台任务</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4"><Pagination page={page} pageSize={pageSize} total={tasks.length} onChange={setPage}
        onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
    </div>
  )
}

/** 数据目录 Tab（健康检查 + 清理） */
function DataDirTab() {
  const [health, setHealth] = useState<HealthCheckResult | null>(null)
  const [dataDir, setDataDir] = useState('')
  const [faceEvidenceDir, setFaceEvidenceDir] = useState('')
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [preferredDir, setPreferredDir] = useState<string | null>(null)
  const [defaultHint, setDefaultHint] = useState<DefaultDataDirHint | null>(null)
  const [relaunchOpen, setRelaunchOpen] = useState(false)

  // #data-dir-choice：mount + 窗口回到前台时 reload（debounce + visible 守卫）
const load = useCallback(() => {
    settingApi.healthCheck().then(setHealth).catch((e) => console.warn('[Settings] 请求失败:', e))
    settingApi.dataDir().then((r) => setDataDir(r.data_dir)).catch((e) => console.warn('[Settings] 请求失败:', e))
    settingApi.faceEvidenceDir().then((r) => setFaceEvidenceDir(r.face_evidence_dir)).catch((e) => console.warn('[Settings] 请求失败:', e))
    window.electronAPI?.getPreferredDataDir().then((r) => setPreferredDir(r?.abs_path ?? null)).catch(() => {})
    loadDefaultDataDirHint().then(setDefaultHint)
  }, [])
  useReloadOnVisible(load)

  const doCleanup = async () => {
    try {
      const r = await settingApi.cleanup()
      // #511：有失败文件（loguru 占用今天的日志 / 权限不足）要单独提示
      const tail = r.failed_files > 0 ? `（${r.failed_files} 个跳过：文件被占用）` : ''
      toast(`已清理 ${r.deleted_files} 个文件，释放 ${r.freed_mb} MB${tail}`, 'success')
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  // #data-dir-choice：更改数据目录 → 写 userData → 弹重启确认 dialog
  const chooseDataDir = async () => {
    if (!window.electronAPI) {
      toast('当前环境不支持更改数据目录', 'error')
      return
    }
    try {
      const r = await window.electronAPI.chooseDataDir()
      if (!r) return  // 用户取消选目录框
      setPreferredDir(r.abs_path)
      setRelaunchOpen(true)  // 弹单一重启确认 dialog（不再额外 toast）
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const doRelaunch = () => {
    setRelaunchOpen(false)
    window.electronAPI?.relaunch()
  }

  return (
    <div className="max-w-2xl space-y-5 p-5">
      <div>
        <div className="text-sm font-medium">数据目录</div>
        <div className="mt-1 text-xs text-muted-foreground">
          数据库 / 素材 / 成品视频 / 账号登录态等全部本地数据。首次启动时已选定。
          {defaultHint && ` 默认目录：${defaultHint.abs_path} (${defaultHint.disk} 盘空间最大) `}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <code className="flex-1 rounded-md bg-muted px-3 py-2 text-xs">{dataDir}</code>
          {window.electronAPI && dataDir && (
            <Button variant="outline" size="sm" onClick={() => window.electronAPI!.openPath(dataDir)}>打开目录</Button>
          )}
          {window.electronAPI && (
            <Button variant="outline" size="sm" onClick={chooseDataDir}>更改目录</Button>
          )}
        </div>
        {preferredDir && preferredDir !== dataDir && (
          <div className="mt-1 text-xs text-amber-600">
            已选择新目录：{preferredDir}（重启后切换）
          </div>
        )}
      </div>

      <div>
        <div className="text-sm font-medium">人脸证据目录</div>
        <div className="mt-1 text-xs text-muted-foreground">
          人脸检测命中帧证据（任务 #129 复盘修复）。误判/漏判复检时打开查看；目录在 cache/ 下，会随缓存清理被删除。
        </div>
        <div className="mt-1 flex items-center gap-2">
          <code className="flex-1 rounded-md bg-muted px-3 py-2 text-xs">{faceEvidenceDir || '加载中...'}</code>
          {window.electronAPI && faceEvidenceDir && (
            <Button variant="outline" size="sm" onClick={() => window.electronAPI!.openPath(faceEvidenceDir)}>打开目录</Button>
          )}
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm font-medium">健康检查</div>
          <Button variant="outline" size="sm" onClick={load}>重新检查</Button>
        </div>
        {health ? (
          <>
            <div className="flex flex-wrap gap-2">
              {health.items.map((i) => (
                <Badge key={i.dir} variant={i.exists && i.writable ? 'success' : 'danger'}>
                  {i.exists && i.writable ? '✅' : '❌'} {i.dir}
                </Badge>
              ))}
            </div>
            <div className="mt-3 text-xs text-muted-foreground">
              磁盘剩余：{health.free_gb} GB
              {health.warnings.map((w) => (
                <div key={w} className="mt-1 text-warn">⚠ {w}</div>
              ))}
            </div>
          </>
        ) : (
          <div className="text-sm text-muted-foreground">检查中…</div>
        )}
      </div>

      <div className="flex items-center justify-between rounded-lg border border-border p-4">
        <div>
          <div className="text-sm font-medium">清理 temp 与 cache</div>
          <div className="mt-0.5 text-xs text-muted-foreground">不触及素材/成品/数据库/日志</div>
        </div>
        <Button variant="outline" size="sm" onClick={() => setCleanupOpen(true)}>清理</Button>
      </div>

      <ConfirmDialog
        open={cleanupOpen}
        onOpenChange={setCleanupOpen}
        title="清理缓存"
        content="确定清理 temp 与 cache 目录中的全部文件？该操作不可恢复。"
        danger
        onConfirm={doCleanup}
      />

      {/* #data-dir-choice：更改数据目录后询问是否立即重启 */}
      <ConfirmDialog
        open={relaunchOpen}
        onOpenChange={setRelaunchOpen}
        title="立即重启以生效新目录？"
        content="新数据目录需要重启应用后才会生效。"
        onConfirm={doRelaunch}
      />
    </div>
  )
}

/** 备份恢复 Tab（第一版：db+config zip） */
function BackupTab() {
  const [busy, setBusy] = useState(false)

  const doBackup = async () => {
    if (!window.electronAPI) return
    const path = await window.electronAPI.saveFile(`backup_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.zip`)
    if (!path) return
    setBusy(true)
    try {
      const r = await settingApi.backup({ target_path: path })
      toast(`备份完成（${r.size_mb} MB）`, 'success')
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-xl space-y-4 p-5">
      <div className="flex items-center justify-between rounded-lg border border-border p-4">
        <div>
          <div className="text-sm font-medium">备份（数据库 + 配置）</div>
          <div className="mt-0.5 text-xs text-muted-foreground">密钥文件不进备份（本机派生）；恢复后需重启应用</div>
        </div>
        <Button size="sm" disabled={busy || !window.electronAPI} onClick={doBackup}>
          {busy ? '备份中…' : '立即备份'}
        </Button>
      </div>
      <div className="text-xs text-muted-foreground">
        恢复入口：当前版本请手动将备份 zip 中 db/ 与 config/ 解压覆盖至数据目录（完整恢复向导于 M7 提供）。
      </div>
    </div>
  )
}

/** 通知中心 Tab（铃铛点击也进入此页） */
function NotificationsTab() {
  const { notifications, setNotifications } = useAppStore()

  const load = () =>
    settingApi.notifications().then((r) => setNotifications(r.list, r.unread_count)).catch((e) => console.warn('[Settings] 请求失败:', e))
  useEffect(() => {
    load()
  }, [])

  return (
    <div className="p-5">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-sm font-semibold">通知中心</h2>
        <Button variant="outline" size="sm"
          onClick={() => settingApi.markRead(undefined, true).then(load)}>
          全部已读
        </Button>
      </div>
      <div className="space-y-2">
        {notifications.map((n) => (
          <div
            key={n.id}
            className={`rounded-lg border p-3 ${n.is_read ? 'border-border bg-background-elev' : 'border-accent/30 bg-blue-50/40'}`}
          >
            <div className="flex items-center gap-2">
              <Badge variant={n.level === 'warn' ? 'warning' : n.level === 'error' ? 'danger' : 'info'}>
                {n.category}
              </Badge>
              <span className="text-sm font-medium">{n.title}</span>
              {!n.is_read && <span className="h-1.5 w-1.5 rounded-full bg-accent" />}
              <span className="ml-auto text-xs text-muted-foreground">{n.create_time}</span>
            </div>
            {n.content && <div className="mt-1 text-xs text-muted-foreground">{n.content}</div>}
          </div>
        ))}
        {notifications.length === 0 && (
          <div className="py-12 text-center text-sm text-muted-foreground">暂无通知</div>
        )}
      </div>
    </div>
  )
}

export function SettingsPage() {
  const [tab, setTab] = useState<Tab>('通用')
  return (
    <div>
      <div className="border-b border-border bg-background-elev px-5 pt-4">
        <div className="flex gap-5">
          {TABS.map((t) => (
            <button
              key={t}
              className={`border-b-2 pb-2.5 text-sm ${tab === t ? 'border-accent font-medium text-accent' : 'border-transparent text-muted-foreground hover:text-foreground'}`}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </div>
      </div>
      {tab === '通用' && <GeneralTab />}
      {tab === '任务队列' && <TaskQueueTab />}
      {tab === '数据目录' && <DataDirTab />}
      {tab === '备份恢复' && <BackupTab />}
      {tab === '通知中心' && <NotificationsTab />}
    </div>
  )
}
