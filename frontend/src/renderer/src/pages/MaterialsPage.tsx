import { useCallback, useEffect, useRef, useState } from 'react'
import { materialApi, type Category, type Material, type VideoPullTask } from '@/api/material'
import { fileUrl, request } from '@/api/client'
import { Button } from '@/components/ui/button'
import { PullTaskDialog, PullTaskPanel } from '@/components/shared/PullTaskManager'
import { Dialog, ConfirmDialog } from '@/components/ui/dialog'
import { Pagination } from '@/components/shared/Pagination'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { toast } from '@/components/ui/toast'
import { usePrompt } from '@/components/ui/prompt'

// 系统固定"未分类"分类 ID（虚拟节点，后端 list API 注入；不落库）
const UNCATEGORIZED_ID = '-'
import { cn } from '@/lib/utils'

/** 分类树节点（前端组装，含完整路径） */
interface TreeNode extends Category {
  children: TreeNode[]
  /** 从根到当前的名称路径（如「视频/探店/火锅」的「探店/火锅」） */
  path: string
}

/** 平铺分类组树（同时算出层级路径） */
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

/** 在树中查节点与父节点：返回 [node, parentNode]（父为 null=根级） */
function findNodeAndParent(
  nodes: TreeNode[], id: string, parent: TreeNode | null,
): [TreeNode, TreeNode | null] | null {
  for (const n of nodes) {
    if (n.id === id) return [n, parent]
    const hit = findNodeAndParent(n.children, id, n)
    if (hit) return hit
  }
  return null
}

/** 分类树节点行（常规=点击筛选；管理模式=显示 修改/删除/上移/下移/左移/右移 图标） */
function TreeItem({ node, depth, selectedId, onSelect, collapsed, toggle, manage, onRename, onDelete, onShift }: {
  node: TreeNode
  depth: number
  selectedId: string
  onSelect: (id: string) => void
  collapsed: Set<string>
  toggle: (id: string) => void
  /** 分类管理模式：显示操作图标 */
  manage?: boolean
  /** 重命名回调（弹输入框） */
  onRename: (node: TreeNode) => void
  /** 删除回调（弹二次确认） */
  onDelete: (node: TreeNode) => void
  /** 平级/层级移动：up/down=上下移排序，out=左移升一级，in=右移降一级（挂到上一兄弟下） */
  onShift: (node: TreeNode, dir: 'up' | 'down' | 'out' | 'in') => void
}) {
  const hasChildren = node.children.length > 0
  const isCollapsed = collapsed.has(node.id)
  /** 管理模式操作图标按钮 */
  const IconBtn = ({ label, glyph, disabled, onClick }: { label: string; glyph: string; disabled?: boolean; onClick: () => void }) => (
    <button className={cn('rounded px-0.5 text-[11px] leading-none',
      disabled ? 'cursor-not-allowed text-muted-foreground/30' : 'text-muted-foreground hover:text-accent')}
      title={label} disabled={disabled} onClick={onClick}>{glyph}</button>
  )
  return (
    <div>
      <div
        className={cn(
          'group flex items-center gap-0.5 rounded pr-1 text-sm',
          selectedId === node.id ? 'bg-blue-50 font-medium text-accent' : 'hover:bg-muted',
        )}
        style={{ paddingLeft: depth * 12 + 2 }}
      >
        {hasChildren ? (
          <button className="w-4 shrink-0 text-center text-[10px] text-muted-foreground hover:text-foreground"
            onClick={() => toggle(node.id)} title={isCollapsed ? '展开' : '收起'}>
            {isCollapsed ? '▸' : '▾'}
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <button
          className="min-w-0 flex-1 truncate py-1.5 text-left"
          title={node.path}
          onClick={() => onSelect(node.id)}
        >
          {node.name}
        </button>
        {manage && node.id !== UNCATEGORIZED_ID ? (
          /* 管理模式：修改 / 删除 / 上移 / 下移 / 左移 / 右移 */
          <span className="flex shrink-0 items-center gap-0.5">
            <IconBtn label="修改名称" glyph="✎" onClick={() => onRename(node)} />
            <IconBtn label="删除分类" glyph="✕" onClick={() => onDelete(node)} />
            <IconBtn label="上移" glyph="↑" onClick={() => onShift(node, 'up')} />
            <IconBtn label="下移" glyph="↓" onClick={() => onShift(node, 'down')} />
            <IconBtn label="左移（升一级）" glyph="←" disabled={depth === 0}
              onClick={() => onShift(node, 'out')} />
            <IconBtn label="右移（降一级）" glyph="→" onClick={() => onShift(node, 'in')} />
          </span>
        ) : (
          /* 常规模式：仅显示素材数（编辑/删除等操作集中在管理模式） */
          <span className="shrink-0 text-xs text-muted-foreground">{node.count}</span>
        )}
      </div>
      {hasChildren && !isCollapsed && (
        <div className="border-l border-border/60" style={{ marginLeft: depth * 12 + 9 }}>
          {node.children.map((c) => (
            <TreeItem key={c.id} node={c} depth={depth + 1} selectedId={selectedId}
              onSelect={onSelect} collapsed={collapsed} toggle={toggle} manage={manage}
              onRename={onRename} onDelete={onDelete} onShift={onShift} />
          ))}
        </div>
      )}
    </div>
  )
}

/** 封面图（本地缓存相对路径 → /api/files/；失败/无封面回退占位）。
 *  固定黑底 + contain 等比缩放：横屏图垂直居中、上下留白呈黑色，不裁剪画面。 */
function CoverImage({ material, className }: { material: Material; className?: string }) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (material.cover_url) {
      fileUrl(material.cover_url).then((u) => alive && setSrc(u))
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [material.cover_url])
  // 竖屏视频封面裁切铺满，横屏保持等比缩放黑底居中
  const isVertical = material.orientation !== 'horizontal'
  if (src) {
    return <img src={src} alt="" className={cn('bg-black', isVertical ? 'object-cover' : 'object-contain', className)}
      onError={() => setSrc('')} />
  }
  return (
    <div className={cn('flex flex-col items-center justify-center gap-1 bg-muted text-muted-foreground', className)}>
      {/* 图片断裂占位图标 */}
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
        <circle cx="8.5" cy="8.5" r="1.5" />
        <path d="M21 15l-5-5L5 21" />
        <path d="M3 11l7-7 4 4 3-3" />
        <line x1="4" y1="4" x2="20" y2="20" />
      </svg>
      <span className="text-[10px]">无预览</span>
    </div>
  )
}

/** 作者头像（本地缓存 → /api/files/，加载失败回退首字圆形占位） */
function AuthorAvatar({ material, size = 40 }: { material: Material; size?: number }) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (material.author_avatar) {
      fileUrl(material.author_avatar).then((u) => alive && setSrc(u))
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [material.author_avatar])
  if (src) {
    return <img src={src} alt="" width={size} height={size} className="shrink-0 rounded-full object-cover"
      onError={() => setSrc('')} />
  }
  return (
    <div className="flex shrink-0 items-center justify-center rounded-full bg-accent/10 text-sm text-accent"
      style={{ width: size, height: size }}>
      {(material.author_nickname || '?').slice(0, 1)}
    </div>
  )
}

/** 数值缩写（13242 → 1.3万） */
const fmtCount = (n: number | null | undefined) => {
  if (n == null) return '-'
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`
  return String(n)
}

/** 相对时间显示（社交平台常见格式：1秒前/1分钟前/1小时前/1天前/超30天转日期） */
function fmtRelativeTime(timeStr?: string | null): string {
  if (!timeStr) return '-'
  const t = new Date(timeStr.replace(' ', 'T')).getTime()
  if (Number.isNaN(t)) return timeStr
  const diff = Date.now() - t
  if (diff < 60 * 1000) return `${Math.max(1, Math.floor(diff / 1000))}秒前`
  if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)}分钟前`
  if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)}小时前`
  if (diff < 30 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)}天前`
  const d = new Date(t)
  return `${d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + '年'}${d.getMonth() + 1}月${d.getDate()}日`
}

/** 素材详情弹窗（含抓取出处信息；文件路径可点击打开所在目录） */
function MaterialDetailDialog({ material, open, onOpenChange, onMoveCategory, onTitleSaved }: {
  material: Material | null
  open: boolean
  onOpenChange: (v: boolean) => void
  /** 移动分类回调（弹分类选择框，父级叠窗显示） */
  onMoveCategory?: (m: Material) => void
  /** 标题编辑保存成功回调（携带新标题） */
  onTitleSaved?: (newTitle: string) => void
}) {
  if (!material) return null
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleValue, setTitleValue] = useState(material.title)
  useEffect(() => { setTitleValue(material.title) }, [material.title])
  const saveTitle = async () => {
    const v = titleValue.trim()
    if (!v || v === material.title) { setEditingTitle(false); return }
    try {
      await materialApi.update(material.id, { title: v })
      toast('标题已更新', 'success')
      setEditingTitle(false)
      onTitleSaved?.(v)
    } catch (e) { toast((e as Error).message, 'error') }
  }
  const isDouyinSource = material.source_type === 'pull' || material.source_type === 'share'
  // 音乐素材：详情字段按音乐语境展示（隐藏分辨率/互动数，标签换原声/音频字眼）
  const isMusic = material.type === 'music'
  /** 打开素材文件所在目录（Electron shell） */
  const openDir = async () => {
    if (!window.electronAPI) { toast('仅桌面端支持打开目录', 'error'); return }
    try {
      const r = await request<{ abs_path: string; exists: boolean }>(`/materials/${material.id}/abs-path`)
      if (r.exists) window.electronAPI.openPath(r.abs_path)
      else toast('文件不存在', 'error')
    } catch (e) { toast((e as Error).message, 'error') }
  }
  /** 详情字段行 */
  const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="flex items-start gap-3 py-1.5 text-sm">
      <span className="w-24 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 flex-1 break-all">{children}</span>
    </div>
  )
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="素材详情" width={640} height={620}
      footer={<Button size="sm" onClick={() => onOpenChange(false)}>关闭</Button>}>
      <div className="space-y-4">
        <div className="space-y-0.5">
          <Row label="标题">
            {editingTitle ? (
              <textarea
                className="min-h-[60px] w-full resize-y rounded-md border border-border bg-white px-2 py-1.5 text-sm"
                value={titleValue}
                onChange={(e) => setTitleValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    setTitleValue(material.title)
                    setEditingTitle(false)
                  } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    saveTitle()
                  }
                }}
                onBlur={saveTitle}
                rows={3}
                autoFocus
              />
            ) : (
              <span className="inline-flex items-start gap-1 align-top">
                <span className="break-all">{material.title}</span>
                <button className="shrink-0 text-muted-foreground/70 hover:text-accent" title="修改标题"
                  onClick={() => setEditingTitle(true)}>✎</button>
              </span>
            )}
          </Row>
          <Row label="分类">
            <span className="inline-flex items-center gap-1.5">
              {material.category_name
                || (material.category_id === UNCATEGORIZED_ID ? '未分类' : '-')}
              {onMoveCategory && (
                <button className="text-muted-foreground/70 hover:text-accent" title="移动分类"
                  onClick={() => onMoveCategory(material)}>📁</button>
              )}
            </span>
          </Row>
          <Row label="类型">{material.type === 'video' ? '视频' : '音乐'}</Row>
          <Row label="时长">{(() => {
            // 先截断取整秒再拆分秒（与播放器控件一致用 floor；round 会出现 1分52秒 vs 播放器 1:51 不一致）
            if (material.duration_ms == null) return '-'
            const s = Math.floor(material.duration_ms / 1000)
            return `${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒`
          })()}</Row>
          <Row label="大小">{material.file_size > 1048576
            ? `${(material.file_size / 1048576).toFixed(1)}MB` : `${(material.file_size / 1024).toFixed(0)}KB`}</Row>
          {/* 分辨率/横竖屏仅视频有意义，音乐隐藏 */}
          {!isMusic && (
            <Row label="分辨率">{material.resolution || '-'}（{material.orientation === 'vertical' ? '竖屏' : '横屏'}）</Row>
          )}
          <Row label="文件状态"><StatusBadge status={material.file_status} /></Row>
          <Row label="创建时间">{material.create_time}</Row>
        </div>
        {isDouyinSource && (isMusic ? (
          /* #46/#47 音乐抓取出处：独立字段集（不显示头像/平台号/封面/发布时间） */
          <div>
            <div className="mb-1 text-sm font-semibold">抓取出处</div>
            <div className="space-y-0.5 rounded-md bg-muted/60 p-3">
              <Row label="原声标题">{material.author_title || material.title || '-'}</Row>
              <Row label="原声作者">{material.author_nickname || '-'}</Row>
              <Row label="来源原声 ID">{material.source_ref || '-'}</Row>
              <Row label="原声页链接">
                {material.share_url
                  ? <a className="cursor-pointer text-accent hover:underline" title={material.share_url}
                      onClick={(e) => {
                        e.preventDefault()
                        // Electron 下走系统默认浏览器；浏览器开发态直接新窗打开
                        const url = material.share_url!
                        if (window.electronAPI) window.electronAPI.openExternal(url)
                        else window.open(url, '_blank', 'noreferrer')
                      }}>{material.share_url}</a>
                  : '-'}
              </Row>
              <Row label="音频下载直链">
                {material.download_url
                  ? <span className="block break-all font-mono text-xs text-muted-foreground">
                      {material.download_url}
                    </span>
                  : '-'}
              </Row>
              <Row label="文件 MD5">
                <span className="font-mono text-xs">{material.file_md5 || '-'}</span>
              </Row>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              注：直链带签名会过期，仅供溯源参考。
            </div>
          </div>
        ) : (
          /* 视频抓取出处：头像/平台号/互动快照 + 作者增强 + 溯源字段 */
          <div>
            <div className="mb-1 text-sm font-semibold">抓取出处</div>
            <div className="space-y-2 rounded-md bg-muted/60 p-3">
              {/* 作者行：头像 + 昵称 + 互动四数（同一行） */}
              <div className="flex items-center gap-2.5">
                <AuthorAvatar material={material} />
                <div className="min-w-0 shrink-0">
                  <div className="truncate text-sm font-medium">{material.author_nickname || '-'}</div>
                  <div className="text-xs text-muted-foreground">
                    平台号：{material.author_douyin_id || '（详情接口可补）'}
                  </div>
                </div>
                <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground">
                  {([
                    ['digg_count', '点赞', material.digg_count],
                    ['comment_count', '评论', material.comment_count],
                    ['collect_count', '收藏', material.collect_count],
                    ['share_count', '分享', material.share_count],
                  ] as const).map(([key, label, val]) => (
                    <span key={key} className="whitespace-nowrap" title={label}>
                      <span className="font-medium text-foreground">{fmtCount(val)}</span> {label}
                    </span>
                  ))}
                </div>
              </div>
              {/* v10 作者增强：简介 + 粉丝/获赞 + 主页链接（无值自动隐藏） */}
              {material.author_signature && (
                <Row label="作者简介">{material.author_signature}</Row>
              )}
              {(material.author_follower_count != null || material.author_total_favorited != null) && (
                <Row label="粉丝 / 获赞">
                  {fmtCount(material.author_follower_count)} 粉丝 · {fmtCount(material.author_total_favorited)} 获赞
                </Row>
              )}
              {material.author_sec_uid && (
                <Row label="作者主页">
                  <a className="cursor-pointer break-all text-accent hover:underline" title="点击在系统浏览器打开"
                    onClick={(e) => {
                      e.preventDefault()
                      // Electron 走系统默认浏览器；浏览器开发态直接新窗打开
                      const url = `https://www.douyin.com/user/${material.author_sec_uid}`
                      if (window.electronAPI) window.electronAPI.openExternal(url)
                      else window.open(url, '_blank', 'noreferrer')
                    }}>
                    https://www.douyin.com/user/{material.author_sec_uid.slice(0, 24)}…
                  </a>
                </Row>
              )}
              <div className="space-y-0.5">
                {material.author_title && (
                  <Row label="出处标题">{material.author_title}</Row>
                )}
                <Row label="发布时间">{material.publish_time || '-'}</Row>
                <Row label="来源视频 ID">{material.source_ref || '-'}</Row>
                <Row label="原视频链接">
                  {material.share_url
                    ? <a className="cursor-pointer text-accent hover:underline" title={material.share_url}
                        onClick={(e) => {
                          e.preventDefault()
                          // Electron 下走系统默认浏览器；浏览器开发态直接新窗打开
                          const url = material.share_url!
                          if (window.electronAPI) window.electronAPI.openExternal(url)
                          else window.open(url, '_blank', 'noreferrer')
                        }}>{material.share_url}</a>
                    : '-'}
                </Row>
                <Row label="原下载直链">
                  {material.download_url
                    ? <span className="block break-all font-mono text-xs text-muted-foreground">
                        {material.download_url}
                      </span>
                    : '-'}
                </Row>
                <Row label="文件 MD5">
                  <span className="font-mono text-xs">{material.file_md5 || '-'}</span>
                </Row>
              </div>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              注：互动数据为入库时点快照；下载直链带签名会过期，仅供溯源参考。
            </div>
          </div>
        ))}
        <div>
          <div className="mb-1 text-sm font-semibold">文件信息</div>
          <div className="space-y-0.5 rounded-md bg-muted/60 p-3">
            <Row label="文件路径">
              <span className="cursor-pointer text-accent hover:underline" title="点击打开文件所在目录"
                onClick={openDir}>{material.file_path}</span>
            </Row>
            <Row label="引用次数">{material.ref_count} 次（项目分镜/BGM）</Row>
          </div>
        </div>
      </div>
    </Dialog>
  )
}

/** 新增分类弹窗 */
function AddCategoryDialog({ open, onOpenChange, type, parentId, onCreated }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  type: 'video' | 'music'
  parentId: string
  onCreated: () => void
}) {
  const [name, setName] = useState('')
  // 父分类为"未分类"虚拟节点时禁止在其下创建子分类
  const blocked = parentId === UNCATEGORIZED_ID
  return (
    <Dialog open={open} onOpenChange={onOpenChange}
      title={blocked ? '无法新增子分类' : '新增分类'} width={420}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>关闭</Button>
          {!blocked && (
            <Button size="sm" onClick={async () => {
              try {
                await materialApi.createCategory(name.trim(), type, parentId)
                toast('分类已创建', 'success')
                setName('')
                onOpenChange(false)
                onCreated()
              } catch (e) { toast((e as Error).message, 'error') }
            }}>创建</Button>
          )}
        </>
      }>
      {blocked
        ? <p className="text-sm text-muted-foreground">「未分类」是固定虚拟分类，不能在其下新增子分类。请先选中其他分类作为父级。</p>
        : <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm" placeholder="分类名称"
            value={name} onChange={(e) => setName(e.target.value)} autoFocus />}
    </Dialog>
  )
}

/** 允许上传的文件后缀（小写、不含点；用于拖拽与选择器统一校验） */
const ALLOWED_EXTS: Record<'video' | 'music', string[]> = {
  video: ['mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv', 'm4v', 'webm'],
  music: ['mp3', 'wav', 'flac', 'aac', 'm4a', 'ogg', 'wma'],
}

/** 取文件路径的后缀（小写；无点返回空串） */
function extOf(p: string): string {
  const i = p.lastIndexOf('.')
  return i < 0 ? '' : p.slice(i + 1).toLowerCase()
}

/** 上传列表顶层项：文件=单路径；文件夹=目录名 + 内部全部文件路径 */
interface UploadEntry {
  name: string
  paths: string[]
}

/** 文件/文件夹上传弹窗（异步任务 + 实时进度 + 失败重试） */
/** 拖拽收集结果（kind/entries 成功时有效；error 非空为用户可读错误） */
interface DropCollect {
  kind?: 'file' | 'folder'
  entries: UploadEntry[]
  error?: string
}

/** 递归读取拖拽文件夹内的所有文件路径。
 *  Electron 只给 dataTransfer.files 顶层 File 注入 path，目录 entry 递归拿到的
 *  内层 File 不带 path → 用「顶层目录真实路径 + entry 相对路径」拼出绝对路径。 */
async function collectDirFiles(entry: FileSystemDirectoryEntry, rootFullPath: string, rootRealPath: string): Promise<string[]> {
  const reader = entry.createReader()
  const entries: FileSystemEntry[] = []
  // readEntries 可能分批返回，需循环读取
  while (true) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
    if (!batch.length) break
    entries.push(...batch)
  }
  const out: string[] = []
  for (const e of entries) {
    if (e.isFile) {
      // 相对路径 = 内层 fullPath 去掉顶层目录的虚拟前缀（如 /music/a/1.mp3 → /a/1.mp3）
      out.push(rootRealPath + e.fullPath.slice(rootFullPath.length))
    } else if (e.isDirectory) {
      out.push(...await collectDirFiles(e as FileSystemDirectoryEntry, rootFullPath, rootRealPath))
    }
  }
  return out
}

/** 从拖拽事件收集待上传文件（纯逻辑，无组件状态；同步阶段先快照 dataTransfer，
 *  因 items/files 在事件返回后失效）。返回 null = 非文件拖拽，忽略。 */
async function collectDropPaths(e: { dataTransfer: DataTransfer | null }): Promise<DropCollect | null> {
  const dt = e.dataTransfer
  if (!dt) return null
  // File.path 由 Electron 注入，需在事件同步阶段先取（整个函数首个 await 前保持同步）
  const droppedFiles: File[] = [...dt.files]
  const topFiles: File[] = []
  const topDirs: FileSystemDirectoryEntry[] = []
  const items = dt.items
  if (items && items.length) {
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry?.()
      if (!entry) {
        const f = items[i].getAsFile()
        if (f) topFiles.push(f)
        continue
      }
      if (entry.isFile) {
        const f = items[i].getAsFile() ?? droppedFiles[i] ?? null
        if (f) topFiles.push(f)
      } else if (entry.isDirectory) {
        topDirs.push(entry as FileSystemDirectoryEntry)
      }
    }
  } else {
    topFiles.push(...droppedFiles)
  }
  if (!topFiles.length && !topDirs.length) return null
  if (topFiles.length && topDirs.length) {
    return { entries: [], error: '文件与文件夹不能同时拖拽' }
  }
  const kind: 'file' | 'folder' = topFiles.length ? 'file' : 'folder'
  const entries: UploadEntry[] = []
  if (kind === 'file') {
    for (const f of topFiles) {
      const p = (f as File & { path?: string }).path || ''
      if (p) entries.push({ name: f.name, paths: [p] })
    }
  } else {
    for (const d of topDirs) {
      // 顶层目录真实路径：droppedFiles 里同名目录 File 的 path（Electron 注入）
      const dirReal = (droppedFiles.find((f) => f.name === d.name) as (File & { path?: string }) | undefined)?.path || ''
      if (!dirReal) continue
      const files = await collectDirFiles(d, d.fullPath, dirReal)
      if (files.length) entries.push({ name: d.name, paths: files })
    }
  }
  if (!entries.length) {
    return { entries: [], error: kind === 'folder' ? '文件夹内没有可上传文件' : '无法获取拖入文件的本地路径' }
  }
  return { kind, entries }
}

function UploadDialog({ open, onOpenChange, categories, defaultCategoryId, type, onDone }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  categories: Category[]
  /** 默认选中的分类 ID */
  defaultCategoryId?: string
  type: 'video' | 'music'
  onDone: () => void
}) {
  const [categoryId, setCategoryId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // 上传列表顶层项（文件一行一项；文件夹一行一项含内部全部文件路径）
  const [entries, setEntries] = useState<UploadEntry[]>([])
  const [dropKind, setDropKind] = useState<'file' | 'folder' | null>(null)
  // 入库分类下拉保留"未分类"虚拟分类作为选项
  const options = flattenTree(buildTree(categories))

  useEffect(() => {
    if (!open) return
    const validIds = new Set(options.map(({ node }) => node.id))
    // fallback 取当前渲染的分类首项（optionsRef 旧实现取到的是首渲染空数组，导致默认分类恒空）
    const fallback = options[0]?.node.id || ''
    setCategoryId((defaultCategoryId && validIds.has(defaultCategoryId)) ? defaultCategoryId : fallback)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const reset = () => {
    setSubmitting(false)
    setEntries([])
    setDropKind(null)
  }

  /** 合并一批顶层项进上传列表：后缀校验过滤 → 类型一致性校验 → 按路径全局去重 */
  const addEntries = (kind: 'file' | 'folder', incoming: UploadEntry[]) => {
    if (dropKind && dropKind !== kind) {
      toast('已选类型与本次拖拽不一致，请先清空', 'error')
      return
    }
    // 按允许后缀过滤（文件夹场景为逐文件过滤）
    const allowed = ALLOWED_EXTS[type]
    let skippedExt = 0
    const cleaned = incoming
      .map((e) => {
        const okPaths = e.paths.filter((p) => allowed.includes(extOf(p)))
        skippedExt += e.paths.length - okPaths.length
        return { name: e.name, paths: okPaths }
      })
      .filter((e) => e.paths.length > 0)
    if (skippedExt > 0) {
      toast(`已忽略 ${skippedExt} 个不支持的文件（支持 ${allowed.join('/')}）`, 'info')
    }
    if (!cleaned.length) {
      toast(`没有符合要求的${type === 'video' ? '视频' : '音乐'}文件（支持 ${allowed.join('/')}）`, 'error')
      return
    }
    setDropKind(kind)
    setEntries((prev) => {
      const exist = new Set(prev.flatMap((e) => e.paths))
      const merged = [...prev]
      for (const e of cleaned) {
        const fresh = e.paths.filter((p) => !exist.has(p))
        if (!fresh.length) continue
        fresh.forEach((p) => exist.add(p))
        merged.push({ name: e.name, paths: fresh })
      }
      return merged
    })
  }

  /** 应用一次拖拽收集结果（错误 toast / 合并待上传列表） */
  const applyCollect = (r: DropCollect) => {
    if (r.error) {
      toast(r.error, 'error')
      return
    }
    if (!r.kind || !r.entries.length) return
    addEntries(r.kind, r.entries)
  }

  /** 系统文件选择器选文件（多选；选择器层已按后缀过滤） */
  const pickFiles = async () => {
    if (!window.electronAPI) {
      toast('仅桌面端支持选择文件', 'error')
      return
    }
    const allowed = ALLOWED_EXTS[type]
    const picked = await window.electronAPI.openFiles([
      { name: type === 'video' ? '视频文件' : '音乐文件', extensions: allowed },
    ])
    if (!picked?.length) return
    addEntries('file', picked.map((p) => ({ name: p.split(/[\\/]/).pop() || p, paths: [p] })))
  }

  /** 系统目录选择器选文件夹（选中后递归展开内部文件） */
  const pickFolder = async () => {
    if (!window.electronAPI) {
      toast('仅桌面端支持选择文件夹', 'error')
      return
    }
    const dir = await window.electronAPI.openFolder()
    if (!dir) return
    let files: string[] = []
    try {
      files = await window.electronAPI.walkDir(dir)
    } catch (e) {
      toast(`读取文件夹失败：${(e as Error).message}`, 'error')
      return
    }
    if (!files.length) {
      toast('文件夹内没有文件', 'info')
      return
    }
    addEntries('folder', [{ name: dir.split(/[\\/]/).pop() || dir, paths: files }])
  }

  // #P2-2：弹窗打开期间挂容器级原生拖拽监听（仅弹窗内拖拽生效，不污染全局）：
  // 1) dragover 放行 drop（否则光标显示禁用；弹窗内任意位置均可放置）
  // 2) drop 交给收集函数 → applyCollect（ref 每渲染刷新，绕开 effect 闭包旧 state）
  // 3) 拦截默认「打开文件」行为（Electron 下会导航 file:// 白屏）
  // 之前挂在 window 上会拦截整个应用的拖拽事件，破坏其他弹窗/页面
  const applyCollectRef = useRef(applyCollect)
  useEffect(() => { applyCollectRef.current = applyCollect })
  const dropZoneRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const el = dropZoneRef.current
    if (!el) return
    const onDragOver = (e: DragEvent) => { e.preventDefault() }
    const onZoneDrop = (e: DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      void collectDropPaths(e).then((r) => { if (r) applyCollectRef.current(r) })
    }
    el.addEventListener('dragover', onDragOver)
    el.addEventListener('drop', onZoneDrop)
    return () => {
      el.removeEventListener('dragover', onDragOver)
      el.removeEventListener('drop', onZoneDrop)
    }
  }, [open])

  // 全部待上传路径（顶层项展开合计）
  const allPaths = entries.flatMap((e) => e.paths)

  /** 提交上传任务：创建成功后立即关闭弹窗，处理进度到任务页查看（R315 一致化） */
  const submit = async () => {
    if (!categoryId) return
    if (!allPaths.length) {
      toast('请先拖拽或选择文件/文件夹', 'info')
      return
    }
    setSubmitting(true)
    try {
      await materialApi.upload(allPaths, categoryId)
      toast(`上传任务已创建（${allPaths.length} 项），结果到任务队列查看`, 'success')
      reset()
      onOpenChange(false)
      onDone()
    } catch (e) {
      toast((e as Error).message, 'error')
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) reset(); onOpenChange(v) }} title="上传文件/文件夹" width={640} height={480}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => { reset(); onOpenChange(false) }}>关闭</Button>
          <Button size="sm" disabled={submitting || !categoryId || !allPaths.length} onClick={submit}>
            {submitting ? '创建中…' : '确定'}
          </Button>
        </>
      }>
      <div ref={dropZoneRef} className="flex h-full flex-col gap-3">
        {/* 拖拽接收由容器级监听承担（弹窗内任意位置可放），虚线框仅作视觉提示 */}
        <div
          className={cn(
            'flex min-h-[160px] flex-1 flex-col items-center justify-center rounded-lg border-2 border-dashed border-border bg-muted/30 p-4 text-sm text-muted-foreground',
            entries.length > 0 && 'items-start justify-start',
          )}
        >
          {entries.length === 0 ? (
            <>
              <div className="mb-2 text-2xl">📁</div>
              <div>拖拽文件或文件夹到弹窗内任意位置，或点击下方按钮选择</div>
              <div className="text-xs">
                支持 {ALLOWED_EXTS[type].join('/')} 格式{type === 'video' ? '视频' : '音乐'}及文件夹，文件与文件夹不可混选
              </div>
            </>
          ) : (
            <div className="w-full space-y-1">
              <div className="mb-1 text-xs">
                已选择 {entries.length} 个{dropKind === 'file' ? '文件' : '文件夹'}
                {dropKind === 'folder' && `（共 ${allPaths.length} 个文件）`}
              </div>
              {entries.map((e) => (
                <div key={`${e.name}|${e.paths[0]}`} className="flex items-center gap-1.5">
                  <span className="min-w-0 truncate text-foreground" title={e.paths.length > 1 ? e.paths.join('\n') : e.paths[0]}>
                    {e.name}
                  </span>
                  {e.paths.length > 1 && (
                    <span className="shrink-0 text-xs text-muted-foreground">{e.paths.length} 个文件</span>
                  )}
                  {/* 移除该顶层项（紧跟文件名右侧；文件夹移除整个目录） */}
                  <button className="shrink-0 rounded px-1 text-muted-foreground hover:text-danger" title="移除"
                    onClick={() => setEntries((prev) => prev.filter((x) => x !== e))}>✕</button>
                </div>
              ))}
              <button
                className="mt-2 text-xs text-accent hover:underline"
                onClick={() => { setEntries([]); setDropKind(null) }}
              >
                清空
              </button>
            </div>
          )}
        </div>
        {/* 非拖拽入口：系统选择器选文件/文件夹 */}
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" onClick={pickFiles}>选择文件</Button>
          <Button variant="outline" size="sm" onClick={pickFolder}>选择文件夹</Button>
        </div>
        <div className="shrink-0">
          <label className="mb-1 block text-sm font-medium">入库分类</label>
          <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm" value={categoryId}
            onChange={(e) => setCategoryId(e.target.value)}>
            {options.map(({ node, depth }) => (
              <option key={node.id} value={node.id}>{'　'.repeat(depth)}{node.name}</option>
            ))}
          </select>
        </div>
      </div>
    </Dialog>
  )
}

/** 分享链接导入弹窗（创建任务后即关闭，处理结果在任务页查看） */
function ImportShareDialog({ open, onOpenChange, categories, defaultCategoryId, type, onDone }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  categories: Category[]
  /** 默认选中的分类 ID（通常为当前左侧选中的分类） */
  defaultCategoryId?: string
  /** 当前页签类型（任务 #132：视频/音乐分别过滤分类） */
  type: 'video' | 'music'
  onDone: () => void
}) {
  const [text, setText] = useState('')
  const [categoryId, setCategoryId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // 入库分类按当前 tab 类型过滤（任务 #132）+ 分类树层级展示（缩进表示层级）；
// 保留"未分类"虚拟分类作为选项
  const options = flattenTree(buildTree(categories.filter((c) => c.type === type)))

  useEffect(() => {
    if (!open) return
    const validIds = new Set(options.map(({ node }) => node.id))
    // fallback 取当前渲染的分类首项（optionsRef 旧实现取到的是首渲染空数组，导致默认分类恒空）
    const fallback = options[0]?.node.id || ''
    setCategoryId((defaultCategoryId && validIds.has(defaultCategoryId)) ? defaultCategoryId : fallback)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const reset = () => {
    setText('')
    setSubmitting(false)
  }

  const lines = text.split('\n').map((s) => s.trim()).filter(Boolean)
  const submit = async () => {
    if (!categoryId) return
    if (!lines.length) {
      toast('请输入至少一条分享链接', 'info')
      return
    }
    setSubmitting(true)
    try {
      await materialApi.importShare(lines, categoryId)
      toast(`分享任务已创建（${lines.length} 条），结果到任务队列查看`, 'success')
      reset()
      onOpenChange(false)
      onDone()
    } catch (e) {
      toast((e as Error).message, 'error')
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) reset(); onOpenChange(v) }} title="导入分享链接" width={640} height={580}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => { reset(); onOpenChange(false) }}>关闭</Button>
          <Button size="sm" disabled={submitting || !categoryId} onClick={submit}>
            {submitting ? '创建中…' : '确定'}
          </Button>
        </>
      }>
      <div className="flex h-full flex-col gap-3">
        <textarea
          className="min-h-[160px] flex-1 resize-none rounded-md border border-border bg-white p-2.5 text-xs"
          placeholder={'每行一条分享文本，如：\n7.92 xxx:/ 复制打开平台 https://v.douyin.com/xxxx/\nhttps://v.douyin.com/xxxx/'}
          value={text} onChange={(e) => setText(e.target.value)} />
        <div className="shrink-0">
          <label className="mb-1 block text-sm font-medium">入库分类</label>
          <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm" value={categoryId}
            onChange={(e) => setCategoryId(e.target.value)}>
            {options.map(({ node, depth }) => (
              <option key={node.id} value={node.id}>{'　'.repeat(depth)}{node.name}</option>
            ))}
          </select>
        </div>
      </div>
    </Dialog>
  )
}



/** 视频浮窗播放（点封面预览；文件经 /api/files/material/ 回源流式播放） */
function VideoPlayDialog({ material, open, onOpenChange }: {
  material: Material | null
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (open && material?.file_path) {
      fileUrl(material.file_path).then((u) => alive && setSrc(u))
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [open, material?.file_path])
  if (!material) return null
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={material.title} width={520}>
      {src ? (
        <video src={src} className="mx-auto max-h-[70vh] w-full rounded-md bg-black" controls autoPlay
          onError={() => toast('视频加载失败（文件缺失或格式不支持）', 'error')} />
      ) : (
        <div className="flex aspect-9/16 items-center justify-center rounded-md bg-muted text-muted-foreground">
          加载中…
        </div>
      )}
    </Dialog>
  )
}

/** 音乐浮窗播放（点列表封面触发；文件经 /api/files/ 回源流式播放，#50） */
function MusicPlayDialog({ material, open, onOpenChange }: {
  material: Material | null
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (open && material?.file_path) {
      fileUrl(material.file_path).then((u) => alive && setSrc(u))
    } else {
      setSrc('')
    }
    return () => { alive = false }
  }, [open, material?.file_path])
  if (!material) return null
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={material.title} width={420}>
      <div className="flex flex-col items-center gap-4 py-2">
        {/* 大封面（无封面显示音符占位） */}
        {material.cover_url
          ? <CoverImage material={material} className="h-44 w-44 rounded-xl" />
          : <div className="flex h-44 w-44 items-center justify-center rounded-xl bg-muted text-5xl text-muted-foreground">🎵</div>}
        <audio src={src} controls autoPlay className="w-full"
          onError={() => toast('音乐加载失败（文件缺失或格式不支持）', 'error')} />
      </div>
    </Dialog>
  )
}

/** 移动分类弹窗（单条/批量共用：树形下拉选择目标分类） */
function MoveCategoryDialog({ open, onOpenChange, categories, type, title, defaultCategoryId, onConfirm }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  categories: Category[]
  type: 'video' | 'music'
  title: string
  /** 默认选中的目标分类 ID */
  defaultCategoryId?: string
  onConfirm: (categoryId: string) => Promise<void>
}) {
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  // 目标分类下拉保留"未分类"虚拟分类作为选项
  const options = flattenTree(buildTree(categories.filter((c) => c.type === type)))
  useEffect(() => {
    if (!open) return
    const validIds = new Set(options.map(({ node }) => node.id))
    const fallback = options[0]?.node.id || ''
    setTarget((defaultCategoryId && validIds.has(defaultCategoryId)) ? defaultCategoryId : fallback)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="移动分类" width={440}
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy || !target}
            onClick={async () => {
              if (!target) return
              setBusy(true)
              try { await onConfirm(target); onOpenChange(false) } finally { setBusy(false) }
            }}>移动</Button>
        </>
      }>
      <div className="space-y-2">
        <div className="text-sm">{title}</div>
        <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm" value={target}
          onChange={(e) => setTarget(e.target.value)}>
          {options.map(({ node, depth }) => (
            <option key={node.id} value={node.id}>{'　'.repeat(depth)}{node.name}</option>
          ))}
        </select>
      </div>
    </Dialog>
  )
}

/** 素材库页（分类树随类型 tab 切换；素材列表 + 拉取任务浮窗 + 批量管理模式） */
export function MaterialsPage() {
  const [tab, setTab] = useState<'video' | 'music' | 'task'>('video')
  const [categories, setCategories] = useState<Category[]>([])
  const [selectedCat, setSelectedCat] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [data, setData] = useState<{ list: Material[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [keyword, setKeyword] = useState('')
  const [addCatOpen, setAddCatOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  // 拉取任务：panelOpen=列表浮窗；editOpen=创建/编辑弹窗（叠上层，关闭后回浮窗）
  const [pullEditOpen, setPullEditOpen] = useState(false)
  const [editPullTask, setEditPullTask] = useState<VideoPullTask | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Material | null>(null)
  const [detailTarget, setDetailTarget] = useState<Material | null>(null)
  const [playTarget, setPlayTarget] = useState<Material | null>(null)
  // 音乐播放浮窗目标（点列表封面触发，#50）
  const [playMusicTarget, setPlayMusicTarget] = useState<Material | null>(null)
  const [deleteCatTarget, setDeleteCatTarget] = useState<TreeNode | null>(null)
  // 分类管理模式：树节点显示 修改/删除/上移/下移/左移/右移
  const [catManage, setCatManage] = useState(false)
  const [moveCatTarget, setMoveCatTarget] = useState<Material | null>(null)
  const [prompt, promptDialog] = usePrompt()
  // 管理模式（多选）：#340 按 tab 隔离已选项，video/music 互不干扰
  const [manageMode, setManageMode] = useState(false)
  const [checkedIdsByTab, setCheckedIdsByTab] = useState<Record<'video' | 'music', Set<string>>>({
    video: new Set(),
    music: new Set(),
  })
  // 当前 tab 的已选集合（任务 tab 无意义，给空 Set 兜底）
  const checkedIds = tab === 'task' ? (new Set() as Set<string>) : checkedIdsByTab[tab]
  /** 更新当前 tab 的已选集合（不重置另一 tab 的已选项） */
  const setCheckedIds = (updater: Set<string> | ((prev: Set<string>) => Set<string>)) => {
    if (tab === 'task') return
    const key = tab
    setCheckedIdsByTab((prev) => {
      const cur = prev[key]
      const next = typeof updater === 'function' ? (updater as (p: Set<string>) => Set<string>)(cur) : updater
      return { ...prev, [key]: next }
    })
  }
  const [batchMoveOpen, setBatchMoveOpen] = useState(false)
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  // 任务 tab 由 PullTaskPanel 内部 load，外部通过 refreshTrigger 触发 + onLoaded 收尾
  const [refreshing, setRefreshing] = useState(false)
  const [refreshTrigger, setRefreshTrigger] = useState(0)

  const loadCategories = useCallback(() => {
    // 任务 tab：拉取任务入库分类是视频分类（#352：拉取任务编辑/创建对话框的入库分类下拉需要数据）
    const loadType: 'video' | 'music' = tab === 'task' ? 'video' : tab
    materialApi.categories(loadType).then((rows) => {
      setCategories(rows)
      if (rows[0] && !rows.some((r) => r.id === selectedCat)) setSelectedCat('')
    }).catch((e) => console.warn('[Materials] 请求失败:', e))
  }, [tab, selectedCat])

  const loadMaterials = useCallback(() => {
    // 任务 tab 不需要素材列表
    if (tab === 'task') return
    return materialApi.list({
      type: tab,
      category_id: selectedCat,
      keyword,
      page: String(page),
      page_size: String(pageSize),
    })
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Materials] 请求失败:', e))
  }, [tab, selectedCat, keyword, page, pageSize])

  /** 一键刷新：同步 setRefreshing(true)，完成后统一关 loading（任务 tab 由 PullTaskPanel onLoadingChange 关闭） */
  const doRefresh = useCallback(() => {
    setRefreshing(true)
    if (tab === 'task') {
      // 任务 tab：setRefreshing 由 PullTaskPanel onLoadingChange(false) 关
      setRefreshTrigger((t) => t + 1)
      loadCategories()
      return
    }
    setLoading(true)
    Promise.all([loadCategories(), loadMaterials()])
      .finally(() => {
        setLoading(false)
        setRefreshing(false)
      })
  }, [tab, loadCategories, loadMaterials])

  // 初次 mount / 切 tab / 改筛选都走 doRefresh：统一一处管 loading
  useEffect(() => {
    doRefresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, selectedCat, keyword, page, pageSize])

  /** 任务 tab 收到 PullTaskPanel load 状态变化通知（用于父级按钮 loading 显示） */
  const onPullLoadingChange = useCallback((v: boolean) => setRefreshing(v), [])

  /** 树展开/收起 */
  const toggleCollapse = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  /** 分类管理模式移动操作：
   *  up/down=同级上下移（交换 sort_order）；out=左移升一级（挂到祖父，排到原父之后）；
   *  in=右移降一级（挂到上一兄弟末尾）；深度上限 4 由后端校验。 */
  const shiftCategory = async (node: TreeNode, dir: 'up' | 'down' | 'out' | 'in') => {
    const hit = findNodeAndParent(tree, node.id, null)
    if (!hit) return
    const [, parent] = hit
    const siblings = parent ? parent.children : tree
    const idx = siblings.findIndex((n) => n.id === node.id)
    try {
      if (dir === 'up' || dir === 'down') {
        const target = dir === 'up' ? siblings[idx - 1] : siblings[idx + 1]
        if (!target) { toast(dir === 'up' ? '已在顶部' : '已在底部', 'info'); return }
        // 交换双方 sort_order
        await materialApi.moveCategory(node.id, node.parent_id, target.sort_order)
        await materialApi.moveCategory(target.id, target.parent_id, node.sort_order)
      } else if (dir === 'out') {
        if (!parent) { toast('顶级分类无法再升级', 'info'); return }
        const grand = findNodeAndParent(tree, parent.id, null)?.[1] ?? null
        // 挂到祖父下，排到原父级之后
        await materialApi.moveCategory(node.id, grand ? grand.id : '', parent.sort_order + 1)
      } else {
        // 右移：挂到上一兄弟末尾
        const prev = siblings[idx - 1]
        if (!prev) { toast('没有上一级兄弟可挂靠', 'info'); return }
        const maxSort = prev.children.length
          ? Math.max(...prev.children.map((c) => c.sort_order)) : -1
        await materialApi.moveCategory(node.id, prev.id, maxSort + 1)
      }
      loadCategories()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  /** 时长格式化（floor 截断，与播放器控件显示一致，避免 1:51 vs 1分52秒） */
  const fmtDuration = (ms: number | null) => {
    if (ms == null) return '-'
    const s = Math.floor(ms / 1000)
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  }

  const [uploadOpen, setUploadOpen] = useState(false)

  /** 管理模式勾选切换 */
  const toggleChecked = (id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  /** 全选/清空当前页 */
  const toggleCheckAll = () => {
    setCheckedIds((prev) =>
      prev.size === data.list.length ? new Set() : new Set(data.list.map((m) => m.id)))
  }

  /** 批量移动分类 */
  const doBatchMove = async (categoryId: string) => {
    const ids = [...checkedIds]
    try {
      await Promise.all(ids.map((id) => materialApi.update(id, { category_id: categoryId })))
      toast(`已移动 ${ids.length} 个素材`, 'success')
      setCheckedIds(new Set())
      setManageMode(false)
      loadMaterials()
      loadCategories()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  /** 批量删除 */
  const doBatchDelete = async () => {
    const ids = [...checkedIds]
    try {
      await Promise.all(ids.map((id) => materialApi.remove(id)))
      toast(`已删除 ${ids.length} 个素材`, 'success')
      setCheckedIds(new Set())
      setManageMode(false)
      loadMaterials()
      loadCategories()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const tree = buildTree(categories)

  return (
    <div className="flex h-full flex-col p-5">
      {/* 上：标题 + 视频/音乐/任务页签 + 搜索 + 刷新（紧跟左边内容末尾）+ 右侧操作 */}
      <div className="mb-4 flex shrink-0 items-center gap-3">
        <h1 className="flex h-8 items-center text-lg font-semibold">素材库</h1>
        <div className="ml-2 flex gap-3 text-sm">
          {([['video', '视频'], ['music', '音乐'], ['task', '任务']] as const).map(([k, l]) => (
            <button key={k} className={tab === k ? 'font-medium text-accent' : 'text-muted-foreground hover:text-foreground'}
              onClick={() => { setTab(k); setSelectedCat(''); setKeyword(''); setManageMode(false); setPage(1); setData({ list: [], total: 0 }) }}>{l}</button>
          ))}
        </div>
        {tab !== 'task' && (
          <input className="ml-4 h-8 w-48 rounded-md border border-border bg-white px-2.5 text-sm" placeholder="搜索标题"
            value={keyword} onChange={(e) => { setKeyword(e.target.value); setPage(1) }} />
        )}
        {/* 三个 tab 都显示刷新按钮；任务 tab 由 PullTaskPanel onLoadingChange 通知 loading 状态 */}
        <Button size="sm" variant="outline" onClick={doRefresh} disabled={refreshing || loading}>
          {refreshing || loading ? '刷新中…' : '刷新'}
        </Button>
        <div className="ml-auto flex items-center gap-2">
          {manageMode && tab !== 'task' ? (
            <>
              <span className="text-sm text-muted-foreground">已选 {checkedIds.size} 项</span>
              <Button variant="outline" size="sm" onClick={toggleCheckAll}>
                {checkedIds.size === data.list.length && data.list.length > 0 ? '取消全选' : '全选本页'}
              </Button>
              <Button variant="outline" size="sm" disabled={checkedIds.size === 0}
                onClick={() => setBatchMoveOpen(true)}>移动分类</Button>
              <Button variant="outline" size="sm" className="!text-danger" disabled={checkedIds.size === 0}
                onClick={() => setBatchDeleteOpen(true)}>批量删除</Button>
              <Button size="sm" onClick={() => { setManageMode(false); setCheckedIds(new Set()) }}>退出管理</Button>
            </>
          ) : tab === 'task' ? (
            // 任务 tab：仅"创建拉取任务"按钮
            <Button variant="outline" size="sm" onClick={() => { setEditPullTask(null); setPullEditOpen(true) }}>创建拉取任务</Button>
          ) : (
            <>
              {/* 任务 #132：分享链接导入在音乐页签也显示（视频/原声 URL 都支持） */}
              <Button variant="outline" size="sm" onClick={() => setShareOpen(true)}>导入分享链接</Button>
              <Button variant="outline" size="sm" onClick={() => setUploadOpen(true)}>上传文件</Button>
              <Button variant="outline" size="sm" onClick={() => setManageMode(true)}>管理</Button>
            </>
          )}
        </div>
      </div>

      {/* 下：tab === 'task' 时显示拉取任务面板；其他 tab 显示左分类 + 右素材列表 */}
      {tab === 'task' ? (
        <PullTaskPanel
          onEdit={(t) => { setEditPullTask(t); setPullEditOpen(true) }}
          onCreated={doRefresh}
          refreshTrigger={refreshTrigger}
          onLoadingChange={onPullLoadingChange}
        />
      ) : (
      <div className="flex min-h-0 flex-1 gap-4">
        {/* 左：分类树（常规=筛选；管理模式=修改/删除/上移/下移/左移/右移） */}
        <div className="w-56 shrink-0 overflow-y-auto rounded-lg border border-border bg-background-elev p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-semibold">分类</span>
            <span className="flex items-center gap-1">
              {catManage && <span className="text-[10px] text-accent">管理中</span>}
              <button className={cn('rounded px-1.5 hover:bg-muted',
                catManage ? 'text-accent' : 'text-muted-foreground')}
                title={catManage ? '退出分类管理' : '管理分类'}
                onClick={() => setCatManage((v) => !v)}>⚙</button>
              <button className="rounded px-1.5 text-muted-foreground hover:bg-muted" title="新增分类"
                onClick={() => setAddCatOpen(true)}>＋</button>
            </span>
          </div>
          <button
            className={cn('mb-1 flex w-full items-center justify-between rounded px-2 py-1.5 text-sm',
              !selectedCat ? 'bg-blue-50 font-medium text-accent' : 'hover:bg-muted')}
            onClick={() => { setSelectedCat(''); setPage(1) }}>
            <span>全部{tab === 'video' ? '视频' : '音乐'}</span>
          </button>
          {/* 分类树（系统分类"未分类"作为顶级末位项固定渲染，sort_order=1_000_000） */}
          {tree.map((n) => (
            <TreeItem key={n.id} node={n} depth={0} selectedId={selectedCat}
              onSelect={(id) => { setSelectedCat(id); setPage(1) }}
              collapsed={collapsed} toggle={toggleCollapse} manage={catManage}
              onRename={async (node) => {
                if (node.id === UNCATEGORIZED_ID) return  // 系统分类不可重命名
                const v = await prompt('重命名分类', node.name)
                if (v && v !== node.name) {
                  try {
                    await materialApi.renameCategory(node.id, v)
                    toast('已重命名', 'success')
                    loadCategories()
                    loadMaterials()
                  } catch (e) { toast((e as Error).message, 'error') }
                }
              }}
              onDelete={(node) => {
                if (node.id === UNCATEGORIZED_ID) return  // 防御性：未分类不可删除
                setDeleteCatTarget(node)
              }}
              onShift={shiftCategory} />
          ))}
          {categories.length === 0 && (
            /* #99：分类为空时给一个明显的大按钮引导，避免用户找不到 + */
            <button
              className="mt-2 flex w-full items-center justify-center gap-1 rounded border border-dashed border-border bg-white px-2 py-2 text-xs text-accent hover:border-accent hover:bg-blue-50"
              onClick={() => setAddCatOpen(true)}>
              <span>＋</span><span>新增{tab === 'video' ? '视频' : '音乐'}分类</span>
            </button>
          )}
        </div>

        {/* 右：素材列表（管理模式下多选；翻页器固定底部。
            视频=滚动区滚卡片网格；音乐=表格容器自身撑满滚动） */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {selectedCat && !manageMode && (
          <div className="mb-3 text-xs text-muted-foreground">
            当前分类：{(() => {
              const hit = flattenTree(tree).find((f) => f.node.id === selectedCat)
              return hit?.node.path || ''
            })()}
          </div>
        )}

        {/* 视频 tab：卡片网格（一行 4 列；每项左图右信息；管理模式加复选框） */}
        {tab === 'video' && (
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2 2xl:grid-cols-4">
          {data.list.map((m) => (
            <div key={m.id}
              className={cn('group flex gap-2.5 rounded-lg border border-border bg-background-elev p-2.5 transition-shadow',
                manageMode && checkedIds.has(m.id) && 'border-accent bg-blue-50/40',
                m.file_status === 'missing' && 'border-danger/40',
                manageMode && 'cursor-pointer')}
              onClick={manageMode ? () => toggleChecked(m.id) : undefined}>
              {/* 管理模式复选框 */}
              {manageMode && (
                <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 accent-blue-600" checked={checkedIds.has(m.id)}
                  onChange={() => toggleChecked(m.id)} onClick={(e) => e.stopPropagation()} />
              )}
              {/* 左：封面（固定 120×160；点视频预览浮窗播放，音乐/管理模式不触发） */}
              <button className="relative block shrink-0 text-left" style={{ width: 120, height: 160 }}
                onClick={(e) => {
                  if (manageMode) return
                  e.stopPropagation()
                  if (m.type === 'video' && m.file_status === 'normal') setPlayTarget(m)
                  else setDetailTarget(m)
                }}>
                <CoverImage material={m} className="h-full w-full rounded-md" />
                {m.type === 'video' && m.duration_ms != null && (
                  <span className="absolute bottom-1 right-1 rounded bg-black/60 px-1 py-0.5 text-[14px] leading-none text-white">
                    {fmtDuration(m.duration_ms)}
                  </span>
                )}
                {m.file_status === 'missing' && (
                  <span className="absolute left-1 top-1 rounded bg-danger px-1 py-0.5 text-[10px] text-white">缺失</span>
                )}
              </button>
              {/* 右：上部标题（动态高度自动换行），下部固定三行 */}
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="cursor-pointer text-sm font-medium leading-snug" title={m.title}
                  onClick={() => !manageMode && setDetailTarget(m)} role="button">
                  {m.title}
                </div>
                <div className="mt-auto flex flex-col gap-1 pt-1.5 text-xs">
                  {/* 第一行：分类 */}
                  <div className="truncate text-muted-foreground" title={m.category_name || ''}>
                    {m.category_name || '未分类'}
                  </div>
                  {/* 第二行：四数（一行流式排列，放不下整体换行） */}
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-muted-foreground">
                    <span className="whitespace-nowrap" title="点赞">👍 {fmtCount(m.digg_count)}</span>
                    <span className="whitespace-nowrap" title="评论">💬 {fmtCount(m.comment_count)}</span>
                    <span className="whitespace-nowrap" title="转发">↗ {fmtCount(m.share_count)}</span>
                    <span className="whitespace-nowrap" title="收藏">⭐ {fmtCount(m.collect_count)}</span>
                  </div>
                  {/* 第三行：昵称 + 相对发布时间 */}
                  <div className="flex items-center gap-1.5 text-muted-foreground">
                    <span className="truncate" title={m.author_nickname || ''}>
                      {m.author_nickname ? `@${m.author_nickname}` : '无作者信息'}
                    </span>
                    <span className="shrink-0 text-muted-foreground/70">· {fmtRelativeTime(m.publish_time || m.create_time)}</span>
                  </div>
                </div>
              </div>
            </div>
          ))}
          {data.list.length === 0 && (
            <div className="col-span-full py-12 text-center text-sm text-muted-foreground">
              暂无素材，可上传文件 / 导入分享链接 / 创建拉取任务
            </div>
          )}
        </div>
        )}

        {/* 音乐 tab：卡片网格（#48 参考视频列表：左圆形封面 + 右标题/作者/创建时间） */}
        {tab === 'music' && (
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2 2xl:grid-cols-4">
          {data.list.map((m) => (
            <div key={m.id}
              className={cn('group flex items-center gap-3 rounded-lg border border-border bg-background-elev p-2.5 transition-shadow',
                manageMode && checkedIds.has(m.id) && 'border-accent bg-blue-50/40',
                m.file_status === 'missing' && 'border-danger/40',
                manageMode && 'cursor-pointer')}
              onClick={manageMode ? () => toggleChecked(m.id) : undefined}>
              {/* 管理模式复选框 */}
              {manageMode && (
                <input type="checkbox" className="h-4 w-4 shrink-0 accent-blue-600" checked={checkedIds.has(m.id)}
                  onChange={() => toggleChecked(m.id)} onClick={(e) => e.stopPropagation()} />
              )}
              {/* 左：圆形封面（点封面浮窗播放，文件缺失回退详情，#50） */}
              <button className="relative shrink-0" title={m.title}
                onClick={(e) => {
                  if (manageMode) return
                  e.stopPropagation()
                  if (m.file_status === 'normal') setPlayMusicTarget(m)
                  else setDetailTarget(m)
                }}>
                {m.cover_url
                  ? <CoverImage material={m} className="h-14 w-14 rounded-full" />
                  : (
                    <div className="flex h-14 w-14 items-center justify-center rounded-full bg-muted text-xl text-muted-foreground">
                      🎵
                    </div>
                  )}
                {m.file_status === 'missing' && (
                  <span className="absolute inset-0 flex items-center justify-center rounded-full bg-black/50 text-[10px] text-white">缺失</span>
                )}
              </button>
              {/* 右：标题（上）+ 作者/创建时间（下） */}
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="cursor-pointer text-sm font-medium leading-snug" title={m.title}
                  onClick={() => !manageMode && setDetailTarget(m)} role="button">
                  {m.title}
                </div>
                <div className="mt-auto flex items-center gap-1.5 pt-1.5 text-xs text-muted-foreground">
                  <span className="truncate" title={m.author_nickname || ''}>
                    {m.author_nickname ? `@${m.author_nickname}` : '无作者信息'}
                  </span>
                  <span className="shrink-0 text-muted-foreground/70">· {fmtRelativeTime(m.create_time)}</span>
                </div>
              </div>
            </div>
          ))}
          {data.list.length === 0 && (
            <div className="col-span-full py-12 text-center text-sm text-muted-foreground">
              暂无音乐，可上传文件 / 导入分享链接自动入库原声
            </div>
          )}
        </div>
        )}
        </div>
        {/* 翻页器固定底部（无底距） */}
        <div>
          <Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
            onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} />
        </div>
        </div>
      </div>
      )}

      {/* 视频浮窗播放 */}
      <VideoPlayDialog material={playTarget} open={!!playTarget}
        onOpenChange={(v) => !v && setPlayTarget(null)} />
      {/* 音乐浮窗播放（#50） */}
      <MusicPlayDialog material={playMusicTarget} open={!!playMusicTarget}
        onOpenChange={(v) => !v && setPlayMusicTarget(null)} />

      <MaterialDetailDialog material={detailTarget} open={!!detailTarget}
        onOpenChange={(v) => !v && setDetailTarget(null)}
        onMoveCategory={(m) => setMoveCatTarget(m)}
        onTitleSaved={(newTitle) => {
          loadMaterials()
          // 保存后立即用已编辑值刷新当前详情，避免列表接口延迟导致旧标题回显
          if (detailTarget) setDetailTarget({ ...detailTarget, title: newTitle })
        }} />
      {/* 单条移动分类 */}
      {tab !== 'task' && (
      <MoveCategoryDialog open={!!moveCatTarget} onOpenChange={(v) => !v && setMoveCatTarget(null)}
        categories={categories} type={tab as 'video' | 'music'} title={`移动「${moveCatTarget?.title}」到分类：`}
        defaultCategoryId={moveCatTarget?.category_id}
        onConfirm={async (cid) => {
          if (!moveCatTarget) return
          await materialApi.update(moveCatTarget.id, { category_id: cid })
          toast('已移动', 'success')
          const catName = flattenTree(buildTree(categories)).find((f) => f.node.id === cid)?.node.name || ''
          // 当前详情若为同一素材，同步分类显示
          if (detailTarget?.id === moveCatTarget.id) {
            setDetailTarget({ ...detailTarget, category_id: cid, category_name: catName })
          }
          loadMaterials()
          loadCategories()
        }} />
      )}
      {/* 批量移动分类 */}
      {tab !== 'task' && (<>
      <MoveCategoryDialog open={batchMoveOpen} onOpenChange={setBatchMoveOpen}
        categories={categories} type={tab as 'video' | 'music'} title={`将 ${checkedIds.size} 个素材移动到分类：`}
        onConfirm={doBatchMove} />
      <ConfirmDialog open={batchDeleteOpen} onOpenChange={setBatchDeleteOpen} title="批量删除" danger
        content={`确定删除已选 ${checkedIds.size} 个素材？被项目引用处将显示素材缺失。`}
        onConfirm={doBatchDelete} />
      <AddCategoryDialog open={addCatOpen} onOpenChange={setAddCatOpen} type={tab as 'video' | 'music'} parentId={selectedCat}
        onCreated={loadCategories} />
      <ImportShareDialog open={shareOpen} onOpenChange={setShareOpen} categories={categories} defaultCategoryId={selectedCat} type={tab as 'video' | 'music'} onDone={loadMaterials} />
      <UploadDialog open={uploadOpen} onOpenChange={setUploadOpen} categories={categories} defaultCategoryId={selectedCat} type={tab as 'video' | 'music'}
        onDone={() => { loadMaterials(); loadCategories() }} />
      </>)}
      {/* 拉取任务创建/编辑弹窗（#349：编辑保存后必须刷列表，loadMaterials 在任务 tab 直接 return，故走 doRefresh 触发 refreshTrigger 让 PullTaskPanel 重载） */}
      <PullTaskDialog open={pullEditOpen}
        onOpenChange={(v) => { if (!v) { setPullEditOpen(false); setEditPullTask(null) } }}
        categories={categories} editTarget={editPullTask}
        onDone={() => { setPullEditOpen(false); setEditPullTask(null); doRefresh() }} />
      <ConfirmDialog open={!!deleteTarget} onOpenChange={(v) => !v && setDeleteTarget(null)} title="删除素材" danger
        content={`确定删除素材「${deleteTarget?.title}」？被项目引用处将显示素材缺失。`}
        onConfirm={async () => {
          if (!deleteTarget) return
          try {
            await materialApi.remove(deleteTarget.id)
            toast('已删除', 'success')
            loadMaterials()
            loadCategories()
          } catch (e) { toast((e as Error).message, 'error') }
        }} />
      {/* 分类重命名：直接复用 prompt 输入框（在 TreeItem 回调触发） */}
      {/* 分类删除 */}
      {deleteCatTarget && (
        <Dialog open onOpenChange={(v) => !v && setDeleteCatTarget(null)} title="删除分类" width={480}
          footer={
            <>
              <Button variant="outline" size="sm" onClick={() => setDeleteCatTarget(null)}>取消</Button>
              <Button variant="danger" size="sm" onClick={async () => {
                try {
                  await materialApi.deleteCategory(deleteCatTarget.id, 'to_parent')
                  toast('已删除分类', 'success')
                  if (selectedCat === deleteCatTarget.id) setSelectedCat('')
                  setDeleteCatTarget(null)
                  loadCategories()
                  loadMaterials()
                } catch (e) { toast((e as Error).message, 'error') }
              }}>删除</Button>
            </>
          }>
          <p className="text-sm">
            确定删除分类「{deleteCatTarget.name}」？
            {deleteCatTarget.children.length > 0
              ? '该分类含子分类，需先删除子分类。'
              : '分类下素材将移动到「未分类」。'}
          </p>
        </Dialog>
      )}
      {promptDialog}
    </div>
  )
}
