import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** 类名合并工具（shadcn 惯例） */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
