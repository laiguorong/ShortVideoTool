import type { HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

const variants = {
  default: 'bg-muted text-muted-foreground',
  success: 'bg-green-50 text-ok border border-green-200',
  info: 'bg-blue-50 text-accent border border-blue-200',
  warning: 'bg-amber-50 text-warn border border-amber-200',
  danger: 'bg-red-50 text-danger border border-red-200',
  neutral: 'bg-gray-100 text-gray-500 border border-gray-200',
}

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: keyof typeof variants
}

/** 状态徽章（UI-5 状态色彩语义） */
export function Badge({ className, variant = 'default', ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        variants[variant],
        className,
      )}
      {...props}
    />
  )
}
