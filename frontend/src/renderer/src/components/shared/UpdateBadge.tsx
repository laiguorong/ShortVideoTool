import { useUpdateStore, triggerUpdateCheck, clickUpdateBadge } from '@/store/updateStore'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

/** 版本号 + 转圈 + "有更新" 红色徽标
 *
 * 两种 UI 形态：
 *  A. 无新版本（idle / upToDate / error / checking / downloading 刚发现瞬间）
 *     → 显示版本号 + 可能的转圈
 *  B. 有新版本（downloading / ready）→ 显示红色"有更新"徽标
 *
 * 交互（按用户钦定 spec）：
 *  - 形态 A：点版本号 → 触发检查更新
 *  - 形态 B：点"有更新" → 正在下载弹 toast / 已下完执行安装
 *
 * @param variant 'about' = 关于弹窗里（大），'footer' = 底栏（小）
 */
export function UpdateBadge({ variant = 'footer' }: { variant?: 'about' | 'footer' }) {
  const { status } = useUpdateStore()
  const hasUpdate = status === 'downloading' || status === 'ready'
  const isSpinning = status === 'checking' || status === 'downloading'

  if (hasUpdate) {
    return (
      <button
        type="button"
        onClick={clickUpdateBadge}
        className={cn(
          'inline-flex items-center gap-1.5 transition-opacity hover:opacity-80',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded',
        )}
        title={status === 'ready' ? '点击安装更新' : '正在下载，点击查看'}
      >
        <Badge variant="danger" className="text-[10px] leading-tight">
          有更新
        </Badge>
        {isSpinning && <Spinner className="h-3 w-3" />}
      </button>
    )
  }

  // title 随状态变化：checking 时不要再说"点击检查"，避免误导
  const versionTitle = status === 'checking' ? '正在检查更新…' : '点击检查更新'

  return (
    <button
      type="button"
      onClick={triggerUpdateCheck}
      className={cn(
        'inline-flex items-center gap-1.5 transition-opacity hover:opacity-80',
        'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded',
        variant === 'about'
          ? 'bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground'
          : 'text-xs text-muted-foreground',
      )}
      title={versionTitle}
    >
      <span>v{__APP_VERSION__}</span>
      {isSpinning && <Spinner className="h-3 w-3" />}
    </button>
  )
}

/** 极简转圈：CSS-only，2px 圆环 + 透明顶部 + animate-spin */
function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="loading"
      className={cn(
        'inline-block animate-spin rounded-full border-2 border-current border-t-transparent',
        className,
      )}
    />
  )
}