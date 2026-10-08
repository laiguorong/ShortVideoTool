/** 发布管理向导（#413）：6 步引导新建发布任务。
 *  #417：步骤条样式优化；项目列表按创建时间倒序；门店/简介列表加计数+全选反选+默认全选；自主声明存文本。
 *  #426：预览按账号分组渲染。
 *  #427：预览分组卡片样式与 step3 门店 / step4 简介同款；dialog 整体滚动，标题/进度条/底部操作区固定。
 *  #428：再合并「声明 / 下载」到「排期规则」（7 → 6 步）；进度条居中；当前步骤圆环显示 idx；
 *       预览顶部 stats 块删除；预览加全局序号列；简介列在 td 内 div 上做 line-clamp-2。
 *  #450：state 全对象数组化（selAccounts/selProjects/selectedShops）；
 *       selectedShops/selectedIntros 直接挂在 ProjectDraftState 上；
 *       视频简介改为临时多行文本框（不再调 intros API）；
 *       不再调 /api/selection/shops（wizard 不显示全量店）。 */
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import {
  publishApi,
  type DailyLimitEntry, type DraftRequest, type PreviewItem, type PreviewResult,
  type ShopDraftEntry, type AccountDraftSnapshot, type ProjectDraftSnapshot,
  type VideoDirEntry,
} from '@/api/publish'
import { accountApi, type Account } from '@/api/account'
import { type ProjectRow } from '@/api/creation'
import { publishIntroApi, type ProjectShopItem, type VideoIntro } from '@/api/publish_intro'

/** 默认起始 = 明天 07:00 */
function defaultStart(): string {
  const d = new Date(Date.now() + 86400000)
  d.setHours(7, 0, 0, 0)
  return fmtLocal(d)
}
/** 默认结束 = 明天 22:00 */
function defaultEnd(): string {
  const d = new Date(Date.now() + 86400000)
  d.setHours(22, 0, 0, 0)
  return fmtLocal(d)
}
function fmtLocal(d: Date): string {
  // #420：发布时间统一去掉秒数，格式 yyyy-MM-dd HH:mm
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** #597：当前本地时间（"yyyy-MM-dd HH:mm"）—— canAdvance 校验过去时间用 */
function localNowStr(): string {
  return fmtLocal(new Date())
}

/** #597：当前本地时间（"yyyy-MM-ddTHH:mm"）—— datetime-local input 的 min 属性用 */
function localNowMin(): string {
  return fmtLocal(new Date()).replace(' ', 'T')
}

/** 行内 spinner（视频目录按钮 loading 用） */
function Spinner() {
  return (
    <span role="status" aria-label="处理中"
      className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
  )
}

interface WizardProps {
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: () => void
  /** #418：编辑模式——传入已有 draft 的 task_id，wizard 自动回填 */
  editingTaskId?: string | null
  /** #453：复制模式——传入等价 DraftRequest payload（来自 duplicate_task），wizard 自动回填 */
  initialPayload?: Omit<DraftRequest, 'task_id'> | null
}

interface ProjectDraftState {
  project_id: string
  // #450：选中门店直接存完整 ShopDraftEntry 对象（id 可从 .id 派生）
  selectedShops: ShopDraftEntry[]
  // #450：选中的视频简介——每条 {id, content, topics?}；list 模式 id 用 DB id；manual 模式用稳定派生
  selectedIntros: { id: string; content: string; topics?: string[] }[]
  // #452：视频简介输入模式——'list'（从项目已绑定的简介列表多选）/ 'manual'（多行文本框输入）
  introMode: 'list' | 'manual'
}

// #418：合并「发布数量 + 排期时间」为「排期规则」一步；8 步 → 7 步
// #428：再合并「声明 / 下载」到「排期规则」，7 步 → 6 步
const STEP_TITLES = [
  '选择账号', '选择项目', '挂载门店', '视频简介',
  '排期规则', '预览确认',
] as const

export function PublishWizardDialog({ open, onOpenChange, onCreated, editingTaskId, initialPayload }: WizardProps) {
  const [step, setStep] = useState(1)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [projects, setProjects] = useState<ProjectRow[]>([])

  // #450：选中态全部对象数组化（不再单独存 id 数组）
  const [selAccounts, setSelAccounts] = useState<Account[]>([])
  const [selProjects, setSelProjects] = useState<ProjectRow[]>([])
  // #418：项目来源类型（默认项目列表；切到 video_dir 时 step2/3 走新分支）
  const [projectSource, setProjectSource] = useState<'project' | 'video_dir'>('project')
  // 视频目录模式：添加目录按钮的两阶段 busy（系统对话框 / API 扫描）
  const [videoDirBusy, setVideoDirBusy] = useState<null | 'selecting' | 'scanning'>(null)
  // #290：标记已回填的 editingTaskId（避免依赖 accounts/projects 异步加载触发重复回填）
  const [editingTaskIdHandled, setEditingTaskIdHandled] = useState<string | null>(null)
  // #453：复制模式——标记已回填的 initialPayload（payload 是对象，用 JSON 长度的简易指纹）
  const [initialPayloadHandled, setInitialPayloadHandled] = useState<string | null>(null)
  // 视频简介-按项目一一对应：每个视频目录一份独立简介文本（一行一条）
  const [manualIntroTexts, setManualIntroTexts] = useState<Record<string, string>>({})
  // #418：成品视频目录列表
  const [videoDirs, setVideoDirs] = useState<VideoDirEntry[]>([])
  // #review-fix-2：批量刷新 video_count 的 AbortController（卸载 / editingTask 切换时中断）
  const scanCtrlRef = useRef<AbortController | null>(null)
  // #418：手动输入门店——按 video_dir.id 索引；key=dir.id, value=textarea 文本（一行一家）
  const [manualShopTexts, setManualShopTexts] = useState<Record<string, string>>({})
  // #450：项目 draft 内嵌 selectedShops/selectedIntros（不再独立 store）
  const [projectDraft, setProjectDraft] = useState<Record<string, ProjectDraftState>>({})
  // #450：每个项目已绑定的门店列表（按 selProjects 懒加载）——只读快照，用于 step3 展示候选店
  const [projShops, setProjShops] = useState<Record<string, ProjectShopItem[]>>({})
  // #456：每个项目已绑定的简介列表缓存（供 step4 list 模式用）
  const [projIntros, setProjIntros] = useState<Record<string, VideoIntro[]>>({})
  const [projIntrosLoaded, setProjIntrosLoaded] = useState<Record<string, boolean>>({})
  const [dailyLimitMode, setDailyLimitMode] = useState<'global' | 'per_account'>('global')
  // #420：发布上限默认值 10 → 75（与后端模型 + 用户偏好对齐）
  const [dailyLimitGlobal, setDailyLimitGlobal] = useState(75)
  // #596：每账号独立上限 + 算模式 + 两个独立 interval 字段（切 mode 不影响值）
  const [dailyLimitPerAccount, setDailyLimitPerAccount] = useState<Record<
    string, {
      limit: number
      scheduleMode: 'fixed' | 'balanced'
      fixedIntervalMin: number
      balancedStepMin: number
    }
  >>({})
  const [startTime, setStartTime] = useState(defaultStart())
  const [endTime, setEndTime] = useState(defaultEnd())
  // #calc_mode：计算模式（固定间隔 / 均衡间隔）—— 全局模式专用（per_account 模式按行）
  const [scheduleMode, setScheduleMode] = useState<'fixed' | 'balanced'>('balanced')
  const [fixedIntervalMin, setFixedIntervalMin] = useState(10)
  const [balancedStepMin, setBalancedStepMin] = useState(60)
  // #417：自主声明存文本内容（不是序号）：「内容由AI生成」/「无需添加自主声明」
  // #419：默认选「无需添加自主声明」（用户偏好更保守的初始选项）
  const [declaration, setDeclaration] = useState('无需添加自主声明')
  const [allowDownload, setAllowDownload] = useState(false)
  const [taskName, setTaskName] = useState('')

  // #441：wizard 临时化——不再落草稿，无 draftTaskId
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [busy, setBusy] = useState(false)

  /** R14：切步骤统一 helper——同步滚动 dialog body 到顶部 */
  const goStep = useCallback((s: number) => {
    setStep(s)
    requestAnimationFrame(() => {
      const body = document.querySelector('[role="dialog"] [data-radix-scroll-area-viewport]')
        ?? document.querySelector('[role="dialog"] .overflow-y-auto')
        ?? document.querySelector('[role="dialog"] .overflow-auto')
      if (body && 'scrollTo' in body) (body as HTMLElement).scrollTo({ top: 0 })
    })
  }, [])

  // 加载基础数据
  // #442：open=true 时加载 + 重置；open=false 时清空 wizard 状态（关弹即丢）
  // #290 修：仅在 open 切换时跑；不回填任务名（避免覆盖 editingTaskId 的回填）
  useEffect(() => {
    // #review-fix-2：组件卸载兜底 abort（dialog 父组件卸载时 open=false 路径走不到这里）
    return () => {
      scanCtrlRef.current?.abort()
      scanCtrlRef.current = null
    }
  }, [])
  useEffect(() => {
    if (!open) {
      // 关闭 wizard：清所有用户态，避免下次打开残留上次选择
      setSelAccounts([])
      setSelProjects([])
      setProjectDraft({})
      setProjShops({})
      setTaskName('')
      setPreview(null)
      setStep(1)
      setVideoDirs([])
      setManualShopTexts({})
      setProjectSource('project')
      setEditingTaskIdHandled(null)  // #290：清回填标记
      setInitialPayloadHandled(null)  // #453：清复制模式回填标记，避免重复复制同一源命中缓存
      setManualIntroTexts({})  // 视频简介-按项目一一对应：清手动 intro
      // #review-fix-2：dialog 关闭时中断未完成的批量扫描
      scanCtrlRef.current?.abort()
      scanCtrlRef.current = null
      return
    }
    setStep(1)
    setPreview(null)
    // #290：仅「非编辑模式」或「编辑回填已完成」时才设默认名
    if (!editingTaskId) {
      setTaskName(`发布任务_${defaultStart().slice(5, 10).replace('-', '')}_${Date.now() % 100000}`)
    }
    accountApi.list({ status: 'normal', page_size: 100 }).then((r) => setAccounts(r.list))
    // #448：改用 listPublishableProjects（带 shop_count + status='normal' + generated_idle），让 step2 能识别 0 绑店项目并禁用勾选
    publishIntroApi.listPublishableProjects().then((rows) => {
      console.log('[WizardDebug] listPublishableProjects rows:', rows)
      setProjects(rows.map((r) => ({
        ...r,
        shot_count_real: 0, clip_count: 0, generated_total: r.generated_idle ?? 0,
        generated_idle: r.generated_idle ?? 0,
        combination_total: 0, create_time: '',
      })) as unknown as ProjectRow[])
    })
    // #450：删除 /api/selection/shops 调用——wizard 不需要全量店，店从 projShops[pid] 拿
  }, [open])  // #290 修：deps 移除 editingTaskId/accounts/projects（避免 async 加载完再触发）

  // #290：独立 effect 处理编辑回填——accounts/projects 加载完后再回填；标记已处理避免重复
  useEffect(() => {
    if (!open || !editingTaskId) return
    if (editingTaskIdHandled === editingTaskId) return  // 已回填过
    // 等 accounts/projects 都加载完（至少有一个数组长度>0）才回填
    if (accounts.length === 0 || projects.length === 0) return
    publishApi.getTaskDetail(editingTaskId)
      .then((detail) => {
        // 基本字段
        setTaskName(detail.task_name)
        setStartTime(detail.start_time || defaultStart())
        setEndTime(detail.end_time || defaultEnd())
        setScheduleMode(detail.schedule_mode || 'balanced')
        setFixedIntervalMin(detail.fixed_interval_min || 10)
        setBalancedStepMin(detail.balanced_step_min || 60)
        setDeclaration(detail.declaration || '无需添加自主声明')
        setAllowDownload(!!detail.allow_download)
        setDailyLimitMode(detail.daily_limit_mode || 'global')
        setDailyLimitGlobal(detail.daily_limit_global || 75)
        // #596：编辑模式回填分账号上限（每账号从 daily_limit_per_account_json 读完整 record）
        try {
          const perAccFromDB = JSON.parse(detail.daily_limit_per_account_json || '[]') as {
            account_id: string; limit: number
            schedule_mode?: string
            fixed_interval_min?: number; balanced_step_min?: number
            interval_min?: number  // 老字段兼容
          }[]
          if (perAccFromDB.length > 0) {
            const rec: typeof dailyLimitPerAccount = {}
            for (const r of perAccFromDB) {
              // 老数据可能只存 interval_min 单字段——按当前 mode 落到对应字段
              const fallback = r.interval_min || (
                r.schedule_mode === 'fixed' ? fixedIntervalMin : balancedStepMin
              )
              rec[r.account_id] = {
                limit: r.limit,
                scheduleMode: (r.schedule_mode === 'fixed' ? 'fixed' : 'balanced'),
                fixedIntervalMin: r.fixed_interval_min || fallback,
                balancedStepMin: r.balanced_step_min || fallback,
              }
            }
            setDailyLimitPerAccount(rec)
          }
        } catch (e) { console.warn('[Wizard] 编辑模式 per_account 回填失败:', e) }
        // project_source + video_dirs / manual_shops
        const source = detail.project_source || 'project'
        setProjectSource(source)
        if (source === 'video_dir' && detail.video_dirs_json) {
          try {
            const vds = JSON.parse(detail.video_dirs_json) as VideoDirEntry[]
            setVideoDirs(vds)
            const shops = JSON.parse(detail.manual_shops_json || '{}') as Record<string, ShopDraftEntry[]>
            const texts: Record<string, string> = {}
            for (const v of vds) {
              texts[v.id] = (shops[v.id] || []).map((s) => s.name).join('\n')
            }
            setManualShopTexts(texts)
            // 视频简介-按项目一一对应：manual_intros_json = {dir_id: [str, ...]}
            const introsMap = JSON.parse(detail.manual_intros_json || '{}') as Record<string, string[]>
            const introTexts: Record<string, string> = {}
            for (const v of vds) {
              introTexts[v.id] = (introsMap[v.id] || []).join('\n')
            }
            setManualIntroTexts(introTexts)
            // 视频目录模式也要还原已选账号（否则 step 1 已选=0 阻断继续）
            const accountIds = JSON.parse(detail.account_ids_json || '[]') as string[]
            const selAccs = accounts.filter((a) => accountIds.includes(a.id))
            if (selAccs.length > 0) setSelAccounts(selAccs)
          } catch (e) {
            console.warn('[Wizard] 回填 video_dir 失败:', e)
          }
        } else if (source === 'project' && detail.projects_payload_json) {
          // 项目模式回填 selAccounts + selProjects + projectDraft
          try {
            const accountIds = JSON.parse(detail.account_ids_json || '[]') as string[]
            const selAccs = accounts.filter((a) => accountIds.includes(a.id))
            if (selAccs.length > 0) setSelAccounts(selAccs)
            const pp = JSON.parse(detail.projects_payload_json || '[]') as {
              project_id: string; shop_ids?: string[];
              shop_names?: string[]; shop_cities?: string[]; shop_poi_ids?: string[];
            }[]
            const matched: ProjectRow[] = []
            const draftMap: Record<string, ProjectDraftState> = {}
            for (const p of pp) {
              const row = projects.find((x) => x.id === p.project_id)
              if (!row) continue
              matched.push(row)
              const shops = (p.shop_ids || []).map((sid, i) => ({
                id: sid,
                name: p.shop_names?.[i] || '',
                city: p.shop_cities?.[i] || '',
                poi_id: p.shop_poi_ids?.[i] || '',
              }))
              draftMap[p.project_id] = {
                project_id: p.project_id,
                selectedShops: shops,
                selectedIntros: [],
                introMode: 'manual',
              }
            }
            if (matched.length > 0) setSelProjects(matched)
            setProjectDraft(draftMap)
          } catch (e) { console.warn('[Wizard] 项目模式回填失败:', e) }
        }
        setEditingTaskIdHandled(editingTaskId)
      })
      .catch((e) => toast(`加载草稿失败：${(e as Error).message}`, 'error'))
  }, [open, editingTaskId, accounts, projects, editingTaskIdHandled])

  // #453：复制模式回填——initialPayload 直接是 DraftRequest 形态（已是对象，无需 JSON.parse）
  useEffect(() => {
    if (!open || !initialPayload) return
    // 用 task_name 作为简易指纹：同源任务多次复制回填内容一致可命中缓存；不同源自然重新回填
    const sig = initialPayload.task_name || ''
    if (initialPayloadHandled === sig) return  // 已回填过
    if (accounts.length === 0 || projects.length === 0) return

    const p = initialPayload
    setTaskName(p.task_name || '')
    setStartTime(p.start_time || defaultStart())
    setEndTime(p.end_time || defaultEnd())
    setScheduleMode(p.schedule_mode || 'balanced')
    setFixedIntervalMin(p.fixed_interval_min ?? 10)
    setBalancedStepMin(p.balanced_step_min ?? 60)
    setDeclaration(p.declaration || '无需添加自主声明')
    setAllowDownload(!!p.allow_download)
    setDailyLimitMode(p.daily_limit_mode || 'global')
    setDailyLimitGlobal(p.daily_limit_global ?? 75)
    // #596：复制模式回填分账号上限（含每账号算模式 + 两个独立间隔字段，老数据 fallback）
    if (p.daily_limit_per_account && p.daily_limit_per_account.length > 0) {
      const rec: typeof dailyLimitPerAccount = {}
      for (const e of p.daily_limit_per_account) {
        // 老数据可能只存 interval_min 单字段——按当前 mode 落到对应字段
        const fallback = e.interval_min || (
          e.schedule_mode === 'fixed' ? fixedIntervalMin : balancedStepMin
        )
        rec[e.account_id] = {
          limit: e.limit,
          scheduleMode: e.schedule_mode === 'fixed' ? 'fixed' : 'balanced',
          fixedIntervalMin: e.fixed_interval_min || fallback,
          balancedStepMin: e.balanced_step_min || fallback,
        }
      }
      setDailyLimitPerAccount(rec)
    }

    const source = p.project_source || 'project'
    setProjectSource(source)
    if (source === 'video_dir') {
      try {
        const vds = (p.video_dirs || []) as VideoDirEntry[]
        setVideoDirs(vds)
        // #fix-video-count-refresh：复制任务（#453）透传的视频目录含旧 video_count 缓存，
        // 首次显示时并发重新扫描每个目录更新数量，不阻塞用户操作。
        // #review-fix-2：组件卸载 / editingTask 切换时 abort，避免 unmounted setState warning。
        // #review-fix-3：scan 失败时记录 warning 日志（保留旧 video_count，confirm 阶段硬阻塞兜底）。
        scanCtrlRef.current?.abort()
        const ctrl = new AbortController()
        scanCtrlRef.current = ctrl
        if (vds.length > 0) {
          void Promise.allSettled(
            vds.map(async (vd) => {
              if (ctrl.signal.aborted) return null
              try {
                const info = await publishApi.scanVideoDir(vd.abs_path)
                if (ctrl.signal.aborted) return null
                return { id: vd.id, video_count: info.video_count }
              } catch (e) {
                // 静默失败但留日志——目录可能已被用户移走，UI 保留旧 video_count；
                // 后续 confirm / build_schedule 阶段会再 hard-check（abs_path 不存在 → 报错）
                console.warn(
                  `[PublishWizard] video_count 刷新失败，目录不可达: ${vd.abs_path}`,
                  e,
                )
                return null
              }
            }),
          ).then((results) => {
            if (ctrl.signal.aborted) return
            const updates = results.flatMap((r) =>
              r.status === 'fulfilled' && r.value ? [r.value] : [])
            if (updates.length === 0) return
            setVideoDirs((cur) => cur.map((d) => {
              const u = updates.find((x) => x.id === d.id)
              return u ? { ...d, video_count: u.video_count } : d
            }))
          })
        }
        const shops = (p.manual_shops || {}) as Record<string, ShopDraftEntry[]>
        const texts: Record<string, string> = {}
        for (const v of vds) texts[v.id] = (shops[v.id] || []).map((s) => s.name).join('\n')
        setManualShopTexts(texts)
        const introsMap = (p.manual_intros || {}) as Record<string, string[]>
        const introTexts: Record<string, string> = {}
        for (const v of vds) introTexts[v.id] = (introsMap[v.id] || []).join('\n')
        setManualIntroTexts(introTexts)
        const accountIds = p.account_ids || []
        const selAccs = accounts.filter((a) => accountIds.includes(a.id))
        if (selAccs.length > 0) setSelAccounts(selAccs)
      } catch (e) {
        console.warn('[Wizard] 复制模式 video_dir 回填失败:', e)
      }
    } else {
      try {
        const accountIds = p.account_ids || []
        const selAccs = accounts.filter((a) => accountIds.includes(a.id))
        if (selAccs.length > 0) setSelAccounts(selAccs)
        const pp = (p.projects || []) as {
          project_id: string; shop_ids?: string[];
          shop_names?: string[]; shop_cities?: string[]; shop_poi_ids?: string[];
        }[]
        const matched: ProjectRow[] = []
        const draftMap: Record<string, ProjectDraftState> = {}
        for (const proj of pp) {
          const row = projects.find((x) => x.id === proj.project_id)
          if (!row) continue
          matched.push(row)
          const shops = (proj.shop_ids || []).map((sid, i) => ({
            id: sid,
            name: proj.shop_names?.[i] || '',
            city: proj.shop_cities?.[i] || '',
            poi_id: proj.shop_poi_ids?.[i] || '',
          }))
          draftMap[proj.project_id] = {
            project_id: proj.project_id,
            selectedShops: shops,
            selectedIntros: [],
            introMode: 'manual',
          }
        }
        if (matched.length > 0) setSelProjects(matched)
        setProjectDraft(draftMap)
      } catch (e) { console.warn('[Wizard] 复制模式项目回填失败:', e) }
    }
    setInitialPayloadHandled(sig)
  }, [open, initialPayload, accounts, projects, initialPayloadHandled])

  // 同步项目 draft（按 selProjects 的 id 维护 draft key）
  useEffect(() => {
    const selIds = selProjects.map((p) => p.id)
    setProjectDraft((prev) => {
      const next = { ...prev }
      for (const pid of selIds) {
        if (!next[pid]) next[pid] = { project_id: pid, selectedShops: [], selectedIntros: [], introMode: 'list' }
      }
      for (const k of Object.keys(next)) {
        if (!selIds.includes(k)) delete next[k]
      }
      return next
    })
  }, [selProjects])

  // 监听 selProjects，加载每个项目已绑定的门店快照（step3 候选店源）
  // #451：加 in-flight 锁 + alive 标志，防止 selProjects 快速切换时同一 pid 多次请求 + 过期 setState
  useEffect(() => {
    let alive = true
    const inFlight: Set<string> = new Set()
    for (const p of selProjects) {
      // 已加载过（projShops 已有值）→ 跳过，避免重复请求
      if (projShops[p.id] !== undefined) continue
      if (inFlight.has(p.id)) continue
      inFlight.add(p.id)
      publishIntroApi.listProjectShops(p.id)
        .then((rows) => {
          if (!alive) return
          setProjShops((prev) => ({ ...prev, [p.id]: rows }))
        })
        .catch((e) => { if (alive) console.warn('[Wizard] 加载项目门店失败:', e) })
    }
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selProjects])

  // #456：监听 selProjects 加载每个项目已绑定的简介列表（step4 list 模式数据源）
  useEffect(() => {
    let alive = true
    const inFlight: Set<string> = new Set()
    for (const p of selProjects) {
      if (projIntrosLoaded[p.id]) continue
      if (inFlight.has(p.id)) continue
      inFlight.add(p.id)
      publishIntroApi.listIntros(p.id)
        .then((rows) => {
          if (!alive) return
          setProjIntros((prev) => ({ ...prev, [p.id]: rows }))
          setProjIntrosLoaded((prev) => ({ ...prev, [p.id]: true }))
        })
        .catch((e) => {
          if (alive) {
            console.warn('[Wizard] 加载项目简介失败:', e)
            setProjIntrosLoaded((prev) => ({ ...prev, [p.id]: true }))
          }
        })
    }
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selProjects])

  // #447 + #450：projShops 加载完后**不**自动写入 selectedShops，draft.selectedShops 保持空数组，
  // 强制用户主动勾选（或点"全选"按钮）才能进入下一步。
  // #451：删除旧 initedShopSelRef / useEffect（无任何代码读它，纯死代码）。

  // R31 修：模式切换或 selAccounts 变化都同步 per_account 表
  // #596：per_account 扩为 { limit, scheduleMode, fixedIntervalMin, balancedStepMin } record；
  // - 新账号：默认值 limit=dailyLimitGlobal, scheduleMode=balanced, fixed=10, balanced=60
  // - 已有账号：保留原值（来回切不破坏用户已输入）
  // - global 模式不写 per_account（UI 不展示；data 残留无副作用）
  useEffect(() => {
    if (dailyLimitMode !== 'per_account') return
    setDailyLimitPerAccount((prev) => {
      const next = { ...prev }
      const defaultLimit = dailyLimitGlobal >= 1 ? dailyLimitGlobal : 75
      for (const a of selAccounts) {
        const cur = next[a.id]
        if (!cur) {
          next[a.id] = {
            limit: defaultLimit,
            scheduleMode: 'balanced',
            fixedIntervalMin: 10,
            balancedStepMin: 60,
          }
        }
      }
      return next
    })
  }, [dailyLimitGlobal, dailyLimitMode, selAccounts])

  /** 本地校验通过则前进 step（不调后端）。向导中途不应落库。 */
  const advanceStep = useCallback((nextStep: number) => {
    // #459：分别提示任务名空 / 账号未选（之前只笼统说「请选账号」，用户想不到任务名）
    if (nextStep > 1) {
      if (taskName.trim().length === 0) { toast('请先填写任务名', 'error'); return }
      if (selAccounts.length === 0) { toast('请先选择账号（步骤 1）', 'error'); return }
    }
    // #413 真机 bug 修复 U8：进入 step 3 起必须先选项目（步骤 2 才选）
    // 视频简介-按项目一一对应：video_dir 模式下用 videoDirs 代替 selProjects
    if (nextStep > 2 && projectSource === 'project' && selProjects.length === 0) {
      toast('请先选择项目（步骤 2）', 'error'); return
    }
    if (nextStep > 2 && projectSource === 'video_dir' && videoDirs.length === 0) {
      toast('请先添加成品视频目录（步骤 2）', 'error'); return
    }
    // #449 调试：每步点「下一步」前输出当前 step 选中记录，便于排查新建发布任务异常
    // #452：撤掉 DEV 守护，所有环境都打 [WizardDebug] 前缀。
    // 想关掉：DevTools console 跑 `localStorage.setItem('__wizard_debug_off','1')`
    const curStep = nextStep - 1
    if (!localStorage.getItem('__wizard_debug_off') && curStep === 1) {
      console.log('[WizardDebug] step1→2 选中账号:', {
        task_name: taskName,
        account_ids: selAccounts.map((a) => a.id),
        accounts: selAccounts.map((a) => ({
          id: a.id, nickname: a.nickname, remark: a.remark, status: a.status,
        })),
      })
    } else if (!localStorage.getItem('__wizard_debug_off') && curStep === 2) {
      console.log('[WizardDebug] step2→3 选中项目:', {
        project_ids: selProjects.map((p) => p.id),
        projects: selProjects.map((p) => ({
          id: p.id, title: p.title, status: p.status,
          shop_count: (p as unknown as { shop_count?: number } | undefined)?.shop_count,
          generated_idle: p.generated_idle,
        })),
      })
    } else if (!localStorage.getItem('__wizard_debug_off') && curStep === 3) {
      console.log('[WizardDebug] step3→4 挂载门店:', {
        project_ids: selProjects.map((p) => p.id),
        per_project: selProjects.map((p) => {
          const draft = projectDraft[p.id]
            ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
          const allShops = projShops[p.id] ?? []
          return {
            project_id: p.id,
            project_title: p.title,
            selected_shops: draft.selectedShops,
            available_in_db: allShops.length,
            missing_in_db: draft.selectedShops
              .filter((s) => !allShops.some((x) => x.shop_id === s.id)),
          }
        }),
      })
    } else if (!localStorage.getItem('__wizard_debug_off') && curStep === 4) {
      console.log('[WizardDebug] step4→5 视频简介:', {
        project_ids: selProjects.map((p) => p.id),
        per_project: selProjects.map((p) => ({
          project_id: p.id,
          project_title: p.title,
          intros: projectDraft[p.id]?.selectedIntros ?? [],
        })),
      })
    } else if (!localStorage.getItem('__wizard_debug_off') && curStep === 5) {
      console.log('[WizardDebug] step5→6 排期规则:', {
        task_name: taskName,
        daily_limit_mode: dailyLimitMode,
        daily_limit_global: dailyLimitGlobal,
        daily_limit_per_account: dailyLimitPerAccount,
        start_time: startTime,
        end_time: endTime,
        schedule_mode: scheduleMode,
        fixed_interval_min: fixedIntervalMin,
        balanced_step_min: balancedStepMin,
        declaration, allow_download: allowDownload,
      })
    }
    goStep(nextStep)
  }, [selProjects, selAccounts, projects, projectDraft, projShops, taskName,
       dailyLimitMode, dailyLimitGlobal, dailyLimitPerAccount, startTime, endTime,
       scheduleMode, fixedIntervalMin, balancedStepMin,
       declaration, allowDownload, projectSource, videoDirs, goStep])

  // #441：wizard 完全临时操作，关弹即丢，无草稿概念 → 直接关
  const requestClose = useCallback(() => {
    onOpenChange(false)
  }, [onOpenChange])

  /** 拼装当前 wizard 状态为 draft payload（仅 step 7/8 使用）。 */
  const buildDraftPayload = useCallback((): DraftRequest => {
    const safeTaskName = (taskName || `发布任务_${Date.now()}`).slice(0, 50)
    // #450：对象数组形态——直接传账号 / 项目 / 项目内嵌门店 / 项目内嵌简介文本
    const account_snapshots: AccountDraftSnapshot[] = selAccounts.map((a) => ({
      id: a.id, nickname: a.nickname ?? null, remark: a.remark ?? null,
    }))
    const project_snapshots: ProjectDraftSnapshot[] = selProjects.map((p) => {
      const draft = projectDraft[p.id]
        ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
      return {
        id: p.id,
        title: p.title,
        shops: draft.selectedShops,
        intros: draft.selectedIntros,
      }
    })
    return {
      task_name: safeTaskName,
      account_snapshots,
      project_snapshots,
      daily_limit_mode: dailyLimitMode,
      // #595 兜底：state 残留 0/NaN 时强制回 75，避免触发后端 Pydantic 422
      daily_limit_global: dailyLimitMode === 'global' && dailyLimitGlobal >= 1 && dailyLimitGlobal <= 75
        ? dailyLimitGlobal
        : (dailyLimitMode === 'global' ? 75 : undefined),
      daily_limit_per_account: dailyLimitMode === 'per_account'
        ? selAccounts.map<DailyLimitEntry>((a) => {
            const rec = dailyLimitPerAccount[a.id]
              ?? { limit: dailyLimitGlobal >= 1 ? dailyLimitGlobal : 75,
                   scheduleMode: 'balanced' as const,
                   fixedIntervalMin: 10, balancedStepMin: 60 }
            const lim = (typeof rec.limit === 'number' && !Number.isNaN(rec.limit) && rec.limit >= 1) ? rec.limit : 1
            const fixedMin = (typeof rec.fixedIntervalMin === 'number' && !Number.isNaN(rec.fixedIntervalMin) && rec.fixedIntervalMin >= 1) ? rec.fixedIntervalMin : 10
            const balancedMin = (typeof rec.balancedStepMin === 'number' && !Number.isNaN(rec.balancedStepMin) && rec.balancedStepMin >= 1) ? rec.balancedStepMin : 60
            return {
              account_id: a.id,
              account_label: a.nickname || a.remark || '',
              limit: Math.min(lim, 75),
              // #596：分账号下每账号独立算模式 + 两个独立间隔字段（切 mode 不影响值）
              schedule_mode: rec.scheduleMode || 'balanced',
              fixed_interval_min: fixedMin,
              balanced_step_min: balancedMin,
            }
          })
        : [],
      start_time: startTime,
      end_time: endTime,
      // #calc_mode：计算模式 + 间隔
      schedule_mode: scheduleMode,
      fixed_interval_min: scheduleMode === 'fixed' ? fixedIntervalMin : undefined,
      balanced_step_min: scheduleMode === 'balanced' ? balancedStepMin : undefined,
      declaration,
      allow_download: allowDownload,
      // #418：项目来源 + 视频目录 + 手动门店
      project_source: projectSource,
      video_dirs: projectSource === 'video_dir' ? videoDirs : [],
      manual_shops: projectSource === 'video_dir'
        ? Object.fromEntries(videoDirs.map((d) => [
            d.id,
            (manualShopTexts[d.id] ?? '').split('\n')
              .map((line) => line.trim())
              .filter((name) => name.length > 0)
              .map((name, idx) => ({ id: `manual_${d.id}_${idx}`, name, city: '', poi_id: '' })),
          ]))
        : {},
      // 视频简介-按项目一一对应：manual_intros = {dir_id: [str, ...]}
      manual_intros: projectSource === 'video_dir'
        ? Object.fromEntries(videoDirs.map((d) => [
            d.id,
            (manualIntroTexts[d.id] ?? '').split('\n').map((l) => l.trim()).filter((l) => l.length > 0),
          ]))
        : {},
    }
  }, [taskName, selProjects, projectDraft, selAccounts,
       dailyLimitMode, dailyLimitGlobal, dailyLimitPerAccount, startTime, endTime,
       scheduleMode, fixedIntervalMin, balancedStepMin,
       declaration, allowDownload, projectSource, videoDirs, manualShopTexts, manualIntroTexts])

  /** 「预览排期」按钮：纯前端临时算排期（不入库）。
   * #441：wizard 临时化——直接调 previewDirect，不 saveDraft。
   * #439：B 方案——preview 返回 validation.warnings 时，刷新 projShops 缓存 + 跳回 step 3
   *        让用户重选门店，避免 stale 草稿带病进入排期 */
  const computePreview = useCallback(async () => {
    setBusy(true)
    try {
      const payload = buildDraftPayload()
      const p = await publishApi.previewDirect(payload)
      setPreview(p)
      if (p.overflow) {
        toast(p.message || '时间窗不足', 'error')
      } else if (p.validation?.has_warnings) {
        // #439：draft 与 DB 不一致 → 阻断自动跳转，回 step 3（门店选择）
        await Promise.all(selProjects.map((proj) =>
          publishIntroApi.listProjectShops(proj.id)
            .then((rows) => setProjShops((prev) => ({ ...prev, [proj.id]: rows })))
            .catch((e) => console.warn('[Wizard] 刷新项目门店失败:', e))
        ))
        toast(
          `${p.validation.warnings.length} 个项目门店配置已变更，请回第 3 步重新选择`,
          'info'
        )
        goStep(3)
      } else {
        goStep(6)
      }
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }, [buildDraftPayload, selProjects, goStep])

  const confirm = useCallback(async () => {
    // #441 + #418 + #453：编辑模式（已有 draft）调 confirmTask(editingTaskId)；
    // 创建 / 复制模式（initialPayload 来自 duplicate_task 或无 editingTaskId）调 confirmDirect(payload) 建新任务
    setBusy(true)
    try {
      const payload = buildDraftPayload()
      const r = editingTaskId
        ? await publishApi.confirmTask(editingTaskId)
        : await publishApi.confirmDirect(payload)
      toast(`发布任务已启动：${r.item_count} 条待发布（task=${r.task_id.slice(0, 8)}）`, 'success')
      // #413 审查 #18：先 onCreated 刷新列表，再关弹窗，保证数据同步
      // #442：onCreated 异常包 try/catch，避免弹窗未关 + state 半新半旧
      try { await onCreated() } catch (err) { console.warn('[Wizard] onCreated 刷新失败:', err) }
      onOpenChange(false)
    } catch (e) {
      const msg = (e as Error).message || ''
      toast(msg, 'error')
      // #442：按后端 ValueError prefix 决定回退到哪一步（与 _validate_payload 约定）：
      //   [账号]→1 / [项目]→2 / [门店]→3 / [简介]→4 / [时间]→5
      if (msg.startsWith('[账号]')) goStep(1)
      else if (msg.startsWith('[项目]')) goStep(2)
      else if (msg.startsWith('[门店]')) goStep(3)
      else if (msg.startsWith('[简介]')) goStep(4)
      else goStep(5)
    } finally {
      setBusy(false)
    }
  }, [buildDraftPayload, onOpenChange, onCreated, goStep, editingTaskId])

  /** #418：保存草稿（preview 步骤加按钮）—— 调 saveDraft，状态=draft，用户可在任务列表编辑/启动 */
  const saveDraftAndClose = useCallback(async () => {
    setBusy(true)
    try {
      const payload = buildDraftPayload()
      const r = await publishApi.saveDraft(payload)
      toast(`已保存到草稿：${r.task_id.slice(0, 8)}`, 'success')
      try { await onCreated() } catch (err) { console.warn('[Wizard] onCreated 刷新失败:', err) }
      onOpenChange(false)
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }, [buildDraftPayload, onOpenChange, onCreated])

  // #413 真机 bug 修复 U2：每步校验通过才允许 advance，防止空账号/空项目落库失败
  // #420 修：step 5 同样校验间隔（后端 Field(ge=1) 会拒 0）
  // #428：7 步 → 6 步；「声明 / 下载」合并到 step 5（与「排期规则」同一步）
  // #456：step 4 视频简介允许为空（不勾/不输入表示不添加视频简介，后端支持空数组）
  const canAdvance = (() => {
    if (step === 1) return selAccounts.length > 0 && taskName.trim().length > 0
    // #418：视频目录模式 step 2 校验=videoDirs 非空；项目模式=selProjects 非空
    if (step === 2) return projectSource === 'video_dir' ? videoDirs.length > 0 : selProjects.length > 0
    // #418：视频目录模式 step 3 校验=每个目录手动输入门店非空；项目模式=每个项目 selectedShops 非空
    if (step === 3) {
      if (projectSource === 'video_dir') {
        return videoDirs.every((d) => {
          const lines = (manualShopTexts[d.id] ?? '').split('\n').map((s) => s.trim()).filter(Boolean)
          return lines.length > 0
        })
      }
      return selProjects.every((p) => (projectDraft[p.id]?.selectedShops.length ?? 0) > 0)
    }
    if (step === 4) {
      // 视频简介-按项目一一对应：每个视频目录都必须至少 1 条 intro
      if (projectSource === 'video_dir') {
        return videoDirs.every((d) => {
          const lines = (manualIntroTexts[d.id] ?? '').split('\n').map((s) => s.trim()).filter(Boolean)
          return lines.length > 0
        })
      }
      return true
    }
    if (step === 5) {
      // #596 + #597：发布模式 + 排期时间 + 间隔统一校验
      if (!startTime || !endTime || startTime >= endTime) return false
      // #597：起始时间必须晚于当前（兜底硬拦截，避免手填过去时间）
      if (startTime <= localNowStr()) return false
      if (dailyLimitMode === 'global') {
        if (!(dailyLimitGlobal >= 1)) return false
        // #calc_mode：固定模式 ≥ 1，均衡模式 ≥ 1
        if (scheduleMode === 'fixed') return fixedIntervalMin >= 1
        return balancedStepMin >= 1
      }
      // 分账号：每账号 limit ≥ 1 + 两个 interval 字段都 ≥ 1
      return selAccounts.every((a) => {
        const rec = dailyLimitPerAccount[a.id]
        if (!rec || rec.limit < 1) return false
        return rec.fixedIntervalMin >= 1 && rec.balancedStepMin >= 1
      })
    }
    return true
  })()

  // #418：进度条——当前步大圆环+主色阴影+加粗+主色文字；已完成 ok 色 + ✓；未到灰显
  // 已完成 / 当前 步骤可点击跳转（goStep 已做边界校验），未来步骤不可点
  // 作为粘性头部渲染：滚动 dialog 内容时进度条始终可见
  // #428：进度条整体居中；当前步骤圆环明确显示 idx 数字
  // #429：子 div 改 inline-flex + mx-auto 才能真正居中（block flex 子项 width:100% justify-center 无效）；
  //       连接线固定宽（w-12），不再 flex-1 撑满；圆环主色 background / ring 改 tailwind 的 accent（无 primary）
  const headerNav = (
    <div className="inline-flex items-center">
      {STEP_TITLES.map((t, i) => {
        const idx = i + 1
        const done = idx < step
        const cur = idx === step
        const reachable = idx <= step  // 当前及之前可点
        return (
          <Fragment key={t}>
            <button type="button" disabled={!reachable}
              onClick={() => reachable && goStep(idx)}
              className={cn('flex shrink-0 items-center gap-2 rounded-md px-1 py-0.5 transition-colors',
                reachable && !cur && 'hover:bg-muted cursor-pointer',
                !reachable && 'cursor-not-allowed opacity-60',
                cur && 'cursor-default')}>
              <span className={cn('flex h-7 w-7 items-center justify-center rounded-full text-sm font-bold transition-all',
                // 当前步：主色实心 + ring 高亮（accent 才是 tailwind 中已定义的主色）
                cur && 'bg-accent text-white ring-2 ring-accent/30',
                done && 'bg-ok text-white',
                !cur && !done && 'bg-muted text-muted-foreground')}>
                {idx}
              </span>
              <span className={cn('text-sm whitespace-nowrap',
                cur && 'font-bold text-accent',
                done && 'font-medium text-ok',
                !cur && !done && 'text-muted-foreground')}>
                {t}
              </span>
            </button>
            {idx < STEP_TITLES.length && (
              // 连接线固定宽：让进度条整体宽度 = 6 按钮 + 5 连接线，由 inline-flex + mx-auto 居中
              <div className={cn('mx-2 h-0.5 w-12 shrink-0 rounded',
                idx < step ? 'bg-ok' : 'bg-border')} />
            )}
          </Fragment>
        )
      })}
    </div>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="新建发布任务" width={960}
      height={720}
      stickyHeader={headerNav}
      footer={
        <div className="flex w-full items-center justify-between">
          <Button variant="outline" size="sm" disabled={step === 1} onClick={() => goStep(step - 1)}>
            上一步
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={requestClose}>取消</Button>
            {step < 5 && (
              <Button size="sm" disabled={busy || !canAdvance}
                onClick={() => advanceStep(step + 1)}>
                下一步
              </Button>
            )}
            {step === 5 && (
              <Button size="sm" disabled={busy} onClick={computePreview}>
                {busy ? '计算中…' : '预览排期'}
              </Button>
            )}
            {step === 6 && (
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={busy || preview?.overflow} onClick={saveDraftAndClose}>
                  {busy ? '保存中…' : '保存到草稿'}
                </Button>
                <Button size="sm" disabled={busy || preview?.overflow || preview?.validation?.has_warnings}
                  onClick={confirm}>
                  {busy ? '确认中…' : '确认创建并启动'}
                </Button>
              </div>
            )}
          </div>
        </div>
      }>
      {step === 1 && (
        <div>
          <label className="mb-1 block text-sm font-medium">任务名</label>
          <input className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm"
            maxLength={50}
            value={taskName} onChange={(e) => setTaskName(e.target.value)} />
          <div className="mt-1 text-xs text-muted-foreground">{taskName.length}/50</div>
          <div className="mt-3 mb-1 flex items-center gap-3 text-xs">
            <span className="shrink-0 whitespace-nowrap text-sm font-medium">发布账号（仅正常状态）</span>
            <span className="ml-auto flex shrink-0 items-center gap-2 whitespace-nowrap">
              <span className="text-muted-foreground">
                共 {accounts.length} 个 / 已选 {selAccounts.length}
              </span>
              {/* #459：反选 = 对 accounts 列表真正翻转（勾→不勾、不勾→勾），
                  保留 cur 中不属于 accounts 的历史项 */}
              <button type="button" className="text-accent hover:underline"
                onClick={() => setSelAccounts(accounts)}>全选</button>
              <button type="button" className="text-accent hover:underline"
                onClick={() => setSelAccounts((cur) => {
                  const selIds = new Set(cur.map((x) => x.id))
                  const accMap = new Map(accounts.map((a) => [a.id, a]))
                  // accounts 列表内：翻转
                  const flipped = accounts.map((a) =>
                    selIds.has(a.id) ? null : a
                  ).filter((x): x is Account => x !== null)
                  // accounts 之外：保留（cur 中的非 accounts 项）
                  const others = cur.filter((x) => !accMap.has(x.id))
                  // 去重合并
                  const seen = new Set<string>()
                  return [...flipped, ...others].filter((x) => {
                    if (seen.has(x.id)) return false
                    seen.add(x.id); return true
                  })
                })}>反选</button>
            </span>
          </div>
          <div className="max-h-72 space-y-1 overflow-auto rounded-md border border-border p-2">
            {accounts.map((a) => {
              const checked = selAccounts.some((x) => x.id === a.id)
              return (
                <label key={a.id} className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={checked}
                    onChange={(e) => setSelAccounts((p) => e.target.checked
                      ? [...p, a] : p.filter((x) => x.id !== a.id))} />
                  {a.remark || a.nickname}
                </label>
              )
            })}
          </div>
        </div>
      )}

      {step === 2 && (
        <div>
          {/* #418：项目来源切换（项目列表 / 成品视频目录） */}
          <div className="mb-3 flex items-center gap-3 text-sm">
            <span className="shrink-0 whitespace-nowrap text-sm font-medium">项目来源</span>
            <label className="flex items-center gap-1 whitespace-nowrap">
              <input type="radio" checked={projectSource === 'project'}
                onChange={() => setProjectSource('project')} />
              项目列表
            </label>
            <label className="flex items-center gap-1 whitespace-nowrap">
              <input type="radio" checked={projectSource === 'video_dir'}
                onChange={() => setProjectSource('video_dir')} />
              成品视频目录
            </label>
          </div>
          {projectSource === 'video_dir' ? (
            /* #418：成品视频目录模式 */
            <div className="space-y-2">
              <div className="flex gap-2">
                <Button size="sm" variant="outline" disabled={!!videoDirBusy}
                  onClick={async () => {
                    // 阶段 1：系统目录对话框（用户可能取消，不算异常）
                    setVideoDirBusy('selecting')
                    // 取当前 videoDirs 最后一个的 abs_path 作为 dialog 起点（纯内存 hint，不持久化）
                    const lastAbs = videoDirs.length > 0 ? videoDirs[videoDirs.length - 1].abs_path : undefined
                    const p = await window.electronAPI.openDirectory(lastAbs)
                    if (!p) { setVideoDirBusy(null); return }
                    // 阶段 2：API 扫描（耗时 100ms~数秒，按目录视频数）
                    setVideoDirBusy('scanning')
                    try {
                      const info = await publishApi.scanVideoDir(p)
                      // #418：目录去重（Windows 不区分大小写）
                      const dup = videoDirs.some((d) =>
                        d.abs_path.toLowerCase() === info.abs_path.toLowerCase())
                      if (dup) {
                        toast('该目录已添加，无需重复', 'error')
                        return
                      }
                      const newDir: VideoDirEntry = {
                        id: `vd_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
                        abs_path: info.abs_path,
                        dir_name: info.dir_name,
                        video_count: info.video_count,
                      }
                      setVideoDirs([...videoDirs, newDir])
                      // 初始化 manual shop text
                      setManualShopTexts((prev) => ({ ...prev, [newDir.id]: '' }))
                    } catch (e) {
                      toast((e as Error).message, 'error')
                    } finally {
                      setVideoDirBusy(null)
                    }
                  }}>
                  {videoDirBusy === 'selecting' && <><Spinner />选择目录中…</>}
                  {videoDirBusy === 'scanning' && <><Spinner />扫描中…</>}
                  {!videoDirBusy && '+ 添加目录'}
                </Button>
              </div>
              <div className="max-h-72 space-y-1 overflow-auto rounded-md border border-border p-2">
                {videoDirs.length === 0 ? (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
                    尚未添加目录，点击「+ 添加目录」选择成品视频所在文件夹
                  </div>
                ) : videoDirs.map((d) => (
                  <div key={d.id} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                    <button type="button"
                      className="text-xl hover:opacity-70"
                      title={`定位到 ${d.abs_path}`}
                      onClick={() => window.electronAPI.openDirInExplorer(d.abs_path)}>
                      📁
                    </button>
                    <span className="flex-1 font-medium" title={d.abs_path}>{d.dir_name}</span>
                    <span className="text-xs text-muted-foreground">{d.video_count} 个视频</span>
                    <button type="button" className="text-danger hover:underline"
                      onClick={() => {
                        setVideoDirs(videoDirs.filter((x) => x.id !== d.id))
                        setManualShopTexts((prev) => {
                          const next = { ...prev }; delete next[d.id]; return next
                        })
                      }}>×</button>
                  </div>
                ))}
              </div>
              <div className="text-xs text-muted-foreground">
                每个目录视为一个项目；门店将在第 3 步手动输入
              </div>
            </div>
          ) : (
          /* 原项目列表模式 */
          <div>
          {(() => {
            const available = projects
              .filter((t) => (t.status ?? 'normal') === 'normal')
              .slice()
              .sort((a, b) => (b.create_time || '').localeCompare(a.create_time || ''))
            return (
              <>
                <div className="mb-1 flex items-center gap-3 text-xs">
                  <span className="shrink-0 whitespace-nowrap text-sm font-medium">发布项目（含可用成品数）</span>
                  <span className="ml-auto flex shrink-0 items-center gap-2 whitespace-nowrap">
                    <span className="text-muted-foreground">
                      共 {available.length} 个 / 已选 {selProjects.length}
                    </span>
                    {/* #459：全选 = 排除未绑店项目（避免后端硬校验拦截 + step3 无店可选）；
                        反选 = 对 available 真正翻转（未绑店项目始终不进 selProjects）。 */}
                    <button type="button" className="text-accent hover:underline"
                      onClick={() => setSelProjects(
                        available.filter((t) =>
                          ((t as unknown as { shop_count?: number }).shop_count ?? 0) > 0
                        )
                      )}>全选</button>
                    <button type="button" className="text-accent hover:underline"
                      onClick={() => setSelProjects((cur) => {
                        const selIds = new Set(cur.map((x) => x.id))
                        const avMap = new Map(available.map((t) => [t.id, t]))
                        // available 内：翻转（且只挑非未绑店）
                        const flipped = available
                          .filter((t) => ((t as unknown as { shop_count?: number }).shop_count ?? 0) > 0
                            && !selIds.has(t.id))
                        // available 之外：保留
                        const others = cur.filter((x) => !avMap.has(x.id))
                        const seen = new Set<string>()
                        return [...flipped, ...others].filter((x) => {
                          if (seen.has(x.id)) return false
                          seen.add(x.id); return true
                        })
                      })}>反选</button>
                  </span>
                </div>
                <div className="max-h-72 space-y-1 overflow-auto rounded-md border border-border p-2">
                  {/* #416：停用项目（status='disabled'）不展示；#417：按创建时间倒序显示；
                      #448：0 绑店项目禁用勾选（避免 step3 canAdvance 卡死 + 后端硬校验拦截） */}
                  {available.map((t) => {
                    // #448：listPublishableProjects 返回的 row 含 shop_count；type assertion 已转 ProjectRow，运行时访问 OK
                    const shopCount = (t as unknown as { shop_count?: number }).shop_count ?? 0
                    const noShops = shopCount === 0
                    const checked = selProjects.some((x) => x.id === t.id)
                    return (
                    <label key={t.id} className={`flex items-center gap-2 text-sm ${noShops ? 'opacity-60' : ''}`}>
                      <input type="checkbox" disabled={noShops}
                        checked={!noShops && checked}
                        onChange={(e) => setSelProjects((p) => e.target.checked
                          ? [...p, t] : p.filter((x) => x.id !== t.id))} />
                      <span className="flex-1">{t.title}</span>
                      <span className={noShops ? 'text-amber-600' : (t.generated_idle > 0 ? 'text-ok' : 'text-danger')}>
                        {noShops ? '未绑店' : `成品 ${t.generated_idle}`}
                      </span>
                    </label>
                    )
                  })}
                </div>
              </>
            )
          })()}
          </div>
          )}
        </div>
      )}

      {step === 3 && (
        <div className="space-y-3">
          {/* #418：视频目录模式显示每个目录的 manual shop 输入 */}
          {projectSource === 'video_dir' ? (
            videoDirs.length === 0 ? (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
                请先在第 2 步添加成品视频目录
              </div>
            ) : videoDirs.map((d) => {
              const lines = (manualShopTexts[d.id] ?? '').split('\n').map((s) => s.trim()).filter(Boolean)
              return (
                <div key={d.id} className="rounded-md border border-border p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-sm font-medium">📁 {d.dir_name}</span>
                    <span className="text-xs text-muted-foreground">已输入 {lines.length} 家</span>
                  </div>
                  <textarea
                    className="block min-h-[100px] w-full rounded-md border border-border bg-white p-2 font-mono text-sm"
                    placeholder={'每行一个门店名称（必填）\n例：\n寿司店\n烧烤店\n咖啡店'}
                    value={manualShopTexts[d.id] ?? ''}
                    onChange={(e) => setManualShopTexts((prev) => ({ ...prev, [d.id]: e.target.value }))}
                  />
                  <div className="mt-1 text-xs text-muted-foreground">
                    门店名称必填；发布时按名称在创作者中心匹配
                  </div>
                </div>
              )
            })
          ) : (
          /* 原项目模式门店选择 */
          selProjects.map((p) => {
            const draft = projectDraft[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
            // #413 走查 #7 W1：只显示项目已绑定的门店，避免全量 200+ 误导
            const projShopList = projShops[p.id]
            // #450：把 ProjectShopItem 转成 ShopDraftEntry 对象数组（统一 store 结构）
            const toShopEntry = (s: { shop_id: string; shop_name?: string | null; city?: string | null; poi_id?: string | null }): ShopDraftEntry => ({
              id: s.shop_id,
              name: s.shop_name ?? '',
              city: s.city ?? '',
              poi_id: s.poi_id ?? '',
            })
            return (
              <div key={p.id} className="rounded-md border border-border p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-sm font-medium">{p.title} · 挂载门店</span>
                  {projShopList && projShopList.length > 0 && (
                    <span className="text-xs text-muted-foreground">
                      共 {projShopList.length} 家 / 已选 {draft.selectedShops.length}
                      <button type="button" className="ml-2 text-accent hover:underline"
                        onClick={() => setProjectDraft((prev) => ({
                          ...prev,
                          [p.id]: {
                            ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                            selectedShops: projShopList.map(toShopEntry),
                          },
                        }))}>全选</button>
                      <button type="button" className="ml-2 text-accent hover:underline"
                        onClick={() => setProjectDraft((prev) => {
                          const cur = prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
                          const curIds = new Set(cur.selectedShops.map((s) => s.id))
                          const next = projShopList
                            .filter((s) => !curIds.has(s.shop_id))
                            .map(toShopEntry)
                          return { ...prev, [p.id]: { ...cur, selectedShops: next } }
                        })}>反选</button>
                    </span>
                  )}
                </div>
                {!projShopList ? (
                  <div className="rounded-md border border-border p-2 text-xs text-muted-foreground">加载中…</div>
                ) : projShopList.length === 0 ? (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
                    该项目未绑定任何门店，请先到「创作中心 › 门店」抽屉绑定
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="max-h-44 space-y-1 overflow-auto rounded-md border border-border p-2">
                      {projShopList.map((s) => {
                        const checked = draft.selectedShops.some((x) => x.id === s.shop_id)
                        return (
                          <label key={s.shop_id} className="flex items-center gap-2 text-sm">
                            <input type="checkbox" checked={checked}
                              onChange={(e) => setProjectDraft((prev) => {
                                const cur = prev[p.id]
                                  ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
                                const entry = toShopEntry(s)
                                const next = e.target.checked
                                  ? (cur.selectedShops.some((x) => x.id === s.shop_id)
                                      ? cur.selectedShops
                                      : [...cur.selectedShops, entry])
                                  : cur.selectedShops.filter((x) => x.id !== s.shop_id)
                                return { ...prev, [p.id]: { ...cur, selectedShops: next } }
                              })} />
                            <span className="flex-1">{s.shop_name ?? s.shop_id}</span>
                          </label>
                        )
                      })}
                    </div>
                  </div>
                )}
              </div>
            )
          })
          )}
        </div>
      )}

      {step === 4 && (
        <div className="space-y-3">
          {/* 视频简介-按项目一一对应：每个视频目录独立一份简介文本 */}
          {projectSource === 'video_dir' ? (
            videoDirs.length === 0 ? (
              <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                请先在第 2 步添加成品视频目录
              </div>
            ) : (
              <div className="space-y-3">
                <div className="text-sm font-medium">视频简介（每个目录独立一份，每行一条）</div>
                {videoDirs.map((d) => (
                  <div key={d.id} className="space-y-1 rounded-md border border-border p-3">
                    <div className="flex items-center justify-between gap-3 text-xs">
                      <div className="font-medium truncate" title={d.abs_path}>{d.dir_name}</div>
                      <div className="text-muted-foreground whitespace-nowrap">
                        {(manualIntroTexts[d.id] ?? '').split('\n').map((l) => l.trim()).filter(Boolean).length} 条
                      </div>
                    </div>
                    <textarea
                      className="block min-h-[100px] w-full rounded-md border border-border bg-white p-2 font-mono text-sm"
                      placeholder={`每行一条视频简介（仅用于「${d.dir_name}」下的视频）\n例：\n#美食 寿司拼盘 治愈你的夏天\n咖啡店的午后时光`}
                      value={manualIntroTexts[d.id] ?? ''}
                      onChange={(e) => setManualIntroTexts((prev) => ({ ...prev, [d.id]: e.target.value }))}
                    />
                  </div>
                ))}
                <div className="text-xs text-muted-foreground">
                  发布时按各自目录的视频顺序循环使用；每个目录至少输入 1 条
                </div>
              </div>
            )
          ) : (
          <Fragment>
          {selProjects.map((p) => {
            const draft = projectDraft[p.id]
              ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }
            const intros = draft.selectedIntros
            return (
              <div key={p.id} className="rounded-md border border-border p-3">
                <div className="mb-2 flex items-center justify-between gap-3 text-xs">
                  <div className="flex shrink-0 items-center gap-3">
                    <span className="text-sm font-medium whitespace-nowrap">{p.title} · 视频简介</span>
                    <label className="flex items-center gap-1 whitespace-nowrap">
                      <input type="radio"
                        checked={draft.introMode === 'list'}
                        onChange={() => setProjectDraft((prev) => ({
                          ...prev,
                          [p.id]: { ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                            introMode: 'list', selectedIntros: [] },
                        }))} />
                      已绑定列表
                    </label>
                    <label className="flex items-center gap-1 whitespace-nowrap">
                      <input type="radio"
                        checked={draft.introMode === 'manual'}
                        onChange={() => setProjectDraft((prev) => ({
                          ...prev,
                          [p.id]: { ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                            introMode: 'manual', selectedIntros: [] },
                        }))} />
                      手动输入
                    </label>
                  </div>
                  {/* 右侧：统计 + 全选/反选，与标题同一行靠右 */}
                  {draft.introMode === 'list' ? (
                    <IntroListActions
                      intros={projIntros[p.id] ?? []}
                      selectedIds={intros.map((it) => it.id)}
                      onChange={(items) => setProjectDraft((prev) => ({
                        ...prev,
                        [p.id]: {
                          ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                          selectedIntros: items,
                        },
                      }))}
                    />
                  ) : (
                    <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                      已输入 {intros.length} 条（每行一条；可为空）
                    </span>
                  )}
                </div>
                {draft.introMode === 'list' ? (
                  <IntroListPicker
                    intros={projIntros[p.id] ?? []}
                    loaded={projIntrosLoaded[p.id] ?? false}
                    selectedIds={intros.map((it) => it.id)}
                    onChange={(items) => setProjectDraft((prev) => ({
                      ...prev,
                      [p.id]: {
                        ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                        selectedIntros: items,
                      },
                    }))}
                  />
                ) : (
                  <IntroManualEditor
                    // #453：key 强制 mode 切换时重新挂载 textarea，避开 list picker 残留事件
                    key={`manual_${p.id}`}
                    projectId={p.id}
                    selectedIntros={intros}
                    onChange={(items) => setProjectDraft((prev) => ({
                      ...prev,
                      [p.id]: {
                        ...(prev[p.id] ?? { project_id: p.id, selectedShops: [], selectedIntros: [], introMode: 'list' }),
                        selectedIntros: items,
                      },
                    }))}
                  />
                )}
              </div>
            )
          })}
          </Fragment>
          )}
        </div>
      )}

      {step === 5 && (
        <div className="space-y-4">
          {/* #596：合并「每天上限 + 计算模式」为「发布模式」一节；
              - 全局：1 行（每天上限 + 计算模式 select + 间隔 input）
              - 分账号：每账号独立行（账号 \| 每天上限 \| 计算模式 select \| 间隔 input） */}
          <div>
            <label className="mb-1 block text-sm font-medium">发布模式</label>
            <div className="flex items-center gap-3 text-sm">
              <label className="flex items-center gap-1">
                <input type="radio" checked={dailyLimitMode === 'global'}
                  onChange={() => setDailyLimitMode('global')} />全局
              </label>
              <label className="flex items-center gap-1">
                <input type="radio" checked={dailyLimitMode === 'per_account'}
                  onChange={() => setDailyLimitMode('per_account')} />分账号
              </label>
            </div>
            {dailyLimitMode === 'global' ? (
              /* 全局：每天上限 + 计算模式（select）+ 间隔（input） */
              <div className="mt-2 space-y-2">
                <div className="flex flex-wrap items-center gap-3">
                  <label className="flex items-center gap-2 text-sm">
                    <span className="shrink-0 text-muted-foreground">每天上限</span>
                    <input type="number" min={1} max={75}
                      className="h-9 w-32 rounded-md border border-border bg-white px-3 text-sm"
                      value={Number.isNaN(dailyLimitGlobal) ? '' : dailyLimitGlobal}
                      onChange={(e) => {
                        const v = parseInt(e.target.value, 10)
                        // #413 真机 bug 修复 U6：清空/0 自动回 75，避免发出非法值触发后端 400 toast
                        if (Number.isNaN(v) || v < 1) {
                          toast('每天上限不能为空，已自动设为 75', 'info')
                          setDailyLimitGlobal(75)
                        } else if (v > 75) {
                          toast('每天上限不能超过 75', 'info')
                          setDailyLimitGlobal(75)
                        } else {
                          setDailyLimitGlobal(v)
                        }
                      }} />
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <span className="shrink-0 text-muted-foreground">计算模式</span>
                    <select
                      className="h-9 rounded-md border border-border bg-white px-2 text-sm"
                      value={scheduleMode}
                      onChange={(e) => setScheduleMode(e.target.value as 'fixed' | 'balanced')}>
                      <option value="fixed">固定间隔</option>
                      <option value="balanced">均衡间隔</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <span className="shrink-0 text-muted-foreground">间隔（分钟）</span>
                    {scheduleMode === 'fixed' ? (
                      <input type="number" min={1}
                        className="h-9 w-24 rounded-md border border-border bg-white px-3 text-sm"
                        value={Number.isNaN(fixedIntervalMin) ? '' : fixedIntervalMin}
                        onChange={(e) => {
                          const v = parseInt(e.target.value, 10)
                          setFixedIntervalMin(Number.isNaN(v) ? 0 : v)
                        }} />
                    ) : (
                      <input type="number" min={1}
                        className="h-9 w-24 rounded-md border border-border bg-white px-3 text-sm"
                        value={Number.isNaN(balancedStepMin) ? '' : balancedStepMin}
                        onChange={(e) => {
                          const v = parseInt(e.target.value, 10)
                          setBalancedStepMin(Number.isNaN(v) ? 0 : v)
                        }} />
                    )}
                  </label>
                </div>
              </div>
            ) : (
              /* 分账号：表（账号 \| 每天上限 \| 计算模式 select \| 间隔 input） */
              <div className="mt-2 overflow-auto rounded-md border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-xs text-muted-foreground">
                      <th className="px-2 py-2">账号</th>
                      <th className="px-2 py-2">每天上限</th>
                      <th className="px-2 py-2">计算模式</th>
                      <th className="px-2 py-2">间隔（分钟）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selAccounts.map((a) => {
                      const rec = dailyLimitPerAccount[a.id]
                        ?? { limit: dailyLimitGlobal >= 1 ? dailyLimitGlobal : 75,
                             scheduleMode: 'balanced' as const,
                             fixedIntervalMin: 10, balancedStepMin: 60 }
                      // #596：两个独立 interval 字段，按当前 mode 显对应——切 mode 不丢另一字段值
                      const currentInterval = rec.scheduleMode === 'fixed'
                        ? rec.fixedIntervalMin
                        : rec.balancedStepMin
                      const updateField = (patch: Partial<typeof rec>) => {
                        const cur = dailyLimitPerAccount[a.id]
                          ?? { limit: 0, scheduleMode: 'balanced' as const,
                               fixedIntervalMin: 10, balancedStepMin: 60 }
                        setDailyLimitPerAccount((p) => ({
                          ...p, [a.id]: { ...cur, ...patch },
                        }))
                      }
                      return (
                        <tr key={a.id} className="border-b last:border-0">
                          <td className="px-2 py-1.5">{a.remark || a.nickname || a.id}</td>
                          <td className="px-2 py-1.5">
                            <input type="number" min={1} max={75}
                              className="h-8 w-24 rounded-md border border-border bg-white px-2 text-sm"
                              value={rec.limit === 0 ? '' : rec.limit}
                              onChange={(e) => {
                                const v = parseInt(e.target.value, 10)
                                if (!Number.isNaN(v) && v > 75) {
                                  toast('每天上限不能超过 75', 'info')
                                  updateField({ limit: 75 })
                                } else {
                                  updateField({ limit: Number.isNaN(v) ? 0 : v })
                                }
                              }} />
                          </td>
                          <td className="px-2 py-1.5">
                            <select
                              className="h-8 rounded-md border border-border bg-white px-2 text-sm"
                              value={rec.scheduleMode}
                              onChange={(e) => {
                                updateField({ scheduleMode: e.target.value as 'fixed' | 'balanced' })
                              }}>
                              <option value="fixed">固定间隔</option>
                              <option value="balanced">均衡间隔</option>
                            </select>
                          </td>
                          <td className="px-2 py-1.5">
                            <input type="number" min={1}
                              className="h-8 w-24 rounded-md border border-border bg-white px-2 text-sm"
                              value={currentInterval === 0 ? '' : currentInterval}
                              onChange={(e) => {
                                const v = parseInt(e.target.value, 10)
                                // 写到当前 mode 对应字段（切 mode 不丢另一字段值）
                                if (rec.scheduleMode === 'fixed') {
                                  updateField({ fixedIntervalMin: Number.isNaN(v) ? 0 : v })
                                } else {
                                  updateField({ balancedStepMin: Number.isNaN(v) ? 0 : v })
                                }
                              }} />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium">起始发布时间</label>
              {/* #597：datetime-local picker — 浏览器原生日期时间选择器，避免手填格式错误；
                  min=now 本地化字符串（浏览器内置拦截过去时间），value 转换 HH:mm ↔ T HH:mm */}
              <input type="datetime-local" step={60}
                className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm"
                min={localNowMin()}
                value={startTime.replace(' ', 'T')}
                onChange={(e) => setStartTime(e.target.value.replace('T', ' '))} />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">结束发布时间</label>
              {/* #597：min=startTime 强制 end > start（start 已是合法的未来时间） */}
              <input type="datetime-local" step={60}
                className="h-9 w-full rounded-md border border-border bg-white px-3 text-sm"
                min={startTime.replace(' ', 'T')}
                value={endTime.replace(' ', 'T')}
                onChange={(e) => setEndTime(e.target.value.replace('T', ' '))} />
            </div>
            {/* #596：计算模式 + 间隔已收纳进上方「发布模式」段（全局/分账号各自展示），
                此处不再重复。保留起始/结束时间 + 提示。 */}
            {startTime && endTime && startTime >= endTime && (
              <div className="col-span-2 text-xs text-danger">结束时间必须晚于起始时间</div>
            )}
            {/* #597：起始时间不能是过去；与后端 _validate_payload 双重拦截 */}
            {startTime && startTime <= localNowStr() && (
              <div className="col-span-2 text-xs text-danger">起始时间必须晚于当前</div>
            )}
            <div className="col-span-2 text-xs text-muted-foreground">
              默认起始 = 明天 07:00，结束 = 明天 22:00。固定模式：所有账号共用固定间隔顺排；均衡模式：每项目独占时段内等距（步长 = 均衡间隔）
            </div>
          </div>
          {/* #428：「声明 / 下载」合并到 step 5 */}
          <div className="border-t border-border pt-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-sm font-medium">自主声明</label>
                {/* #417：保存文本内容（不是序号）；后端 declaration 字段为 string，直接存选项文案 */}
                <select className="h-9 w-full rounded-md border border-border bg-white px-2 text-sm"
                  value={declaration}
                  onChange={(e) => setDeclaration(e.target.value)}>
                  <option value="内容由AI生成">内容由 AI 生成</option>
                  <option value="无需添加自主声明">无需添加自主声明</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">是否允许下载</label>
                <div className="flex h-9 items-center gap-3 text-sm">
                  <label className="flex items-center gap-1">
                    <input type="radio" checked={!allowDownload}
                      onChange={() => setAllowDownload(false)} />不允许
                  </label>
                  <label className="flex items-center gap-1">
                    <input type="radio" checked={allowDownload}
                      onChange={() => setAllowDownload(true)} />允许
                  </label>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {step === 6 && preview && (
        <div className="space-y-2">
          {/* #450：把 wizard 内每个项目选中的店名汇总成 shopNameById 字典，
              供 PreviewPane 渲染时取（不再依赖全量 shops）。 */}
          <PreviewPane preview={preview}
            shopNameById={Object.fromEntries(
              selProjects.flatMap((p) =>
                (projectDraft[p.id]?.selectedShops ?? []).map((s) => [s.id, s.name])
              )
            )}
            projects={projects}
            accounts={accounts} startTime={startTime} endTime={endTime}
            onJumpToShops={() => goStep(3)} />
        </div>
      )}
    </Dialog>
  )
}

// ============ #452 + #456 step4 子组件 ============

/** #456：列表模式头部右侧「统计 + 全选 + 反选」。由 wizard 把 intros + onChange 传进来。 */
function IntroListActions({ intros, selectedIds, onChange }: {
  intros: VideoIntro[]
  selectedIds: string[]
  onChange: (items: { id: string; content: string; topics?: string[] }[]) => void
}) {
  const toItem = (r: VideoIntro) => ({
    id: r.id, content: r.content, topics: r.topics ?? [],
  })
  return (
    <span className="flex shrink-0 items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
      <span>共 {intros.length} 条 / 已选 {selectedIds.length}</span>
      <button type="button" className="text-accent hover:underline"
        onClick={() => onChange(intros.map(toItem))}>全选</button>
      <button type="button" className="text-accent hover:underline"
        onClick={() => {
          const sel = new Set(selectedIds)
          onChange(intros.filter((r) => !sel.has(r.id)).map(toItem))
        }}>反选</button>
    </span>
  )
}

/** #456：list 模式——纯渲染 checkbox 列表（不再含头部统计 / 全选反选，移到了 IntroListActions）。 */
function IntroListPicker({ intros, loaded, selectedIds, onChange }: {
  intros: VideoIntro[]
  loaded: boolean
  selectedIds: string[]
  onChange: (items: { id: string; content: string; topics?: string[] }[]) => void
}) {
  if (!loaded) {
    return <div className="rounded-md border border-border p-2 text-xs text-muted-foreground">加载中…</div>
  }
  if (intros.length === 0) {
    return <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
      该项目暂无简介，请到「创作中心 › 简介」抽屉添加，或切换到「手动输入」
    </div>
  }
  const toItem = (r: VideoIntro) => ({
    id: r.id, content: r.content, topics: r.topics ?? [],
  })
  return (
    <div className="max-h-44 space-y-1 overflow-auto rounded-md border border-border p-2 text-sm">
      {intros.map((it) => (
        <label key={it.id} className="flex items-start gap-2">
          <input type="checkbox" checked={selectedIds.includes(it.id)}
            onChange={(e) => {
              if (e.target.checked) {
                const cur = intros.filter((r) => selectedIds.includes(r.id)).map(toItem)
                if (!cur.some((x) => x.id === it.id)) cur.push(toItem(it))
                onChange(cur)
              } else {
                onChange(intros
                  .filter((r) => selectedIds.includes(r.id) && r.id !== it.id)
                  .map(toItem))
              }
            }} />
          <span className="flex-1 truncate" title={it.content}>{it.content}</span>
        </label>
      ))}
    </div>
  )
}

/** manual 模式：多行文本框输入（每行一条简介，#451 稳定 id 派生）。 */
function IntroManualEditor({ projectId, selectedIntros, onChange }: {
  projectId: string
  selectedIntros: { id: string; content: string; topics?: string[] }[]
  onChange: (items: { id: string; content: string; topics?: string[] }[]) => void
}) {
  const text = selectedIntros.map((it) => it.content).join('\n')
  return (
    <div>
      <textarea
        // #453：首次切换手动输入模式时 textarea 不可编辑。
        // 原因：textarea 之前是 list picker 的 DOM 子树，React 条件渲染切到 manual 时
        //   focus 被 list picker 残留抢走 + onChange 被 React batch 延迟。
        // 修：显式 autoFocus + readOnly={false} + 用 useRef 强制 mount 后 focus
        autoFocus
        readOnly={false}
        className="block min-h-[140px] w-full rounded-md border border-border bg-white p-2 font-mono text-sm"
        placeholder={'每行一条视频简介，例如：\n寿司拼盘，治愈你的夏天 #美食 #日料\n今天的快乐是一碗鳗鱼饭 #探店'}
        value={text}
        onChange={(e) => {
          // #451：按 index + pid 派生稳定 id，避免每敲一个字符全部 id 重生成
          const lines = e.target.value.split('\n')
          const next = lines
            .map((line, idx) => ({ idx, content: line.trim() }))
            .filter((x) => x.content.length > 0)
            .map(({ idx, content }) => ({
              id: `intro_${projectId}_${idx}`,
              content,
              topics: [],
            }))
          onChange(next)
        }}
      />
      <div className="mt-1 text-xs text-muted-foreground">
        关闭 wizard 后会丢失——这是 wizard 临时输入，不入库到「视频简介」表
      </div>
    </div>
  )
}

// ---------- 步骤 8：预览 ----------

function PreviewPane({ preview, shopNameById, projects, accounts, startTime, endTime, onJumpToShops }: {
  preview: PreviewResult
  /** #450：店 id → 店名 字典（wizard 拼自每个项目的 selectedShops）。 */
  shopNameById: Record<string, string>
  projects: ProjectRow[]
  accounts: Account[]
  startTime: string
  endTime: string
  /** #439：点击 banner 上的「回到第 3 步重选门店」按钮回调 */
  onJumpToShops?: () => void
}) {
  const projMap = new Map(projects.map((p) => [p.id, p.title]))
  const accMap = new Map(accounts.map((a) => [a.id, a.remark || a.nickname || a.id]))

  if (preview.overflow) {
    return (
      <div className="rounded-md bg-red-50 p-3 text-sm text-danger">
        时间窗不足：{preview.message}（{startTime} → {endTime}）
        <div className="mt-1 text-xs">返回上一步调整起始/结束时间，或减少每天上限。</div>
      </div>
    )
  }

  // #439：draft 与 DB 不一致 → 顶部黄色 banner + 一键回 step3
  const warnings = preview.validation?.warnings ?? []
  const shopName = (sid: string) => shopNameById[sid] ?? sid.slice(0, 8)

  // #426 + #454-补：按账号分组渲染，每组独立表格；组内明细就是该账号的过滤结果
  // #454-改：序号改用组内 1..M（不再用全局 1..N），每账号组自 1 开始
  const grouped = new Map<string, PreviewItem[]>()
  for (const it of preview.items) {
    const arr = grouped.get(it.account_id) ?? []
    arr.push(it)
    grouped.set(it.account_id, arr)
  }

  return (
    <div className="space-y-3">
      {/* #439：draft.selectedShops 与 DB.project_shop 不一致 → 警告 banner，提示用户回 step3 重选 */}
      {warnings.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <div className="mb-1 font-medium">
            ⚠ {warnings.length} 个项目的门店配置与数据库不一致，请回第 3 步重新选择门店
          </div>
          <ul className="space-y-1 text-xs">
            {warnings.map((w) => (
              <li key={w.project_id}>
                <span className="font-medium">{projMap.get(w.project_id) ?? w.project_id.slice(0, 8)}</span>
                {w.stale_in_draft.length > 0 && (
                  <span className="ml-2">
                    · draft 中 {w.stale_in_draft.length} 家店当前不可用：
                    {w.stale_in_draft.slice(0, 3).map(shopName).join('、')}
                    {w.stale_in_draft.length > 3 ? '…' : ''}
                  </span>
                )}
                {w.extra_in_draft.length > 0 && (
                  <span className="ml-2">
                    · DB 已删但草稿残留 {w.extra_in_draft.length} 家：
                    {w.extra_in_draft.slice(0, 3).map(shopName).join('、')}
                    {w.extra_in_draft.length > 3 ? '…' : ''}
                  </span>
                )}
              </li>
            ))}
          </ul>
          {onJumpToShops && (
            <button type="button" onClick={onJumpToShops}
              className="mt-2 rounded bg-amber-600 px-2 py-1 text-xs font-medium text-white hover:bg-amber-700">
              回到第 3 步重新选择
            </button>
          )}
        </div>
      )}
      {/* #432 + #456：去掉顶部统计栏（preview.items 实际总数 + 各账号上限对比） */}
      {Array.from(grouped.entries()).map(([accountId, items]) => (
        // #431：分组卡片用主色左边框 + 浅主色背景，让分组边界更醒目（之前只是普通 border）
        <div key={accountId} className="overflow-hidden rounded-md border border-border">
          <div className="flex items-center justify-between border-b border-border bg-accent/5 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="rounded bg-accent px-2 py-0.5 text-xs font-medium text-white">
                {accMap.get(accountId) ?? accountId}
              </span>
              {/* #456：去掉「账号 ID：xxx」短码 */}
            </div>
            <span className="text-xs font-medium text-muted-foreground">
              共 {items.length} 条
            </span>
          </div>
          {/* #427：分组卡片样式与 step3 门店 / step4 简介同款；单账号明细较多时组内可滚动 */}
          <div className="max-h-60 overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted-foreground">
                  {/* #454-改：组内序号 1..M（每账号组自 1 开始，不跨账号全局递增） */}
                  <th className="w-10 px-2 py-1">序号</th>
                  <th className="px-2 py-1">计划时间</th>
                  <th className="px-2 py-1">项目</th>
                  <th className="px-2 py-1">门店</th>
                  <th className="px-2 py-1">简介</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => (
                  <tr key={i} className="border-t border-border">
                    <td className="px-2 py-1.5 text-muted-foreground">{i + 1}</td>
                    <td className="px-2 py-1.5 font-mono">{it.plan_time}</td>
                    <td className="px-2 py-1.5">
                      {/* #418：video_dir 模式后端回填 project_title（= dir_name），与 wizard step2 显示一致 */}
                      {it.project_title ?? projMap.get(it.project_id) ?? it.project_id}
                    </td>
                    <td className="px-2 py-1.5">{shopNameById[it.shop_id] ?? it.shop_id}</td>
                    {/* #428：line-clamp-2 在 td 内不生效，改在 td 内的 div 上做：
                        max-width + display:-webkit-box + -webkit-line-clamp:2 + overflow:hidden */}
                    <td className="px-2 py-1.5" style={{ maxWidth: 240 }}>
                      <div className="break-words leading-snug line-clamp-2"
                        title={it.intro_content}>
                        {it.intro_content}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  )
}