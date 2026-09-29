import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

/** 按钮变体（UI-3：危险操作红色） */
const variants = {
  primary: 'bg-accent text-white hover:bg-accent-hover',
  secondary: 'bg-muted text-foreground hover:bg-border',
  outline: 'border border-border bg-background-elev hover:bg-muted',
  ghost: 'hover:bg-muted text-muted-foreground hover:text-foreground',
  danger: 'bg-danger text-white hover:bg-red-700',
}

const sizes = {
  sm: 'h-7 px-2.5 text-xs',
  md: 'h-9 px-4 text-sm',
  lg: 'h-10 px-6 text-base',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof variants
  size?: keyof typeof sizes
}

/** 通用按钮 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'primary', size = 'md', ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        'inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors',
        'disabled:pointer-events-none disabled:opacity-50',
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  ),
)
Button.displayName = 'Button'
