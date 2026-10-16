import { Dialog } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

/** 关于系统弹窗（顶栏 ❓ 按钮触发）
 *  内容包含：系统版本号 / 所有权 / 系统说明 / 开源协议 / 免责声明 / 支持作者
 *  使用 build-time 注入的 __APP_VERSION__ 全局常量（来源：frontend/package.json） */
export function AboutDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="关于系统"
      width={600}
      // 固定高度启用滚动：5 节内容较多，超过视口时中间区域滚动，避免整个 dialog 顶到屏外
      height={520}
      footer={
        <Button size="sm" onClick={() => onOpenChange(false)}>
          关闭
        </Button>
      }
    >
      <div className="space-y-5 text-sm">
        {/* 1. 系统版本号 */}
        <section>
          <div className="flex items-center gap-2">
            <span className="font-medium">系统版本号</span>
            <span className="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
              v{__APP_VERSION__}
            </span>
          </div>
        </section>

        {/* 2. 所有权说明 */}
        <section className="space-y-1">
          <h3 className="font-medium">所有权</h3>
          <p className="text-muted-foreground">
            Copyright © 2026 荣爵.
            本程序基于 <span className="font-medium text-foreground">GNU AGPL v3.0</span> 开源发布。
          </p>
        </section>

        {/* 3. 系统说明 */}
        <section className="space-y-1">
          <h3 className="font-medium">系统说明</h3>
          <p className="text-muted-foreground">
            一站式个人短视频平台运营工作台：
            <span className="font-medium text-foreground">账号 → 选品 → 素材 → 创作 → 发布 → 数据</span> 全流程本地化管理。
          </p>
          <p className="text-xs text-muted-foreground">
            所有数据（含账号 Cookie）仅存储在本地，不上传任何服务器。
          </p>
        </section>

        {/* 4. 开源协议 */}
        <section className="space-y-1">
          <h3 className="font-medium">开源协议</h3>
          <p className="text-muted-foreground">
            基于 <span className="font-medium text-foreground">GNU Affero General Public License v3.0</span>（AGPL-3.0）开源，
            完整协议文本参见{' '}
            <a
              href="https://www.gnu.org/licenses/agpl-3.0.html"
              className="text-accent hover:underline"
              onClick={(e) => {
                e.preventDefault()
                // Electron 下走系统默认浏览器；浏览器开发态直接新窗打开
                // 参考 MaterialsPage:336 的成熟模式（preload 暴露 openExternal，IPC 走 shell:openExternal）
                const url = 'https://www.gnu.org/licenses/agpl-3.0.html'
                if (window.electronAPI) window.electronAPI.openExternal(url)
                else window.open(url, '_blank', 'noreferrer')
              }}
            >
              GNU 官方页面
            </a>
            。
          </p>
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
            <li>你可以自由地使用、学习、修改、再分发本程序的源码；</li>
            <li>若基于本程序修改并通过网络对外提供服务，必须同样以 AGPL-3.0 开源修改后的完整源码；</li>
            <li>任何衍生作品必须保留版权声明与协议文本。</li>
          </ul>
        </section>

        {/* 5. 免责声明 */}
        <section className="space-y-1">
          <h3 className="font-medium">免责声明</h3>
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
            <li>
              <span className="font-medium text-foreground">源码仅供学习研究使用</span>：
              以「按原样」（AS IS）原则提供，仅供学习、技术研究与合法合规二次开发参考。
            </li>
            <li>
              <span className="font-medium text-foreground">程序可以免费使用</span>：
              可免费用于个人和非商业用途；商业用途须严格遵守 AGPL-3.0 协议条款。
            </li>
            <li>
              <span className="font-medium text-foreground">不承担任何责任</span>：
              不承担因使用、修改或再分发本程序引起的任何直接或间接损失，包括但不限于
              <span className="text-danger">数据丢失、数据损坏、设备故障、第三方平台账号封禁、接口变更</span>
              等。
            </li>
          </ul>
        </section>

        <p className="border-t border-border pt-3 text-xs text-muted-foreground">
          使用、修改或再分发本程序即视为您已阅读、理解并同意上述声明。
        </p>

        {/* 6. 支持作者（微信赞赏码，图片存于 frontend/public/wechatpay.png，vite 原样输出到 dist 根） */}
        <section className="space-y-2">
          <h3 className="font-medium">支持作者</h3>
          <p className="text-xs text-muted-foreground">
            如果这个工具对你有帮助，欢迎扫码支持一杯咖啡 ☕
          </p>
          <div className="flex justify-center">
            <img
              src="./wechatpay.png"
              alt="微信赞赏码"
              // 160x160 方形居中，border 让二维码在浅色/深色背景下都有清晰轮廓
              className="h-40 w-40 rounded border border-border"
              draggable={false}
            />
          </div>
        </section>
      </div>
    </Dialog>
  )
}