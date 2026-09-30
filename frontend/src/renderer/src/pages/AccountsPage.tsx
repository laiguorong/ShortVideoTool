import { useCallback, useEffect, useRef, useState } from 'react'
import { accountApi, type Account } from '@/api/account'
import { fileUrl } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { Pagination } from '@/components/shared/Pagination'
import { toast } from '@/components/ui/toast'

/** 账号头像：DB 存 data 相对路径，经 /api/files/ 读取；加载失败回退首字母圆标 */
function Avatar({ account }: { account: Account }) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (account.avatar) {
      fileUrl(account.avatar).then((u) => alive && setSrc(u))
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [account.avatar])
  if (src) {
    return <img src={src} alt="" className="h-8 w-8 rounded-full object-cover"
                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }} />
  }
  return (
    <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-xs text-muted-foreground">
      {(account.nickname || '?').slice(0, 1)}
    </div>
  )
}

/** 添加账号弹窗（浏览器登录模式：点按钮拉起登录窗，登录成功自动抓取会话 + 自动添加） */
function AddAccountDialog({ open, onOpenChange, onAdded }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onAdded: () => void
}) {
  const [loading, setLoading] = useState(false)
  // Cookie 串 + 登录窗 evaluate 拿到的 profile（不落组件状态，避免渲染到 DOM）
  const cookieRef = useRef('')
  const profileRef = useRef<{ nickname: string; douyin_id: string; avatar: string }>({
    nickname: '', douyin_id: '', avatar: '',
  })
  // #fix-profile-dir-move：登录窗临时 chromium profile 路径，add 时回传给后端搬移
  const profileDirRef = useRef('')
  const submittedRef = useRef(false)  // 防止重复提交

  // 弹窗打开时重置状态
  useEffect(() => {
    if (open) {
      submittedRef.current = false
      cookieRef.current = ''
      profileRef.current = { nickname: '', douyin_id: '', avatar: '' }
      profileDirRef.current = ''
    }
  }, [open])

  /** 拿到 cookie 后自动提交（备注名留空，后端 add_account 用昵称兜底） */
  const autoSubmit = async (
    cookie: string,
    profile: { nickname: string; douyin_id: string; avatar: string },
    profileDir: string,
  ) => {
    if (submittedRef.current) return
    submittedRef.current = true
    setLoading(true)
    try {
      // #136：备注名传空——后端 remark_name = remark or nickname 自动用昵称兜底
      // #fix-profile-dir-move：把登录窗临时 profile_dir 一并传给后端，
      // 后端负责搬移到 accounts/{id}/profile/（否则后续发布用空 profile 失败）
      await accountApi.add(cookie, '', profile, profileDir || undefined)
      toast('账号添加成功', 'success')
      cookieRef.current = ''
      onOpenChange(false)
      onAdded()
    } catch (e) {
      // 提交失败：允许重试
      submittedRef.current = false
      toast((e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }

  /** 拉起登录窗：登录成功自动抓取会话 + 自动添加（无 account_id，添加场景） */
  const startLogin = async () => {
    setLoading(true)
    try {
      const r = await accountApi.openLogin()
      if (!r.cookie) {
        toast('已取消登录或超时', 'info')
        return
      }
      cookieRef.current = r.cookie
      profileRef.current = { nickname: r.nickname, douyin_id: r.douyin_id, avatar: r.avatar }
      profileDirRef.current = r.profile_dir ?? ''
      toast('登录成功，正在添加账号…', 'success')
      // 拿到 cookie 立即自动提交，无需点确认
      await autoSubmit(r.cookie, profileRef.current, profileDirRef.current)
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      if (!submittedRef.current) setLoading(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="添加账号（浏览器登录）"
      footer={
        <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
      }
    >
      <div className="space-y-3">
        <div className="rounded-md border border-border bg-muted/40 p-3 text-sm">
          {loading ? (
            <span className="text-muted-foreground">
              {cookieRef.current ? '正在添加账号…' : '等待浏览器登录…'}
            </span>
          ) : (
            <span className="text-muted-foreground">
              点击下方按钮打开平台登录页（支持扫码/手机号登录），
              登录成功后自动抓取会话并添加账号。
            </span>
          )}
        </div>
        <Button className="w-full" disabled={loading} onClick={startLogin}>
          {loading ? '处理中…' : '打开平台登录页'}
        </Button>
      </div>
    </Dialog>
  )
}

/** 重新登录弹窗（浏览器登录抓会话，与添加账号同方式）
 *  登录成功自动提交 + 自动关闭，无需手动点确认 */
function ReloginDialog({ account, open, onOpenChange, onDone }: {
  account: Account | null
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [loading, setLoading] = useState(false)
  const cookieRef = useRef('')
  const profileRef = useRef<{ nickname: string; douyin_id: string; avatar: string }>({
    nickname: '', douyin_id: '', avatar: '',
  })
  const submittedRef = useRef(false)  // 防止 gotCookie 副作用 + startLogin 重复触发提交

  useEffect(() => {
    if (open) {
      submittedRef.current = false
      cookieRef.current = ''
      profileRef.current = { nickname: '', douyin_id: '', avatar: '' }
    }
  }, [open])

  /** 拿到 cookie 后自动提交 + 关闭（用户无感） */
  const autoSubmit = async (cookie: string, profile: { nickname: string; douyin_id: string; avatar: string }) => {
    if (submittedRef.current) return
    submittedRef.current = true
    setLoading(true)
    try {
      await accountApi.relogin(account!.id, cookie, profile)
      toast('重新登录成功，状态已更新', 'success')
      cookieRef.current = ''
      onOpenChange(false)
      onDone()
    } catch (e) {
      // 提交失败：允许用户重试
      submittedRef.current = false
      toast((e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }

  const startLogin = async () => {
    if (!account) return
    setLoading(true)
    try {
      const r = await accountApi.openLogin(account.id)  // 传 account_id → storage.json 落账号目录
      if (!r.cookie) {
        toast('已取消登录或超时', 'info')
        return
      }
      cookieRef.current = r.cookie
      profileRef.current = { nickname: r.nickname, douyin_id: r.douyin_id, avatar: r.avatar }
      toast('登录成功，会话已抓取', 'success')
      // 拿到 cookie 立即自动提交，无需点确认
      await autoSubmit(r.cookie, profileRef.current)
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      // autoSubmit 内部已 setLoading(false)，避免覆盖；此处仅清 startLogin 的 loading
      if (!submittedRef.current) setLoading(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`重新登录：${account?.remark || account?.nickname || ''}`}
      footer={
        <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
      }
    >
      <div className="space-y-3">
        <div className="rounded-md border border-border bg-muted/40 p-3 text-sm">
          {loading ? (
            <span className="text-muted-foreground">
              {cookieRef.current ? '正在保存会话…' : '等待浏览器登录…'}
            </span>
          ) : (
            <span className="text-muted-foreground">
              请使用该账号绑定的手机号/扫码登录（登录其他账号会被校验拒绝）。
            </span>
          )}
        </div>
        <Button className="w-full" disabled={loading} onClick={startLogin}>
          {loading ? '处理中…' : '打开平台登录页'}
        </Button>
      </div>
    </Dialog>
  )
}

/** 编辑备注名弹窗（Electron 无 window.prompt，改为应用内 Dialog） */
function EditRemarkDialog({ account, open, onOpenChange, onDone }: {
  account: Account | null
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [remark, setRemark] = useState('')

  // 弹窗每次打开时，用当前账号备注初始化输入框
  useEffect(() => {
    if (open) setRemark(account?.remark ?? '')
  }, [open, account])

  const submit = async () => {
    if (!account) return
    try {
      await accountApi.updateRemark(account.id, remark.trim())
      toast('备注名已更新', 'success')
      onOpenChange(false)
      onDone()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`编辑备注名：${account?.nickname || account?.douyin_id || ''}`}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" onClick={submit}>保存</Button>
        </>
      }
    >
      <input
        className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm focus:border-accent focus:outline-none"
        placeholder="备注名（留空则清除）"
        value={remark}
        onChange={(e) => setRemark(e.target.value)}
        autoFocus
      />
    </Dialog>
  )
}

/** 账号管理页（界面交互设计 9.3.1） */
export function AccountsPage() {
  const [data, setData] = useState<{ list: Account[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('')
  const [keyword, setKeyword] = useState('')
  const [loading, setLoading] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [reloginTarget, setReloginTarget] = useState<Account | null>(null)
  const [editTarget, setEditTarget] = useState<Account | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  // 正在检测的账号 ID 集合（多个账号并发检测时单独 disabled）
  const [checkingIds, setCheckingIds] = useState<Set<string>>(new Set())

  const load = useCallback(async () => {
    try {
      const r = await accountApi.list({ status, keyword, page, page_size: pageSize })
      setData({ list: r.list, total: r.total })
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }, [status, keyword, page, pageSize])

  /** 按钮点击触发：同步 setLoading(true) 后再调 load，避免 React 18 把 true/false 合并成一次渲染 */
  const doRefresh = () => {
    setLoading(true)
    void load()
  }

  useEffect(() => {
    doRefresh()
  }, [load])

  const doToggle = async (acc: Account) => {
    try {
      await accountApi.toggleDisable(acc.id, acc.status !== 'disabled')
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  // 状态展示映射：normal=已登录；disabled=已停用；其余（invalid/undetected）=登录失败
  const isLoginFailed = (acc: Account) => acc.status === 'invalid' || acc.status === 'undetected'

  /** 手动检测单个账号的会话有效性。
   *  检测到 valid 时后端会把 status 回写为 normal，前端 load() 刷新即生效。
   *  失败状态账号检测后转 normal 时，给出"已恢复"友好提示。
   */
  const doCheck = async (acc: Account) => {
    const wasLoginFailed = isLoginFailed(acc)
    setCheckingIds((s) => new Set(s).add(acc.id))
    try {
      const r = await accountApi.check(acc.id)
      const valid = r.status === 'normal'
      if (valid) {
        toast(
          wasLoginFailed ? `账号已恢复正常：${r.message}` : `账号有效：${r.message}`,
          'success',
        )
      } else if (r.auto_relogin_started) {
        toast('Cookie 已失效，已自动打开浏览器，请在浏览器中完成登录', 'error')
      } else if (r.message?.includes('已检测过')) {
        toast(r.message, 'info')
      } else {
        toast(`已失效：${r.message}`, 'error')
      }
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setCheckingIds((s) => { const n = new Set(s); n.delete(acc.id); return n })
    }
  }

  const doDelete = async () => {
    if (!deleteTarget) return
    try {
      await accountApi.remove(deleteTarget.id)
      toast('账号已删除，历史记录保留', 'success')
      load()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="flex h-full flex-col p-5">
      {/* 工具条（标题 + 筛选 + 刷新 + 右侧操作） */}
      <div className="mb-4 flex items-center gap-3">
        <h1 className="flex h-8 items-center text-lg font-semibold">账号管理</h1>
        <select
          className="ml-4 h-8 rounded-md border border-border bg-white px-2 text-sm"
          value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1) }}
        >
          <option value="">全部状态</option>
          <option value="normal">已登录</option>
          <option value="invalid">登录失败</option>
          <option value="disabled">已停用</option>
        </select>
        <input
          className="h-8 w-48 rounded-md border border-border bg-white px-2.5 text-sm"
          placeholder="搜索备注/昵称/平台号"
          value={keyword}
          onChange={(e) => { setKeyword(e.target.value); setPage(1) }}
        />
        <Button size="sm" variant="outline" onClick={doRefresh} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </Button>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => setAddOpen(true)}>添加账号</Button>
        </div>
      </div>

      {/* 表格（表格区滚动 + 翻页器固定底部） */}
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">平台账号</th>
              <th className="px-4 py-2.5 font-medium">平台号</th>
              <th className="px-4 py-2.5 font-medium">备注名</th>
              <th className="px-4 py-2.5 font-medium">状态</th>
              <th className="px-4 py-2.5 font-medium">最近登录时间</th>
              <th className="px-4 py-2.5 font-medium">创建时间</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((acc) => (
              <tr
                key={acc.id}
                className={`border-b border-border last:border-0 hover:bg-muted/50 ${isLoginFailed(acc) ? 'bg-red-50/50' : ''}`}
              >
                {/* 头像 + 昵称 */}
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-2.5">
                    <Avatar account={acc} />
                    <span className="font-medium">{acc.nickname || '-'}</span>
                  </div>
                </td>
                <td className="px-4 py-2.5 font-mono text-xs">{acc.douyin_id}</td>
                {/* 备注名：空显示"设置备注"按钮，非空显示文本+编辑图标 */}
                <td className="px-4 py-2.5">
                  {acc.remark ? (
                    <button
                      className="group flex items-center gap-1.5 text-left hover:text-accent"
                      onClick={() => setEditTarget(acc)}
                      title="修改备注名"
                    >
                      <span>{acc.remark}</span>
                      <span className="text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">✎</span>
                    </button>
                  ) : (
                    <button
                      className="text-muted-foreground underline-offset-2 hover:text-accent hover:underline"
                      onClick={() => setEditTarget(acc)}
                    >
                      设置备注
                    </button>
                  )}
                </td>
                {/* 状态：正常带"检测"；失败带"检测 + 重新登录"；停用不带操作按钮。
                    失败也提供"检测"：cookie 可能在外被改回 / 临时恢复，先检测能省一次重新登录 */}
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-2">
                    {acc.status === 'disabled' ? (
                      <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">已停用</span>
                    ) : acc.status === 'normal' ? (
                      <>
                        <span className="inline-flex items-center rounded-full bg-ok/10 px-2 py-0.5 text-xs text-ok">已登录</span>
                        <button
                          className="text-xs text-muted-foreground hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
                          disabled={checkingIds.has(acc.id)}
                          onClick={() => doCheck(acc)}
                          title="检测账号登录态（失效会自动弹重新登录）"
                        >
                          {checkingIds.has(acc.id) ? '检测中…' : '检测'}
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="inline-flex items-center rounded-full bg-danger/10 px-2 py-0.5 text-xs text-danger">登录失败</span>
                        <button
                          className="text-xs text-muted-foreground hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
                          disabled={checkingIds.has(acc.id)}
                          onClick={() => doCheck(acc)}
                          title="检测账号登录态，cookie 若已恢复会自动转回「已登录」"
                        >
                          {checkingIds.has(acc.id) ? '检测中…' : '检测'}
                        </button>
                        <button
                          className="text-xs text-accent hover:underline"
                          onClick={() => setReloginTarget(acc)}
                        >
                          重新登录
                        </button>
                      </>
                    )}
                  </div>
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{acc.last_login_time || '-'}</td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">{acc.create_time}</td>
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-1">
                    <Button variant="outline" size="sm" onClick={() => doToggle(acc)}>
                      {acc.status === 'disabled' ? '恢复' : '停用'}
                    </Button>
                    <Button variant="outline" size="sm" className="!text-danger" onClick={() => setDeleteTarget(acc)}>
                      删除
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {!loading && data.list.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-12 text-center text-muted-foreground">
                  暂无账号，点击右上「添加账号」开始
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
        <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
      </div>

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onAdded={load} />
      <ReloginDialog account={reloginTarget} open={!!reloginTarget} onOpenChange={(v) => !v && setReloginTarget(null)} onDone={load} />
      <EditRemarkDialog account={editTarget} open={!!editTarget} onOpenChange={(v) => !v && setEditTarget(null)} onDone={load} />
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(v) => !v && setDeleteTarget(null)}
        title="删除账号"
        danger
        content={`确定删除账号「${deleteTarget?.remark || deleteTarget?.nickname}」？其待执行发布明细将被取消，历史发布记录与统计数据保留。`}
        onConfirm={doDelete}
      />
    </div>
  )
}
