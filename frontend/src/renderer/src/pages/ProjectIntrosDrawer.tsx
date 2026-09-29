/** #415：标题/话题多行文本框（右侧抽屉，存储合并）。
 * - 一行一条；每行 = 一个完整「标题 + #话题」字符串
 * - 「保存」按钮 → 后端全量覆盖 API（items: string[]）
 * - 后端存储只用 content 字段（#415），从 content 用正则提取 #话题 返回 topics
 *
 * 并发与守卫（与 ProjectShopsDrawer 对齐）：
 * - aliveRef：unmount / 抽屉关闭后 setState/toast 守卫
 * - busyRef：连点「保存」并发锁
 * - reqId：切换项目 / 重开抽屉时丢弃在途 listIntros */
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { publishIntroApi, type VideoIntro } from '@/api/publish_intro'

interface Props {
  project: { id: string; title: string } | null
  open: boolean
  onOpenChange: (v: boolean) => void
}

/** 按 \r?\n 拆分；过滤空行（保留有内容的行） */
function parseLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

/** 行数组 → textarea 字符串（每行一条，用 \n 连接） */
function joinLines(lines: string[]): string {
  return lines.join('\n')
}

export function ProjectIntrosDrawer({ project, open, onOpenChange }: Props) {
  const [text, setText] = useState('')
  const [savedLines, setSavedLines] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  /** 关闭前如有改动，弹页面内 confirm */
  const [confirmClose, setConfirmClose] = useState(false)
  /** dirty 标记：文本框内容相对上次保存是否改动 */
  const dirtyRef = useRef(false)
  /** 全局操作锁（同步）：防止连点保存导致并发 setProjectIntros */
  const busyRef = useRef(false)
  /** 请求序号：切换项目 / 重开抽屉时丢弃在途 listIntros */
  const reqIdRef = useRef(0)
  /** unmount / 抽屉关闭守卫 */
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  // 打开时：拉已有 intros → 回显到 textarea
  useEffect(() => {
    if (!open || !project) return
    const projectId = project.id
    const reqId = ++reqIdRef.current
    let alive = true
    publishIntroApi.listIntros(projectId).then((rows: VideoIntro[]) => {
      if (!alive || reqId !== reqIdRef.current || !aliveRef.current) return
      const lines = rows.map((r) => r.content)
      setSavedLines(lines)
      setText(joinLines(lines))
      dirtyRef.current = false
    }).catch((e) => {
      if (alive && reqId === reqIdRef.current && aliveRef.current) {
        toast((e as Error).message, 'error')
      }
    })
    return () => { alive = false }
  }, [open, project])

  /** textarea onChange：更新文本 + dirty 标记 */
  const handleChange = (v: string) => {
    dirtyRef.current = true
    setText(v)
  }

  /** 保存：按行解析 → 全量覆盖（带并发锁）。
   *  #415：文本框为空 = 空数组 = 全删；不再二次确认，用户点保存即终态。 */
  const save = async () => {
    if (busyRef.current) return
    if (!project) return
    const lines = parseLines(text)
    busyRef.current = true
    setBusy(true)
    try {
      const r = await publishIntroApi.setProjectIntros(project.id, lines)
      if (!aliveRef.current) return
      // 重新拉一次作为权威数据，并写回文本框
      const reqId = ++reqIdRef.current
      publishIntroApi.listIntros(project.id).then((rows: VideoIntro[]) => {
        if (!aliveRef.current || reqId !== reqIdRef.current) return
        const fresh = rows.map((rw) => rw.content)
        setSavedLines(fresh)
        setText(joinLines(fresh))
        dirtyRef.current = false
      })
      toast(r.saved === 0 ? '已清空全部标题/话题' : `已保存 ${r.saved} 条`, 'success')
    } catch (e) {
      if (aliveRef.current) toast((e as Error).message, 'error')
    } finally {
      busyRef.current = false
      if (aliveRef.current) setBusy(false)
    }
  }

  /** 关闭前如有改动，弹页面内 confirm */
  const handleOpenChange = (v: boolean) => {
    if (!v && dirtyRef.current) {
      setConfirmClose(true)
      return
    }
    onOpenChange(v)
  }

  /** 当前编辑非空行数（实时派生） */
  const editingLineCount = parseLines(text).length

  return (
    <>
    <Dialog open={open} onOpenChange={handleOpenChange} side="right" width={560}
      title={project ? `标题/话题 · ${project.title}` : '标题/话题'}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)}>关闭</Button>
          <Button size="sm" disabled={busy} onClick={save}>
            {busy ? '保存中…' : '保存'}
          </Button>
        </>
      }>
      <div className="space-y-3">
        <div className="text-xs text-muted-foreground">
          每条独立一行（Enter 换行）。话题用 <code className="text-accent">#话题</code> 标注，将自动被识别。
        </div>
        <textarea
          value={text}
          onChange={(e) => handleChange(e.target.value)}
          placeholder={'例：\n门店 #美食 #团购\n门店 #美食 #团购'}
          className="min-h-[55vh] w-full resize-y rounded-md border border-border bg-white p-3 font-mono text-sm leading-7 focus:outline-none focus:ring-2 focus:ring-accent/30"
        />
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>已保存 {savedLines.length} 条 / 当前编辑 {editingLineCount} 行</span>
          <span>{dirtyRef.current ? '● 有未保存改动' : '— 无改动'}</span>
        </div>
      </div>
    </Dialog>
    {/* 二次确认：关闭抽屉前有未保存改动 */}
    <ConfirmDialog
      open={confirmClose}
      onOpenChange={(v) => !v && setConfirmClose(false)}
      title="放弃未保存的改动？"
      content="当前编辑未保存，关闭抽屉将丢失改动。"
      danger
      onConfirm={() => {
        dirtyRef.current = false
        setConfirmClose(false)
        onOpenChange(false)
      }}
    />
    </>
  )
}