// -*- coding: utf-8 -*-
/** 启动检查 API 封装（与后端 app/api/startup.py 对应）。 */

import { request } from './client'

export interface CheckResult {
  key: string
  status: 'pending' | 'running' | 'ok' | 'failed'
  detail: string
  /** 步骤相关数据。后端 data_dir 步骤会写 need_choose=true 通知前端弹选择数据目录弹窗 */
  data: { need_choose?: boolean; [key: string]: unknown }
  ts: string
}

/** 7 步启动检查（步骤 1 后端存活由 Electron 主进程判定，渲染层只管 2-7） */
export const STARTUP_STEPS: { key: string; label: string; description: string }[] = [
  { key: 'data_dir',   label: '数据目录',  description: '检查应用配置文件、初始化数据目录' },
  { key: 'database',   label: '数据库迁移', description: '建表 + 数据迁移 + 通知清理' },
  { key: 'settings',   label: '系统配置',  description: '加载应用配置 + 加密密钥' },
  { key: 'ffmpeg',     label: 'ffmpeg',   description: '检测 ffmpeg 可执行 + libx264 编码器' },
  { key: 'playwright', label: 'Chromium', description: '检测 ms-playwright Chromium 路径' },
  { key: 'scheduler',  label: '调度服务',  description: '启动定时任务 + 拉取任务恢复 + 缓存清理' },
]

/** 拉取所有步骤当前状态（页面刷新/初次挂载用） */
export async function getState(): Promise<Record<string, CheckResult>> {
  return request<Record<string, CheckResult>>('/startup/state')
}

/** 执行某步（已 ok 返缓存，其他重跑） */
export async function check(key: string): Promise<CheckResult> {
  return request<CheckResult>(
    `/startup/check/${encodeURIComponent(key)}`,
    { method: 'POST' },
  )
}

/** 设置数据目录（步骤 2 用户选完路径调） */
export async function setDataDir(path: string): Promise<{ ok: boolean; data_dir: string }> {
  return request<{ ok: boolean; data_dir: string }>(
    '/startup/data-dir',
    { method: 'POST', body: JSON.stringify({ path }) },
  )
}
