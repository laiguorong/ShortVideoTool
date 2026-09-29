import { Badge } from '@/components/ui/badge'

/** 状态文本 → 中文标签映射（供其他组件判断"message 是否与 status 默认文案重复"） */
export const STATUS_LABEL: Record<string, string> = {
  // 账号
  normal: '正常',
  undetected: '未检测',
  invalid: 'Cookie 失效',
  disabled: '已停用',
  // 定时任务
  enabled: '已启用',
  // 门店标记
  favorite: '已收藏',
  excluded: '已排除',
  // 后台/生成/发布任务
  pending: '等待中',
  waiting: '等待中',
  running: '执行中',
  completed: '已完成',
  publishing: '发布中',
  suspended: '已挂起',
  paused: '已暂停',
  success: '成功',
  partial: '部分成功',
  failed: '失败',
  cancelled: '已取消',
  finished: '已完成',
  draft: '草稿',
  // 成品视频
  idle: '未占用',
  occupied: '已占用',
  // 发布记录
  removed: '已删除',
  // 素材文件
  missing: '文件缺失',
}

/** 状态文本 → 徽章变体映射（各模块状态通用，UI-5 色彩语义）。label 从 STATUS_LABEL 派生，保持单一来源 */
const STATUS_MAP: Record<string, { variant: 'success' | 'info' | 'warning' | 'danger' | 'neutral' | 'default' }> = {
  normal: { variant: 'success' },
  undetected: { variant: 'neutral' },
  invalid: { variant: 'danger' },
  disabled: { variant: 'neutral' },
  enabled: { variant: 'success' },
  favorite: { variant: 'info' },
  excluded: { variant: 'neutral' },
  pending: { variant: 'neutral' },
  waiting: { variant: 'neutral' },
  running: { variant: 'info' },
  completed: { variant: 'success' },
  publishing: { variant: 'info' },
  suspended: { variant: 'warning' },
  paused: { variant: 'warning' },
  success: { variant: 'success' },
  partial: { variant: 'warning' },
  failed: { variant: 'danger' },
  cancelled: { variant: 'neutral' },
  finished: { variant: 'success' },
  draft: { variant: 'neutral' },
  idle: { variant: 'success' },
  occupied: { variant: 'info' },
  removed: { variant: 'neutral' },
  missing: { variant: 'danger' },
}

/** 状态徽章：传入状态英文枚举自动映射中文标签与颜色 */
export function StatusBadge({ status }: { status: string }) {
  const item = STATUS_MAP[status]
  const label = STATUS_LABEL[status] ?? status
  const variant = item?.variant ?? 'default'
  return <Badge variant={variant}>{label}</Badge>
}
