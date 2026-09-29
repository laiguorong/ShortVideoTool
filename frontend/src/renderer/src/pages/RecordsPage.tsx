import { useCallback, useEffect, useState } from 'react'
import { publishApi, type PublishRecord } from '@/api/publish'
import { request } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Pagination } from '@/components/shared/Pagination'
import { toast } from '@/components/ui/toast'

/** 发布记录页（#456 重构：6 列表 + 入参实时查询） */
export function RecordsPage() {
  const [data, setData] = useState<{ list: PublishRecord[]; total: number }>({ list: [], total: 0 })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [keyword, setKeyword] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    const params: Record<string, string> = { page: String(page), page_size: String(pageSize) }
    if (keyword) params.keyword = keyword
    if (startDate) params.start_time = `${startDate} 00:00:00`
    if (endDate) params.end_time = `${endDate} 23:59:59`
    publishApi.listRecords(params)
      .then((r) => setData({ list: r.list, total: r.total }))
      .catch((e) => console.warn('[Records] 请求失败:', e))
      .finally(() => setLoading(false))
  }, [page, pageSize, keyword, startDate, endDate])
  useEffect(load, [load])

  return (
    <div className="flex h-full flex-col p-5">
      {/* #456：去掉「共 X 条」「导出 Excel」，只剩入参筛选；任一入参变化直接触发查询 */}
      <div className="mb-4 flex items-center gap-3">
        <h1 className="flex h-8 items-center text-lg font-semibold">发布记录</h1>
        <input type="date" className="h-8 rounded-md border border-border bg-white px-2 text-sm"
          value={startDate} onChange={(e) => { setStartDate(e.target.value); setPage(1) }} />
        <span className="text-muted-foreground">至</span>
        <input type="date" className="h-8 rounded-md border border-border bg-white px-2 text-sm"
          value={endDate} onChange={(e) => { setEndDate(e.target.value); setPage(1) }} />
        <input className="h-8 w-48 rounded-md border border-border bg-white px-2.5 text-sm"
          placeholder="搜索简介/账号/门店"
          value={keyword}
          onChange={(e) => { setKeyword(e.target.value); setPage(1) }} />
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          {loading ? '查询中…' : '刷新'}
        </Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-background-elev">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">计划发布时间</th>
              <th className="px-4 py-2.5 font-medium">账号</th>
              <th className="px-4 py-2.5 font-medium">视频</th>
              <th className="px-4 py-2.5 font-medium">门店</th>
              <th className="px-4 py-2.5 font-medium">视频简介</th>
              <th className="px-4 py-2.5 font-medium">创建时间</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {data.list.map((r) => {
              // 「视频简介」：优先 intro_snapshot（#413 发布的视频介绍快照），否则退化 publish_title
              const intro = r.intro_snapshot || r.publish_title
              // 「计划发布时间」：来自 publish_task_item.plan_time（v42 JOIN）；缺失退化为 publish_time
              const planTime = r.plan_time || r.publish_time
              // 「预览」：必须拿到线上视频 ID 且明细非失败
              const showPreview = !!r.online_video_id && r.item_status !== 'failed'
              return (
                <tr key={r.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                  <td className="px-4 py-2.5 font-mono text-xs">{planTime}</td>
                  <td className="px-4 py-2.5">{r.account_snapshot || '-'}</td>
                  <td className="max-w-44 px-4 py-2.5 font-mono text-xs text-accent"
                    title={r.video_title}>
                    <span className="line-clamp-2 block">{r.video_title || r.video_local_path.split(/[\\/]/).pop()}</span>
                  </td>
                  <td className="max-w-40 px-4 py-2.5 text-xs"
                    title={r.shop_snapshot ?? ''}>
                    <span className="line-clamp-2 block">{r.shop_snapshot?.split('|')[0] || '-'}</span>
                  </td>
                  <td className="max-w-52 px-4 py-2.5 font-medium" title={intro}>
                    <span className="line-clamp-2 block">{intro}</span>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs">{r.create_time}</td>
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {showPreview && window.electronAPI && (
                        <Button variant="outline" size="sm"
                          onClick={() => window.electronAPI!.openExternal(
                            `https://www.douyin.com/video/${r.online_video_id}`)}>
                          预览
                        </Button>
                      )}
                      {window.electronAPI && (
                        <Button variant="outline" size="sm"
                          onClick={async () => {
                            try {
                              const resp = await request<{ abs_path: string; exists: boolean }>(
                                `/publish/records/${r.id}/abs-path`)
                              if (resp.exists) window.electronAPI!.openPath(resp.abs_path)
                              else toast('成品文件不存在', 'error')
                            } catch (e) { toast((e as Error).message, 'error') }
                          }}>定位</Button>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
            {data.list.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-12 text-center text-muted-foreground">暂无发布记录</td></tr>
            )}
          </tbody>
        </table>
        </div>
        <div className="px-4"><Pagination page={page} pageSize={pageSize} total={data.total} onChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(1) }} /></div>
      </div>
    </div>
  )
}