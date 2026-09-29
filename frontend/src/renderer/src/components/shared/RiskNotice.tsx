import { Dialog } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { settingApi } from '@/api/setting'

/** 首次启动风险告知弹窗（F-08.6 初始化闭环 / 第 8 章告知文案） */
export function RiskNoticeDialog({
  open,
  onConfirm,
}: {
  open: boolean
  onConfirm: () => void
}) {
  /** 确认：落库后不再弹出；拒绝：仅提示（退出由用户手动关闭应用） */
  const confirm = async (value: boolean) => {
    if (value) {
      try {
        await settingApi.riskConfirm(true)
        onConfirm()
      } catch {
        /* 网络异常时下次启动仍会提示 */
      }
    } else {
      // 拒绝承担风险：不进入功能使用，关闭确认（弹窗保留提示）
      window.alert('需确认风险告知后方可使用本工具，关闭应用后可重新打开')
    }
  }

  return (
    <Dialog open={open} onOpenChange={() => {}} title="风险告知" width={520}>
      <div className="space-y-3 text-sm leading-6">
        <p className="font-medium">使用本工具前，请务必知晓以下内容：</p>
        <ol className="list-decimal space-y-1.5 pl-5 text-muted-foreground">
          <li>本工具为个人效率工具，依赖短视频平台 Web 接口，非官方开放能力；</li>
          <li>平台规则可能限制自动化行为，过度使用可能影响账号权重（限流、封禁等）；</li>
          <li>请控制使用频率、遵守平台协议，因使用本工具产生的账号风险由您自行承担；</li>
          <li>所有数据（含账号 Cookie）仅存储在本地，不上传任何服务器。</li>
        </ol>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="outline" size="sm" onClick={() => confirm(false)}>
          拒绝并退出
        </Button>
        <Button size="sm" onClick={() => confirm(true)}>
          已知晓，同意并继续
        </Button>
      </div>
    </Dialog>
  )
}
