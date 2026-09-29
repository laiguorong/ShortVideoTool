# -*- coding: utf-8 -*-
"""Playwright 浏览器执行器：模拟真人操作绕过 JS 签名墙（B 方案）。

背景：抖音/精选联盟部分接口需要 a_bogus/msToken 签名（前端 JS 生成），
后端复现成本高且易失效。改为起真实 Chromium 加载页面——签名由页面自己算，
本模块只负责：注入登录态 Cookie → 导航到目标页 → 拦截 XHR 响应 → 滚动翻页。

设计：
- 模块级单例 browser_actor；
- 每个线程独立持有一份 (playwright, browser)——Playwright sync API 的
  runtime/context/page 全部绑创建线程，跨线程访问必抛
  "greenlet.error: Cannot switch to a different thread"。threading.local
  在 BrowserActor 实例上让每个线程访问 _tls.* 时取自己线程的 storage。
- 每个 task 独立 BrowserContext（Cookie 隔离，用完即关）；
- 同步 Playwright API（task_service worker 线程调用，不碰事件循环）；
- run() 全局串行：避免多 context 并发 nav/XHR 互相干扰（#131 实测偶发 0 响应）。
"""

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

# 反自动化注入脚本（页面加载前注入，清 webdriver / chrome runtime / languages / plugins）
# #140：顺带注入固定顶部警示条（headed 用户可见，headless 无害），MutationObserver 保活防 SPA 重渲染清掉
# #451：抖音 2025+ 指纹检测升级，需同时覆盖 webdriver / cdc_ / chrome.runtime / WebGL vendor
_ANTI_BOT_INIT_SCRIPT = (
    # 1. navigator.webdriver = false（覆盖 Playwright 默认 true）
    "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
    "delete Navigator.prototype.webdriver;"
    # 2. 删 cdc_ 系列变量（Chrome DevTools Protocol 痕迹，最容易穿帮）
    "Object.keys(window).filter(k => k.startsWith('cdc_') || k.startsWith('$cdc_') || k.startsWith('__$cdc_')).forEach(k => { try { delete window[k]; } catch(e){} });"
    # 3. chrome.runtime 完整对象（让 isTrusted 行为通过）
    "window.chrome = {"
    "runtime: {"
    "onInstalled: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onMessage: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onMessageExternal: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onConnect: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onConnectExternal: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onUpdateAvailable: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onStartup: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onSuspend: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onSuspendCanceled: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "onRestartRequired: {addListener: () => {}, removeListener: () => {}, hasListener: () => false},"
    "sendMessage: () => {},"
    "connect: () => {},"
    "connectNative: () => {},"
    "getURL: () => '',"
    "getManifest: () => ({})"
    "},"
    "csi: () => ({startE: 1, onloadT: Date.now()}),"
    "loadTimes: () => ({requestTime: Date.now() / 1000}),"
    "app: {"
    "isInstalled: false,"
    "InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},"
    "RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'},"
    "getDetails: () => null,"
    "getIsInstalled: () => false,"
    "installState: 'not_installed',"
    "runningState: 'ready_to_run'"
    "}"
    "};"
    # 4. navigator.plugins 真实 Chrome 列表（5 个 PDF viewer）
    "Object.defineProperty(navigator, 'plugins', {get: () => {"
    "const m = (name, file) => ({name, filename: file, description: 'Portable Document Format', length: 1});"
    "const arr = [m('PDF Viewer', 'internal-pdf-viewer'), m('Chrome PDF Viewer', 'internal-pdf-viewer'),"
    "m('Chromium PDF Viewer', 'internal-pdf-viewer'), m('Microsoft Edge PDF Viewer', 'internal-pdf-viewer'),"
    "m('WebKit built-in PDF', 'internal-pdf-viewer')];"
    "arr.item = i => arr[i];"
    "arr.namedItem = n => arr.find(x => x.name === n);"
    "arr.refresh = () => {};"
    "return arr;"
    "}});"
    # 5. navigator.languages 完整 zh 列表
    "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});"
    # 6. WebGL vendor / renderer 伪装真实 Intel 集显（Playwright 默认 SwiftShader → 立刻穿帮）
    "(() => {"
    "const orig = WebGLRenderingContext.prototype.getParameter;"
    "WebGLRenderingContext.prototype.getParameter = function(p) {"
    "if (p === 37445) return 'Intel Inc.';"
    "if (p === 37446) return 'Intel Iris OpenGL Engine';"
    "return orig.call(this, p);"
    "};"
    "const orig2 = WebGL2RenderingContext.prototype.getParameter;"
    "WebGL2RenderingContext.prototype.getParameter = function(p) {"
    "if (p === 37445) return 'Intel Inc.';"
    "if (p === 37446) return 'Intel Iris OpenGL Engine';"
    "return orig2.call(this, p);"
    "};"
    "})();"
    # 7. permissions.query（notifications 默认 prompt 真实行为）
    "const _q = navigator.permissions && navigator.permissions.query;"
    "if (_q) { navigator.permissions.query = (p) => {"
    "if (p && p.name === 'notifications') return Promise.resolve({state: Notification.permission});"
    "return _q.call(navigator.permissions, p);"
    "}; }"
    # 8. 顶部警示条（保留 #140）
    "(function(){"
    "var T='⚠ 请不要手动关闭本窗口，系统处理完会自动关闭！';"
    "function up(){var b=document.body;if(!b)return;"
    "var e=document.getElementById('__tw');"
    "if(!e){e=document.createElement('div');e.id='__tw';"
    "e.style.cssText='position:fixed;top:0;left:50%;transform:translateX(-50%);"
    "width:fit-content;max-width:100vw;height:48px;line-height:48px;"
    "font:16px/48px sans-serif;padding:0 24px;box-sizing:border-box;"
    "background:#fef3c7;color:#92400e;border:1px solid #f59e0b;border-top:none;"
    "border-radius:0 0 8px 8px;white-space:nowrap;z-index:100;"
    "pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.12)';"
    "e.textContent=T;}"
    "if(b.firstChild!==e)b.insertBefore(e,b.firstChild);}"
    "if(document.body)up();else document.addEventListener('DOMContentLoaded',up);"
    "new MutationObserver(up).observe(document.documentElement,{childList:true,subtree:false});"
    "})();"
)

# 浏览器通用 UA（与登录窗抓会话时的 UA 保持一致，降低风控）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _cookie_header_to_playwright(cookie_header: str, domain: str) -> list[dict]:
    """把 "name=value; name2=value2" 串转 Playwright add_cookies 格式。

    参数:
        cookie_header: Cookie 请求头格式串
        domain: 归属域（如 ".douyin.com"）
    返回:
        Playwright Cookie dict 列表
    """
    cookies = []
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if not name:
            continue
        cookies.append({
            "name": name, "value": value,
            "domain": domain, "path": "/",
        })
    return cookies


class BrowserActor:
    """Chromium 执行器单例：每个线程独立维护 (playwright, browser) 实例对。

    Playwright sync API 用 greenlet 实现协程切换——所有对象（runtime / browser /
    context / page）绑首次创建线程。跨线程访问必抛 "Cannot switch to a
    different thread"。

    threading.local 隔离每个线程的 storage：B 线程读 _tls.browser 拿到 None
    （自己线程 storage 未设），触发新建本线程独立的实例。
    """

    def __init__(self):
        self._lock = threading.Lock()         # 保护 _all_browsers 读写 + 跨线程重建竞态
        self._run_lock = threading.Lock()    # #131：保护 run() 全流程串行
        self._tls = threading.local()         # 每线程 (playwright, browser) 独立 storage
        self._all_browsers: list = []         # 跟踪所有线程的 browser，shutdown 时统一尝试关闭

    # ---------- 浏览器生命周期 ----------

    def _resolve_headless(self, headless: Optional[bool]) -> bool:
        """解析 headless 参数：显式传值用之，否则读配置 browser_show_window。

        #410：默认 False（无头）；调试时设 True 显示窗口。
        登录窗（open_login_window）固定 False，不调本方法。
        """
        if headless is not None:
            return headless
        try:
            from app.services.setting_service import load_settings
            return not bool(load_settings().get("browser_show_window", False))
        except Exception:  # noqa: BLE001 配置未初始化（启动早期）默认无头
            return True

    def _ensure_browser(self, headless: bool = True):
        """确保当前线程的 Chromium 已启动（懒启动 + 崩溃重建）。

        每个线程独立持有一份 (playwright, browser)。is_connected 跨线程访问
        （worker 线程读 main 线程创建的 browser）必抛空异常 → try/except 兜底。

        #139：headless 参数与当前 browser 不一致时关闭重建——
        例如先 run(headless=True) 拉素材，再 open_login_window(headless=False)
        添加账号，若不重建会复用 headless browser，登录窗不弹窗。

        #P0-5：headless 变更时同时关闭旧 playwright runtime——playwright 对象绑
        创建线程，跨线程复用于 chromium.launch() 会抛 "Cannot switch to a
        different thread"。必须在当前线程停掉旧 runtime，再 start() 新的。
        """
        # 当前线程已启动且连接正常 + headless 模式匹配 → 复用
        try:
            browser = getattr(self._tls, "browser", None)
            if browser is not None and browser.is_connected():
                if getattr(self._tls, "headless", None) == headless:
                    return
                # headless 模式不匹配 → 关闭旧 browser + 旧 playwright runtime 重建
                logger.info("[浏览器] 显示模式变更（{}→{}），关闭旧 runtime 重建",
                            "显示" if not getattr(self._tls, "headless", None) else "无头",
                            "显示" if not headless else "无头")
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._tls.browser = None
                # 旧 playwright runtime 必须在创建它的线程停掉，否则会抛跨线程错
                # 同一线程内关闭安全；如果当前线程不是创建线程则跳过 stop
                # （shutdown 时由 daemon 线程兜底）
                old_pw = getattr(self._tls, "playwright", None)
                if old_pw is not None:
                    try:
                        old_pw.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._tls.playwright = None
        except Exception:  # noqa: BLE001 跨线程读 main 线程 browser 抛空异常
            logger.warning("[浏览器] 连接状态检查异常（线程隔离生效，将重建）")
            # 跨线程读失败时清空整个 tls 状态，让下面走全新启动路径
            self._tls.browser = None
            self._tls.playwright = None
        from playwright.sync_api import sync_playwright
        if getattr(self._tls, "playwright", None) is None:
            self._tls.playwright = sync_playwright().start()
        logger.info("[浏览器] 启动 Chromium（线程={}，{}）",
                    threading.current_thread().name,
                    "显示窗口" if not headless else "无头模式")
        self._tls.browser = self._tls.playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",  # 降低自动化特征
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._tls.headless = headless
        with self._lock:
            self._all_browsers.append(self._tls.browser)

    def shutdown(self):
        """退出清理：尝试关闭所有线程的浏览器。runtime 跨线程 stop 不可靠，跳过即可。

        Chromium 子进程由 OS 兜底回收，runtime 主动 stop 跨线程不安全。
        """
        with self._lock:
            browsers = list(self._all_browsers)
            self._all_browsers.clear()
        for i, browser in enumerate(browsers):
            try:
                # 用 daemon 线程 + Event 超时，避免主线程卡在 close
                done = threading.Event()

                def _close():
                    try:
                        browser.close()
                    except BaseException:  # noqa: BLE001 跨线程 close 经常抛空异常，OS 兜底 Chromium 子进程
                        pass
                    finally:
                        done.set()

                t = threading.Thread(target=_close, daemon=True,
                                     name=f"browser-close-{i}")
                t.start()
                if not done.wait(timeout=2.0):
                    logger.warning(f"[浏览器] close-{i} 超时（2s），跳过等待")
            except Exception:  # noqa: BLE001 退出清理全兜底
                pass

    # ---------- 任务执行 ----------

    def new_session(
        self,
        url: str,
        storage_state: dict | None = None,
        cookies: list[dict] | None = None,
        headless: Optional[bool] = None,
        timeout_ms: int = 60000,
        referer: str = "https://www.douyin.com/",
        label: str = "session",
    ) -> "BrowserSession":
        """开一个浏览器会话：context + page 复用，多次 call() 走同会话。

        用途：批量请求场景（POI 翻页 + N 个详情）。一次会话只创建 1 个
        context、只 goto 一次目标页，后续 evaluate fetch 由 axios 拦截器
        现场算 a_bogus 签名，不再重加载页面。

        失败处理：会话内 call() 抛异常不回滚 context，由调用方决定
        （用户要求：失败就记录，不做兜底恢复）。

        参数:
            url: 会话入口 URL（一般 creator-micro/content/upload）
            storage_state: 登录态（含 cookies + origins/localStorage）
            cookies: 兜底 cookie（仅 storage_state 为空时使用）
            headless: 无头模式（None=读配置 browser_show_window，#410）
            timeout_ms: 首次 goto 超时
            referer: 首次 goto referer
            label: 会话标签（close 时日志输出，方便调用方关联 task / context）
        返回:
            BrowserSession（上下文管理器，with 退出自动关 context）
        """
        self._ensure_browser(self._resolve_headless(headless))
        context = self._tls.browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            storage_state=storage_state,
        ) if storage_state else self._tls.browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        try:
            if cookies and not storage_state:
                context.add_cookies(cookies)
            page = context.new_page()
            page.add_init_script(_ANTI_BOT_INIT_SCRIPT)
            session = BrowserSession(
                context=context, page=page,
                goto_url=url, timeout_ms=timeout_ms, referer=referer,
                parent=self, label=label,
            )
            # 首次 goto（持 _run_lock 与 run() 串行，互不干扰）
            with self._run_lock:
                session._initial_goto()
            return session
        except Exception:
            # 启动失败兜底关 context（避免 Chromium 残留）
            try:
                context.close()
            except Exception:
                pass
            raise

    def run(self, url: str, capture: Callable[[str], bool], cookies: list[dict] | None = None,
            storage_state: dict | None = None,
            actions: Optional[Callable] = None, headless: Optional[bool] = None,
            timeout_ms: int = 30000, referer: str = "https://www.douyin.com/",
            manual_wait_ms: int = 0) -> list[dict]:
        """执行一次浏览器任务：开 context → 注 Cookie → 导航 → 拦截响应 → 收集。

        参数:
            url: 目标页地址
            capture: 响应拦截判定函数（收到 XHR 时调用，返回 True 则抓取响应体）
            cookies: Playwright 格式 Cookie 列表（登录态，可多域）。仅无 storage_state 时用
            storage_state: 完整 storage_state dict（含 cookies + origins/localStorage）。
                抖音接口需要 msToken / a_bogus 等 localStorage token 算签名，
                只注 cookies 拿不到 localStorage → 签名失败 0 响应。
                优先于 cookies，二选一。
            actions: 页面加载后的附加操作（如滚动翻页），参数为 page
            headless: 无头模式（调试时 False 可肉眼观察）
            timeout_ms: 单次导航超时
            manual_wait_ms: 人工等待窗口毫秒数。actions 执行完 0 响应时进入等待
                （验证码弹窗需人工拖动，完成后页面自动重发请求），期间每秒轮询
                captured，有数据即提前返回；0 = 不等待直接返回
        返回:
            拦截到的响应 JSON 列表（解析失败的跳过）
        """
        # #131：浏览器单例不支持高并发，多 context 同时 nav/拦 XHR 会互相
        # 干扰（实测偶发 0 响应）。run() 全程加锁串行，简单稳定。
        with self._run_lock:
            return self._run_impl(url, capture, cookies, storage_state, actions, headless,
                                  timeout_ms, referer, manual_wait_ms)

    def _run_impl(self, url: str, capture: Callable[[str], bool],
                  cookies: list[dict] | None = None,
                  storage_state: dict | None = None,
                  actions: Optional[Callable] = None, headless: Optional[bool] = None,
                  timeout_ms: int = 30000, referer: str = "https://www.douyin.com/",
                  manual_wait_ms: int = 0) -> list[dict]:
        """run() 的实际实现（持锁串行）。"""
        self._ensure_browser(self._resolve_headless(headless))
        # 有 storage_state（含 origins/localStorage）时一次性恢复，否则仅注 cookies
        if storage_state:
            context = self._tls.browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                storage_state=storage_state,
            )
        else:
            context = self._tls.browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
        captured: list[dict] = []
        try:
            if cookies and not storage_state:
                context.add_cookies(cookies)
            page = context.new_page()
            # 反自动化检测：清 navigator.webdriver 等无头特征（页面加载前注入）
            page.add_init_script(_ANTI_BOT_INIT_SCRIPT)
            # 响应拦截：capture 返回 True 的 XHR 抓 body
            def _on_response(resp):
                try:
                    if capture(resp.url):
                        body = resp.json()
                        captured.append(body)
                        logger.debug("[浏览器] 拦截到响应: {}（已抓 {} 条）", resp.url[:100], len(captured))
                except Exception:  # noqa: BLE001 非 JSON/未就绪响应跳过
                    pass
            page.on("response", _on_response)

            page.goto(url, timeout=timeout_ms, referer=referer, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # 等 XHR 首屏发出
            if actions:
                actions(page)
            # 缓冲等待：XHR 可能晚于 actions 结束到达（CSR 首屏 8~12 秒），
            # 轮询直到有响应或到超时上限（取 timeout_ms 与 15 秒的较小值）
            settle_deadline = min(timeout_ms, 15000) // 1000
            for _ in range(settle_deadline):
                if captured:
                    break
                page.wait_for_timeout(1000)
            # 人工等待窗口：0 响应时留窗给人过验证码（过完页面自动重发，拦截继续工作）
            if not captured and manual_wait_ms > 0:
                logger.info("[浏览器] 0 响应，进入人工等待（{} 秒内完成验证码可继续）",
                            manual_wait_ms // 1000)
                deadline = manual_wait_ms // 1000
                for _ in range(deadline):
                    page.wait_for_timeout(1000)
                    if captured:
                        logger.info("[浏览器] 人工等待期收到响应，继续任务")
                        break
                else:
                    pass
                if not captured:
                    logger.warning("[浏览器] 人工等待超时，仍 0 响应")
            return captured
        finally:
            context.close()

    # ---------- 登录窗模式（账号添加/重新登录：Playwright headed 弹窗）----------

    def open_login_window(
        self,
        url: str,
        save_path=None,
        timeout_s: int = 300,
        headless: bool = False,
        user_data_dir: Optional[Path] = None,
    ) -> dict | None:
        """开 headed 浏览器等待用户登录，成功后保存登录态并返回 Cookie 串。

        用于账号添加 / 重新登录流程：
        1. 起 Chromium（headed，用户能看到）
        2. 打开登录页（用户扫码 / 手机验证 / 一键登录）
        3. 持续监测：URL 离开 /login* + sessionid cookie 出现 + DOM 资料卡渲染 三重判据
        4. #452：登录态持久化到 user_data_dir（持久化 cookies + localStorage + IndexedDB + SW + Cache）
        5. 抓 context.cookies() → 拼 Cookie 请求头串返回给调用方

        参数:
            url: 登录入口 URL（一般是 https://creator.douyin.com/）
            save_path: 已废弃（保留兼容），原 storage.json 路径
            timeout_s: 登录等待秒数（默认 5 分钟）
            headless: False 强制 headed（让用户操作）
            user_data_dir: #452 持久化 profile 目录（推荐）；None 时回退 save_path.parent/profile
        返回:
            Cookie 串（"name=value; name2=value2"）/ None（超时或异常）

        #P0-6：登录窗用**独立 sync_playwright 实例**（不共享 _tls/_run_lock），
        避免 worker 线程持 _run_lock 跑业务时 auto_relogin 弹窗死锁。
        登录窗短暂独占 headed 显示窗口，独立 runtime 完全合理。
        """
        from pathlib import Path as _Path
        from playwright.sync_api import sync_playwright

        # #452：解析 user_data_dir（profile 持久化目录）；save_path 兼容旧调用
        if user_data_dir is None:
            if save_path is not None:
                user_data_dir = _Path(save_path).parent / "profile"
            else:
                raise ValueError("user_data_dir 或 save_path 必须传一个")
        user_data_dir = _Path(user_data_dir)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # 用独立 playwright runtime（独立线程内创建、close、stop），
        # 不动 self._tls / 不抢 self._run_lock
        pw = sync_playwright().start()
        context = None
        try:
            # #452：用 launch_persistent_context，登录态写到 user_data_dir（cookies/localStorage/IndexedDB/SW 全部持久）
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--start-maximized",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                ],
            )
            # #452：persistent_context 直接是 context，没有 browser
            page = context.new_page()
            # 反自动化脚本（避免被抖音识别为 webdriver）
            page.add_init_script(_ANTI_BOT_INIT_SCRIPT)

            logger.info("[浏览器] 打开登录窗（headless={}, timeout={}s, profile={}）：{}",
                        headless, timeout_s, user_data_dir.name, url)
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning("[浏览器] 登录页初始导航异常（可能需用户手动操作）: {}", exc)

            # 监测登录成功：与 check_account 统一为「域 + sessionid + DOM 资料卡」三重判据
            # 注：用户扫码后可能停在 creator-micro/content/upload 或 home 子路径，
            # 不能精确匹配 creator-micro/home（会卡死 300s）——只用 creator.douyin.com 域判定。
            deadline = timeout_s
            for i in range(deadline):
                page.wait_for_timeout(1000)
                current_url = page.url
                # 1. URL 在 creator.douyin.com 域 + 不在 /login（已登出 /login）
                if "/login" in current_url.lower() or "creator.douyin.com" not in current_url:
                    continue
                # 2. sessionid cookie 必须出现（创作者接口鉴权唯一凭据）
                cookies = context.cookies()
                cookie_names = {c['name'] for c in cookies}
                if 'sessionid' not in cookie_names:
                    continue
                # 3. 等 DOM 资料卡 selector（与 check_account 同一 selector，5s 超时不抛）
                dom_ok = False
                for sel in ('[class*="unique_id-"]', '[class*="nick_name"]'):
                    try:
                        page.wait_for_selector(sel, timeout=5000)
                        dom_ok = True
                        break
                    except Exception:
                        continue
                if not dom_ok:
                    # 域对 + sessionid 有但 DOM 没渲染（SPA 跳转中 / 风控空壳）
                    # → 不算登录成功，继续等下一轮
                    continue
                # 三重确认通过：等 SDK 异步写入 + 触发创作者中心 → 持久化由 user_data_dir 自动完成
                # #451：登录成功后等 SDK 异步写入 localStorage（__tea_cache_tokens_*/security-sdk 等
                # 跨域 origin 异步初始化），立即关闭会丢 origins，导致发布时找不到 input
                page.wait_for_timeout(5000)
                try:
                    page.goto("https://creator.douyin.com/creator-micro/home",
                              timeout=15000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                # #452：context.close() 会 flush user_data_dir（cookies + IndexedDB + localStorage 全部持久化）
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                logger.info("[浏览器] 登录成功（{}s），profile 持久化到 {}（{} 个 cookie）",
                            i + 1, user_data_dir, len(cookies))
                # #452：同步写 storage.json 备份（API 校验用 + 兼容旧调用方）
                if save_path is not None:
                    try:
                        # 抓 origins 列表（cookies 已有，origins 需要 page.evaluate 序列化）
                        origins_data = []
                        for origin_url in {c.get("domain", "") for c in cookies if c.get("domain")}:
                            if not origin_url:
                                continue
                            try:
                                page.goto(f"https://{origin_url.lstrip('.')}",
                                          timeout=10000, wait_until="domcontentloaded")
                                ls = page.evaluate(
                                    "() => Object.entries(localStorage).map(([k,v]) => ({name:k, value:v}))"
                                )
                                if ls:
                                    origins_data.append({"origin": page.url.rstrip('/'), "localStorage": ls})
                            except Exception:
                                pass
                        storage_dict = {"cookies": cookies, "origins": origins_data}
                        import json as _json
                        save_path.parent.mkdir(parents=True, exist_ok=True)
                        save_path.write_text(_json.dumps(storage_dict, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                        logger.info("[浏览器] storage.json 备份: {}", save_path)
                    except Exception as exc:
                        logger.warning("[浏览器] storage.json 备份失败: {}", exc)
                profile = _extract_creator_profile(page)
                # evaluate 拿不到 douyin_id 时，从 cookies 兜底（uid/passport_uid）
                if not profile.get("douyin_id"):
                    cookie_map = {c['name']: c.get('value', '') for c in cookies}
                    profile["douyin_id"] = (
                        cookie_map.get("uid")
                        or cookie_map.get("passport_uid")
                        or cookie_map.get("user_unique_id")
                        or ""
                    )
                result = {
                    "cookie": cookie_str,
                    "nickname": profile.get("nickname", ""),
                    "douyin_id": profile.get("douyin_id", ""),
                    "avatar": profile.get("avatar", ""),
                }
                context.close()  # flush profile 落盘
                context = None
                return result
            logger.warning("[浏览器] 登录等待超时（{}s），未到 home 或 sessionid/DOM 未就绪", deadline)
            return None
        except Exception as exc:
            logger.error("[浏览器] 登录窗异常: {}", exc)
            return None
        finally:
            # 独立 runtime 完整关闭：context → pw.stop
            # 当前线程就是创建线程，跨线程错不会发生
            try:
                if context is not None:
                    context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def _extract_creator_profile(page) -> dict:
    """从创作者中心已登录页面拿用户身份（昵称/抖音号/头像）。

    #137：creator.douyin.com 是 CSS module（class 名 `nick_name-ss1ZBv` 形态，
    hash 后缀随构建变，但前缀 `name-` / `unique_id-` / `img-` 稳定）。
    旧 selector 用连字符 `user-info-name` 全失效——改用下划线前缀匹配。

    DOM 结构（2026-09 实测抓取）：
      <div class="user_info-p0xmO9">            # 用户资料卡容器
        <div class="avatar-HAvcz0"><img class="img-PeynF_" src="https://p3.douyinpic.com/aweme/.../avatar/...jpeg"></div>
        <div class="content-fFY6HC">
          <div class="header-_F2uzl">
            <div class="left-zEzdJX">
              <div class="name-_lSSDc">昵称文本</div>      ← 昵称
              <div class="accountIdentityTags-uCP0Kx"></div>
            </div>
          </div>
          <div class="unique_id-EuH8eA">抖音号：20765888255</div>  ← 抖音号
          <div class="signature-HLGxt7">签名…</div>
        </div>
      </div>

    定位策略：以 unique_id 元素为锚，向上找资料卡容器，容器内拿 name- + img。
    失败/字段缺失返回空字符串（不抛异常，由调用方判定）。
    """
    try:
        return page.evaluate("""
            () => {
                const trim = (s) => (s || '').trim();
                // 1. 抖音号锚点：[class*="unique_id-"] 文本"抖音号：123"
                const uidEl = document.querySelector('[class*="unique_id-"]');
                let douyin_id = '';
                let card = null;
                if (uidEl) {
                    card = uidEl.closest('[class*="user_info"], [class*="content"], [class*="header"]');
                    const m = (uidEl.textContent || '').match(/(\\d+)/);
                    if (m) douyin_id = m[1];
                }
                // 2. 昵称：卡片内 [class*="name-"]（前缀稳定，hash 变）
                let nickname = '';
                const scope = card || document;
                const nameEl = scope.querySelector('[class*="name-"]');
                if (nameEl) nickname = trim(nameEl.textContent);
                // 3. 头像：卡片内 img[src*="douyinpic"]（真实头像 CDN，排除 logo svg）
                let avatar = '';
                const img = scope.querySelector('img[src*="douyinpic"][src*="avatar"]')
                    || scope.querySelector('img[src*="douyinpic"]')
                    || document.querySelector('img[src*="douyinpic"][src*="avatar"]');
                if (img) avatar = img.src || '';
                // 4. 兜底：昵称仍空时试更多 selector（顶栏用户卡片）
                if (!nickname) {
                    const fallback =
                        trim(document.querySelector('[class*="nick_name"]')?.textContent) ||
                        trim(document.querySelector('[class*="userName"]')?.textContent) ||
                        trim(document.querySelector('[class*="account-name"]')?.textContent) ||
                        '';
                    nickname = fallback;
                }
                return { nickname, douyin_id, avatar };
            }
        """)
    except Exception as exc:
        logger.warning("[浏览器] 提取创作者身份失败: {}", exc)
        return {"nickname": "", "douyin_id": "", "avatar": ""}


# 模块级单例
browser_actor = BrowserActor()


class BrowserSession:
    """浏览器会话：复用 context + page，多次 call() 触发 evaluate fetch。

    与 BrowserActor.run() 区别：
    - run() 每次开新 context + 重新 goto（适合一次抓一个 URL）；
    - session 一次开 + 一次 goto，按需 evaluate fetch（适合翻页 + 批量详情）。

    线程模型：持有创建线程的 context/page（Playwright 跨线程访问必抛），
    外部用 threading.local 隔离或仅在 worker 线程使用。

    拦截器：context 级别注册 on_response 回调；call() 内部维护 captured
    列表，按 capture() 函数过滤；不同 call() 互不干扰（各持独立 captured）。

    统计：每次 call() 按 kind（search / detail / other）累计次数 + 耗时；
    close() 输出汇总日志（复用次数、节省 context/goto 估算、空响应计数）。
    """

    def __init__(self, context, page, goto_url: str, timeout_ms: int,
                 referer: str, parent: BrowserActor, label: str = "session"):
        self._context = context
        self._page = page
        self._goto_url = goto_url
        self._timeout_ms = timeout_ms
        self._referer = referer
        self._parent = parent  # 持父级 _run_lock
        self._label = label
        self._closed = False
        self._resp_handler = None
        # 统计：call 次数 / 分类耗时 / 空响应数
        self._stats = {
            "calls": 0,            # 总 call 次数
            "search_calls": 0,
            "detail_calls": 0,
            "other_calls": 0,
            "empty_responses": 0,  # 未拦截到响应的 call 数
            "ms_total": 0.0,       # 所有 call 耗时累计（持锁段）
            "ms_search": 0.0,
            "ms_detail": 0.0,
        }
        self._opened_at = time.monotonic()  # 会话起点（算存活时长）

    # ---------- 上下文管理器 ----------

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """关 context（with 自动调）。重复调用幂等。close 时输出统计日志。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._resp_handler is not None:
                try:
                    self._page.remove_listener("response", self._resp_handler)
                except Exception:
                    pass
                self._resp_handler = None
            self._context.close()
        except Exception:
            pass
        # 输出统计汇总行（一次会话一打，方便量化复用收益）
        try:
            self._log_summary()
        except Exception:  # noqa: BLE001 统计失败不影响 close
            pass

    def _log_summary(self) -> None:
        """close 时打印一行会话统计。"""
        s = self._stats
        alive_s = time.monotonic() - self._opened_at
        # 节省估算：旧实现每次 call 都开 context + goto（≈3.5s/次），复用版只 ≈0.5s/次
        saved_contexts = max(0, s["calls"] - 1)  # 第 1 次仍要开/关
        # 平均每 call（仅在有数据时算）
        if s["calls"]:
            avg_ms = s["ms_total"] / s["calls"] * 1000
            avg_str = f"平均每 call={avg_ms:.0f}ms"
        else:
            avg_str = "无 call"
        logger.info(
            "[Browser-session] {} 复用={} 次（search={} + detail={} + other={} + 空响应={}），"
            "会话时长={:.1f}s，{}，节省 context 开/关≈{} 次",
            self._label,
            s["calls"],
            s["search_calls"], s["detail_calls"], s["other_calls"],
            s["empty_responses"],
            alive_s, avg_str, saved_contexts,
        )

    # ---------- 内部 ----------

    def _initial_goto(self) -> None:
        """首次 goto（持父级 _run_lock 调用一次，避免与 run() 并发）。"""
        self._page.goto(self._goto_url, timeout=self._timeout_ms,
                        referer=self._referer, wait_until="domcontentloaded")
        # 等首屏 XHR 发完（拦截器才能注册）
        self._page.wait_for_timeout(2000)

    # ---------- 任务执行 ----------

    def call(self, capture: Callable[[str], bool],
             actions: Callable | None = None,
             wait_ms: int = 3000,
             kind: str = "other") -> list[dict]:
        """在会话内发一次 fetch 并拦截响应。

        参数:
            capture: 响应拦截判定（返回 True 的 XHR 抓 body）。
            actions: page 上的附加操作（通常是 lambda p: p.evaluate(fetch_url_template)）。
            wait_ms: actions 后等待 XHR 到达的时间（毫秒）。默认 3000。
            kind: 调用分类标签，仅用于统计（"search" / "detail" / "other"）。
        返回:
            拦截到的响应 JSON 列表。
        抛出:
            RuntimeError：会话已关闭。
        """
        if self._closed:
            raise RuntimeError("BrowserSession 已关闭")

        captured: list[dict] = []
        # 上下文管理：注册 → actions → 卸载，避免跨 call 干扰
        def _on_response(resp):
            try:
                if capture(resp.url):
                    captured.append(resp.json())
            except Exception:
                pass

        # 持父级 _run_lock：与 run() 串行（多个 session 之间也串行——Chromium 单例
        # 同时只跑一个导航/拦截器，多 session 并发会互相冲掉 captured）
        t0 = time.monotonic()
        with self._parent._run_lock:
            try:
                self._resp_handler = _on_response
                self._page.on("response", _on_response)
                if actions:
                    actions(self._page)
                # 等 XHR 落定（轮询提前返回）
                settle_s = max(1, wait_ms // 1000)
                for _ in range(settle_s):
                    if captured:
                        break
                    self._page.wait_for_timeout(1000)
            finally:
                try:
                    self._page.remove_listener("response", _on_response)
                except Exception:
                    pass
                self._resp_handler = None
        elapsed = time.monotonic() - t0

        # 累加统计
        s = self._stats
        s["calls"] += 1
        s["ms_total"] += elapsed
        if kind == "search":
            s["search_calls"] += 1
            s["ms_search"] += elapsed
        elif kind == "detail":
            s["detail_calls"] += 1
            s["ms_detail"] += elapsed
        else:
            s["other_calls"] += 1
        if not captured:
            s["empty_responses"] += 1

        return captured