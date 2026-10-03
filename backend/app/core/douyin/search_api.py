# -*- coding: utf-8 -*-
"""抖音网页版搜索接口浏览器持久化会话版（#506 重写，2026-09-30 v5 wheel 升级）。

# 506 改造：旧版用 curl_cffi 裸 HTTP 调 /aweme/v1/web/general/search/single/，
# 被搜索接口 2483 风控拦截。根因是搜索接口需要完整登录会话（持久化 chromium
# profile 的 storage_state + IndexedDB + ttwid 设备指纹），裸 cookie 不足。

# 2026-09-30 二次风控：浏览器直跳 https://www.douyin.com/search/<keyword>?...
# 仍被风控——search/item XHR 完全不返回（_captured=[]），但搜索页 DOM 渲染成功。
# 推断：直跳 URL 会被识别为爬虫（缺搜索框事件指纹 + 无 keyboard event 时序）。

# v2（locator.fill + locator.press("Enter")）部分场景 React 受控组件 input
# 事件未触发，跳到 /jingxuan。v3 cursor fetch（page.evaluate fetch + 手算
# offset 公式）高频翻页触发 msToken/a_bogus 风控（实测 5 页以上易被拦）。

# v5 wheel 真实用户路径（当前实现）：
# 1. _ensure_open 启动后 goto www.douyin.com/ 首页（建立登录态 + 搜索框 DOM）
# 2. page.mouse.click 搜索框 → page.keyboard.type(keyword, delay=80) →
#    page.keyboard.press("Enter") → 触发首个 general/search/single XHR
# 3. page>=2：page.mouse.wheel(0, 300) 触发 React 内部 fetch（自带签名），
#    _on_response 累积捕获每页 XHR，返回 target_page 对应那页的 body
# v5 实测：15 页连续 0 限流 0 重复，每页 ~4s。

# 兼容性：search_single / search_page 签名不变（page 参数 → offset 转换）；
# material_service 现有「每页 close+rebuild session」调用模式仍可用。
# 优化方向：让 material_service 跨页复用 session（避免每页重启 playwright）。

# 监听器 / 诊断：_on_response 注册一次（避免重复 on 错位），search_page 用
# _poll_captured_until 轮询累积响应（避免 expect_response race），60s 超时
# 统一抛 SearchBlockedError + 诊断上下文。

# asyncio loop 生命周期：严格用 `with sync_playwright()` 管理 PlaywrightContextManager，
# __exit__ 完整关闭 driver + asyncio loop。
"""

from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote as _url_quote

from loguru import logger

# browser_actor 模块级 import 安全：browser.py 不引用 search_api，
# 无循环 import 风险。__init__ 用其 _resolve_headless 读 browser_show_window 配置。
from app.core.douyin.browser import browser_actor


class SearchBlockedError(Exception):
    """搜索接口被风控拦截（浏览器拦截或 status_code!=0）。"""


def _is_browser_closed_err(e: Exception) -> bool:
    """检测异常是否源于浏览器被关闭（TargetClosedError / "closed" 消息）。

    #fix-browser-closed-stop：调用方据此判断是否中断后续流程（抛 SearchBlockedError）
    还是按普通 UI 失败降级（warning + 继续）。浏览器关闭时降级继续会触发连串抛错，
    应尽早中断让上层捕获并清理 ctx/pw_cm。
    """
    msg = str(e) or ""
    if "closed" in msg.lower():
        return True
    name = type(e).__name__
    return "TargetClosedError" in name or "BrowserClosed" in name


def _raise_if_browser_closed(e: Exception, where: str) -> None:
    """检测到浏览器关闭：抛 SearchBlockedError 中断后续（不让兜底继续）。

    #fix-browser-closed-stop：调用方 try/except 内先调用本 helper，
    #若不是浏览器关闭则 silent return 让原 swallow 逻辑（warning + 继续）执行。
    """
    if _is_browser_closed_err(e):
        raise SearchBlockedError(
            f"浏览器已关闭（{where}）：{e}"
        ) from e


def _safe_wait_or_raise(page, ms: int, where: str) -> None:
    """wait_for_timeout 安全封装：浏览器关闭 → 抛错中断，其他异常 → swallow 静默。

    #fix-browser-closed-stop：统一 5 处 wait_for_timeout swallow 块的样板代码。
    调用方拿到 None 即「已完成或已被 swallow」，无需再处理。
    """
    try:
        page.wait_for_timeout(ms)
    except Exception as e:
        _raise_if_browser_closed(e, where)
        # 非 closed 异常 swallow：旧路径 swallow + 继续（不再刷 warning）


_SEARCH_URL = "https://www.douyin.com/aweme/v1/web/search/item/"


class BrowserSearchSession:
    """单页搜索会话：每次构造自带同步 playwright context + persistent context。

    设计：
    - 构造时 lazy launch（耗时 ~3-5s），只跑一页 search_page 后 close
    - 严格用 `with sync_playwright() as pw:` 走完整 __exit__ 关 driver + asyncio loop
    - material_service 每页 search 前 new + close 一次，避免与 detail 路径冲突
    """

    def __init__(self, profile_dir: Path, headless: Optional[bool] = None):
        # #589 任务配套：素材拉取阶段 A 默认读系统设置 browser_show_window
        # 与 detail 路径（BrowserActor._resolve_headless）保持一致，
        # 让用户设置「显示浏览器窗口」时素材拉取也能弹出窗口便于观察。
        # 显式传 True/False 则覆盖配置（便于测试 / 强制无头）。
        if headless is None:
            headless = browser_actor._resolve_headless(None)
        self._profile_dir = str(profile_dir)
        self._headless = headless
        self._pw = None              # SyncPlaywright 实例
        self._pw_cm = None           # PlaywrightContextManager（持有 loop）
        self._ctx = None             # BrowserContext（launch_persistent_context）
        self._page = None
        # 当前页捕获的响应列表（每次 search_page 入口清空 + 复用同一监听器写入）
        self._captured: list[dict] = []
        # 跨 search（含 material_service retry 路径）总累计页数：
        # search_all_for_ids 入口 _captured.clear() 会丢失上一轮页数；
        # material_service 拉取循环 retry 时复用同一 session，
        # 这里在 search 出口 += len(_captured) 累加跨轮计数（retry 真实总页数）。
        # 终态 progress 用此字段（不是 len(_captured) 单轮值）反映 retry 后真实页数。
        self._total_captured_pages: int = 0
        # 2026-09-30 翻页改造：记录当前已搜索的 keyword，跨页复用避免每页重新 fill+Enter
        # （如果 material_service 跨页复用 session 即可生效；现在每页 close+rebuild
        # 模式下此字段总是被 reset，每次 search_page 都会重新 fill+Enter 触发首屏）
        self._searched_keyword: str = ""
        # 上次响应的 cursor（cursor fetch 翻页用，必须用上次响应 cursor 不固定公式）
        self._last_cursor: int | None = None

    def _ensure_open(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        from app.core.douyin.browser import UA, browser_actor, _make_anti_bot_init_script
        # #P0-11：BrowserActor detail path 在同线程持有 sync_playwright runtime，
        # dispatcher loop 持续 running（_tls.playwright 不关）→ 当前线程
        # asyncio.get_running_loop() 返回 is_running=True 的 loop。
        # PlaywrightContextManager.__enter__ 检测到 running loop 抛
        # "inside the asyncio loop"。
        # 解法：search 前先强制关闭 BrowserActor 同线程的 playwright + browser runtime，
        # 再起新的 sync_playwright()——两个 runtime 在同一线程不共存，避免 loop 残留冲突。
        # BrowserActor 后续 detail 时会自动重建（_ensure_browser 检查 is_connected）。
        browser_actor._reset_tls_for_sync_api()

        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.__enter__()
        # #审查建议：playwright 启动是耗时操作（3-5s），之前完全静默，运维无法判断
        # 是浏览器启动慢还是网络慢。打印 profile_dir + headless 便于排查。
        logger.info(
            "[搜索] 启动浏览器，账号目录={}",
            self._profile_dir,
        )
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self._profile_dir,
            headless=self._headless,
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled,AutomationControlledForRenderProcessHost,AutomationControlledForSwap",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        self._page = self._ctx.new_page()
        # 搜索浏览器警示条位置默认 center（中央浮层，不挡搜索框/筛选面板）
        self._page.add_init_script(_make_anti_bot_init_script("center"))
        # 一次性注册监听器（避免多次 on() 累积导致重复 append 错位）
        self._page.on("response", self._on_response)
        # 2026-09-30 v2：导航到抖音首页（建立登录态 + 搜索框 DOM），
        # 后续 search_page 通过 fill + Enter 在站内触发搜索。
        # 直跳 search/<keyword> 会被风控识别为爬虫（缺 keyboard event 指纹）。
        try:
            self._page.goto(
                "https://www.douyin.com/", wait_until="load", timeout=40_000,
            )
            logger.debug(
                "[搜索] 已加载抖音首页 url={}", self._page.url,
            )
        except Exception as e:  # noqa: BLE001
            # 首页加载失败不影响后续搜索（可能 profile 已带登录态），仅留痕
            logger.warning("[搜索] 抖音首页加载异常：{}（继续走搜索）", e)

    def _on_response(self, r) -> None:
        """统一响应拦截：匹配抖音搜索结果 XHR 写入 _captured。
        匹配规则复用 _match_search_xhr（统一接口新旧名字匹配）。
        v3 模式：_captured 持续累积，由 search_page 入口主动 clear，
        用户滚动模式（trigger_search + wait_for_next_xhr）则不 clear。
        """
        if not self._match_search_xhr(r):
            return
        try:
            self._captured.append(r.json())
        except Exception:
            pass

    # ---------- 公共 API ----------

    def reset_captured(self) -> None:
        """清空累积响应（新 search 任务开始前调用，避免旧响应污染）。"""
        self._captured.clear()

    def search_page(
        self,
        keyword: str,
        offset: int = 0,
        sort_type: int = 0,
        publish_time: int = 0,
        filter_duration: str = "",
        timeout: int = 60,
    ) -> dict:
        """模拟真人操作：首页 → 搜索框 click + keyboard.type + Enter → wheel 滚动翻页。

        替代旧版「page.goto search/<keyword>?」实现——后者被识别为爬虫，
        XHR 完全不返回（_captured=[]）。v5 用真实用户路径绕开风控。

        流程：
        - 首次/换 keyword：定位搜索框 → mouse.click → keyboard.type(80ms/char)
          → keyboard.press("Enter") → 等首个 general/search/single XHR
        - target_page>=2：page.mouse.wheel(0, 300) 触发真实 React 加载链，
          _on_response 累积捕获每页 XHR，返回 target_page 对应那页的 body

        sort_type/publish_time/filter_duration 参数当前 v5 未下推（搜索框 input
        不带筛选条件，由搜索结果页 UI 后续操作），与旧 URL 下推行为不同——
        已知偏差，TODO：后续通过页面 UI 交互（点排序按钮等）实现。

        参数:
            keyword: 搜索关键词
            offset: 翻页 offset（0=首屏；10=第2页；20=第3页...）
            sort_type/publish_time/filter_duration: 保留参数占位（v5 未生效）
            timeout: 单次 XHR 等待超时（秒），默认 60s；wheel 翻页用 idle_timeout 60s
        返回:
            单页搜索接口原始 JSON dict（含 has_more / cursor / status_code 透传）
        异常:
            SearchBlockedError: 找不到搜索框 / XHR 超时 / status_code!=0 / 翻页超时
        """
        target_page = offset // 10 + 1
        self._ensure_open()
        # 清空上一轮捕获（监听器复用，避免新旧响应混杂）
        self._captured.clear()

        # 首次/换 keyword 才重新 click+type+Enter；同 keyword 翻页复用已激活的搜索结果页
        if self._searched_keyword != keyword:
            try:
                self._click_and_type_keyword(self._page, keyword)
            except SearchBlockedError:
                raise
            except Exception as e:
                raise SearchBlockedError(f"搜索框操作失败：{e}") from e
            self._searched_keyword = keyword

        if target_page == 1:
            # page=1：等首屏 XHR（click+type+Enter 已触发，_on_response 已捕获）
            body = self._poll_captured_until(lambda: bool(self._captured), timeout)
        else:
            # page>=2：wheel 真实滚动累积，直到 _captured 数量达到 target_page
            # v5 实测：15 页 0 限流，每页间隔 ~4s
            self._scroll_until(target_page=target_page, idle_timeout=timeout)
            if len(self._captured) < target_page:
                _diag = self._diag_context()
                raise SearchBlockedError(
                    f"翻页超时：目标 page={target_page} 但只收到 {len(self._captured)} 页 XHR"
                    f" | 诊断：{_diag}"
                )
            body = self._captured[target_page - 1]

        if body.get("status_code") not in (0, None):
            raise SearchBlockedError(
                f"搜索接口 status_code={body.get('status_code')}"
            )
        return body

    def _poll_captured_until(self, predicate, timeout: int) -> dict:
        """轮询 _captured 直到 predicate() 为真或超时，返回 _captured 最后一条。

        race fix：page.expect_response 是注册后才监听，click+type+Enter 触发的 XHR
        比 expect_response 注册更早，会漏抓。改用 _on_response 在 _ensure_open
        一次性注册的累积监听器，poll _captured 拿到响应。

        防御：page 异常断开（chromium 崩溃 / asyncio loop 关闭）让 wait_for_timeout
        抛 Error: Page closed，包 try/except 转 SearchBlockedError（与超时同语义）。
        """
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if predicate() and self._captured:
                body = self._captured[-1]
                self._last_cursor = body.get("cursor")
                return body
            try:
                self._page.wait_for_timeout(200)
            except Exception as e:
                _diag = self._diag_context()
                raise SearchBlockedError(
                    f"等待 XHR 异常（page 已断开）：{str(e)[:80]}"
                    f" | 诊断：{_diag}"
                ) from e
        _diag = self._diag_context()
        raise SearchBlockedError(
            f"等待 XHR 超时（{timeout}s 内未捕获 general/search/single）"
            f" | 诊断：{_diag}"
        )

    @staticmethod
    def _sum_aweme_in_body(body: dict) -> int:
        """单页响应中实际视频条数（data 数组长度）。"""
        return len(body.get("data") or [])


    def _scroll_until(
        self,
        target_page: int | None = None,
        idle_timeout: int = 60,
        max_pages: int = 50,
        wheel_delta: int = 300,
        wheels_per_round: int = 5,
        round_settle_ms: int = 1500,
        wheel_interval_ms: int = 150,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """v5 wheel 真实滚动翻页：mouse.wheel 触发 React 内部 fetch（自带签名）。

        v5 实测：15 页连续 0 限流 0 重复，每页 ~4s。
        旧版 _fetch_page_via_cursor（page.evaluate fetch + 手算 cursor 公式）
        高频调用会触发 msToken/a_bogus 风控，已废弃。

        参数:
            target_page: 累计多少页 XHR 后返回（None 表示滚到 has_more=0 或空闲超时）
            idle_timeout: 静默多少秒无新 XHR 后停止（默认 60s）
            max_pages: 最大累计页数上限（防 keyword 过宽泛无限滚，默认 50）
            wheel_delta: 每次 wheel 滚动量（CSS pixel），默认 300
            wheels_per_round: 每轮连续 wheel 次数，默认 5
            round_settle_ms: 每轮之间静默等待 XHR 时间，默认 1500ms
            wheel_interval_ms: 每次 wheel 之间间隔，默认 150ms（模拟真人节奏）
            progress_cb: 进度回调（任务 #589）签名 (累计页数, 累计视频数) -> None；
                每抓到一页新 XHR 后调用一次，让外层 worker 实时刷新 info.progress。
        返回:
            实际累积 XHR 数量（<= max_pages）
        退出条件（任一满足即返回）：
            - target_page 达到（仅在 target_page 不为 None 时生效）
            - max_pages 达到上限
            - 最新响应 has_more=0
            - idle_timeout 秒无新 XHR
        """
        import time as _time
        page = self._page
        # 移到结果列表中央区域（避免在搜索框位置被滚动拦截）
        page.mouse.move(700, 500)
        start_count = len(self._captured)
        # 防御：翻页循环整个包 try/except，page 异常断开（chromium 崩溃 / asyncio loop
        # 关闭）时让循环提前退出而非抛到 search_all_for_ids 让任务直接失败。
        # 返回当前已捕获的页数（部分结果）让上层能基于已有数据继续。
        try:
            return self._scroll_loop(
                page, idle_timeout, max_pages, target_page, wheels_per_round,
                wheel_delta, wheel_interval_ms, round_settle_ms, progress_cb,
                _time, start_count,
            )
        except Exception as e:
            err_msg = str(e)[:120]
            # #fix-browser-closed：浏览器被用户手动关闭时降级为 INFO，不刷 WARNING 噪音
            if "closed" in err_msg.lower():
                logger.info(
                    "[搜索翻页] 浏览器已关闭 pages={}（返回已捕获部分结果）",
                    len(self._captured) - start_count)
            else:
                logger.warning(
                    "[搜索翻页] 翻页异常退出 pages={} err={}（返回已捕获部分结果）",
                    len(self._captured) - start_count, err_msg,
                )
            return len(self._captured) - start_count


    def _scroll_loop(self, page, idle_timeout, max_pages, target_page,
                     wheels_per_round, wheel_delta, wheel_interval_ms,
                     round_settle_ms, progress_cb, _time, start_count) -> int:
        """_scroll_until 实际翻页循环：与外层 try/except 分离便于调试栈清晰。"""
        last_progress_ts = _time.time()
        while True:
            before = len(self._captured)
            # 连续 wheel 多轮（抖音 IntersectionObserver 节流，一次 wheel 不一定触发）
            for _ in range(wheels_per_round):
                page.mouse.wheel(0, wheel_delta)
                page.wait_for_timeout(wheel_interval_ms)
            # 等 XHR 落库
            page.wait_for_timeout(round_settle_ms)
            delta = len(self._captured) - before
            if delta > 0:
                last_body = self._captured[-1]
                cursor = last_body.get("cursor")
                has_more = last_body.get("has_more")
                self._last_cursor = cursor
                last_progress_ts = _time.time()
                # 合并 INFO + DEBUG 为单条 DEBUG：用户视角从 progress_cb 拿累计数据。
                logger.debug(
                    "[搜索翻页] 累计 {} 页 / 本页 {} 条 / cursor={} / has_more={}",
                    len(self._captured), self._sum_aweme_in_body(last_body),
                    cursor, has_more,
                )
                # 任务 #589：实时进度回调，外层 worker 据此刷新 info.progress
                if progress_cb is not None:
                    try:
                        # re-sum 而非增量累加：max_pages≤50 时单次 O(n) 可忽略；
                        # 换来容错性（万一 listener 漏抓可自愈累计值）
                        total_pages = len(self._captured) - start_count
                        total_videos = sum(
                            self._sum_aweme_in_body(b) for b in self._captured[start_count:]
                        )
                        progress_cb(total_pages, total_videos)
                    except Exception:  # noqa: BLE001 回调异常不影响翻页主流程
                        # debug 留 trace，便于排查回调里真正的 bug
                        logger.debug("进度回调异常", exc_info=True)
                # 终止条件（通俗）
                if has_more == 0:
                    logger.info("[搜索翻页] 没有更多视频了，停止翻页")
                    break
                if len(self._captured) - start_count >= max_pages:
                    logger.info("[搜索翻页] 已达最大页数 {}，停止", max_pages)
                    break
                if target_page is not None and len(self._captured) >= target_page:
                    return len(self._captured)
            else:
                elapsed = int(_time.time() - last_progress_ts)
                # 静默策略：没拉到新视频不再日志（一次任务可能上百次，纯噪音）。
                # 仅在达到空闲阈值时打 WARNING 让用户感知卡顿。
                if elapsed >= idle_timeout:
                    logger.warning(
                        "[搜索翻页] 已连续 {}s 无新内容，停止翻页", idle_timeout,
                    )
                    break
        return len(self._captured) - start_count

    # ---------- 507 改造：筛选面板 UI 化 ----------

    # 筛选面板 UI 元素 selector 常量（抖音 2026-09-30 改版后）。
    # 旧版 selector（按浮层 / dialog / 确认按钮）已全部失效。
    # 新版结构：搜索结果页右上角「筛选^」按钮 → hover 出下拉面板 → 选项即时生效（无确认按钮）。
    # DOM 结构（auto_09_215521.html 截取）：
    #   <div class="StjIHdE0">            ← 触发按钮（tabindex=0）
    #     <span class="bR4uhU1W">筛选^</span>
    #   </div>
    #   <div class="IMWRHJOg">              ← 弹出面板容器
    #     <div>                              ← 一组（排序依据 / 发布时间 / ...）
    #       <div class="pvZiVjtd">组标题</div>
    #       <span data-index1="0" data-index2="0" class="KlEyP1lp">综合排序</span>
    #       <span data-index1="0" data-index2="1" class="KlEyP1lp HjptjtzN">最新发布</span>
    #       ...
    #     </div>
    #   </div>
    # 选中态：span.KlEyP1lp.HjptjtzN（橙色边框 + 浅红底）
    _FILTER_BTN_SELECTORS = [
        'div.StjIHdE0',                           # 触发按钮容器（hover 弹出）
        'span.bR4uhU1W',                          # 「筛选^」文字
    ]
    _FILTER_PANEL_SELECTOR = 'div.IMWRHJOg'      # 弹出面板容器
    _SORT_LATEST_SELECTORS = [
        # 主：精确字面量匹配（避免误命中含「最新发布」字样的其他文案）
        f'{_FILTER_PANEL_SELECTOR} span.KlEyP1lp:text-is("最新发布")',
        # 兜底：同 class 内的 :has-text 匹配，class 限定防止误命中面板外文本
        f'{_FILTER_PANEL_SELECTOR} span.KlEyP1lp:has-text("最新发布")',
    ]
    # 排序方式字面量映射（与 selection.VideoPullConditions.sort_type 对齐）
    # 0=综合 / 1=最多点赞 / 2=最新发布（默认）。未识别值按"最新发布"兜底。
    _SORT_TEXT = {
        0: "综合排序",
        1: "最多点赞",
        2: "最新发布",
    }
    _SORT_DEFAULT_TEXT = "最新发布"
    _PUBLISH_RANGE_TEXT = {
        "any": "不限",
        "1d": "一天内",
        "7d": "一周内",
        "180d": "半年内",
    }
    _DURATION_TEXT = {
        "any": "不限",
        "lt1m": "1分钟以下",
        "1to5m": "1-5分钟",
        "gt5m": "5分钟以上",
    }
    # 内容形式=视频：精确匹配「视频」字面量，避免误命中「图文」/「直播」等含「视」字项
    _CONTENT_VIDEO_SELECTORS = [
        f'{_FILTER_PANEL_SELECTOR} span.KlEyP1lp:text-is("视频")',
    ]
    # 无确认按钮：旧版 confirm selectors 全部删除（点选项即时生效）。

    def _apply_filters(self, page, conditions: dict, *, debug: bool = False) -> bool:
        """507 改造：搜索结果页点 [筛选] 按钮 + 设置排序/发布时间/视频时长/内容形式。

        返回 True 表示筛选面板成功设置至少一项；False 表示找不到面板（UI 改版或非结果页）。
        UI 改版 → 静默降级，不抛错（用户决策：筛选失效 = 不需要过滤）。

        507 #125 修复：通用 text= selector 加 parent 限定（filter panel 内）防误命中。
        507 #123 修复：点确认后等筛选 chip DOM 出现验证生效；找不到则记 warning。
        审查 #2 修复：删除 panel_scope 限定外的裸 text= 兜底。
        2026-09-30 改版适配：抖音新版「筛选^」按钮改为 hover 触发下拉面板，
        选项即时生效（无确认按钮）。DOM 结构：div.StjIHdE0 触发，
        div.IMWRHJOg 面板容器，div.pvZiVjtd 组标题，span.KlEyP1lp 选项
        （.HjptjtzN = 选中态）。旧 _FILTER_BTN_SELECTORS / _CONFIRM_BTN_SELECTORS
        / _FILTER_CHIP_SELECTORS 全部失效，本函数按新结构重写。

        防御：phase A 期间浏览器异常断开（chromium 崩溃 / asyncio loop 关闭）
        会让任何裸 wait_for_timeout 抛 Error: Page closed。外层 try/except 兜底
        按无筛选拉取，避免异常传播到 _run_pull_round 让任务直接失败（fail_reason
        会变成「Page closed」这种技术性文案）。
        """
        try:
            return self._apply_filters_inner(page, conditions, debug=debug)
        except Exception as e:
            # #fix-browser-closed-stop：浏览器已关闭 → 抛错中断，不再降级继续。
            # 否则后续 _poll_captured_until / _scroll_until 全部抛 Page closed 噪音。
            if _is_browser_closed_err(e):
                raise SearchBlockedError(
                    f"浏览器已关闭（筛选中断）：{e}"
                ) from e
            logger.warning("[筛选] 面板操作意外异常：{}（按无筛选拉取）", e)
            return False


    def _apply_filters_inner(self, page, conditions: dict, *, debug: bool = False) -> bool:
        """_apply_filters 实际逻辑：与外层 try/except 分离便于调试栈清晰。"""
        # 筛选面板失败不再每条 warning 刷屏：收集结果到尾部一次性 INFO。
        # 用户决策：筛选失效 = 不需要过滤，按无筛选拉取。
        # 1. hover 触发筛选按钮（hover 才能显示下拉面板，click 反而可能关闭）
        btn = None
        for sel in self._FILTER_BTN_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    btn = loc
                    break
            except Exception:
                continue
        if btn is None:
            logger.info("[筛选] 抖音筛选按钮找不到，本次按无筛选拉取")
            return False
        try:
            btn.hover()
        except Exception as e:
            # #fix-browser-closed-stop：浏览器关闭时中断，不再走「按无筛选拉取」
            _raise_if_browser_closed(e, "筛选按钮 hover")
            logger.info("[筛选] 筛选按钮 hover 失败 {}（按无筛选拉取）", e)
            return False
        # 2. 等面板容器出现
        try:
            page.wait_for_selector(self._FILTER_PANEL_SELECTOR, timeout=3_000)
        except Exception as e:
            # 区分浏览器关闭（中断）和"找不到面板"（降级继续）
            _raise_if_browser_closed(e, "等筛选面板")
            logger.info("[筛选] hover 后筛选面板未弹出（按无筛选拉取）")
            return False
        # 防御：wait_for_timeout 在 page 异常断开时会抛 Error: Page closed，
        # 包 try/except 后继续后续步骤（按部分生效拉取），不让单点故障阻断整个筛选。
        _safe_wait_or_raise(page, 500, "面板稳定等待")
        # 收集每步结果
        steps: list[tuple[str, bool]] = []
        # 3. 排序依据（0=综合 / 1=最多点赞 / 2=最新发布，默认 2）
        # 用户反馈：抖音 UI 切换有延迟，点完立即下一步会丢点击。点完等
        # 选中态 .HjptjtzN class 出现作为生效信号（最多 1s）。
        raw_sort = conditions.get("sort_type")
        try:
            sort_type = int(raw_sort) if raw_sort is not None else 2
        except (TypeError, ValueError):
            sort_type = 2
        # 0/1/2 越界或未知值按"最新发布"兜底（与 Pydantic 默认对齐）
        sort_text = self._SORT_TEXT.get(sort_type, self._SORT_DEFAULT_TEXT)
        sort_sels = [
            # 主：精确字面量匹配（避免误命中含该字样的其他文案）
            f'{self._FILTER_PANEL_SELECTOR} span.KlEyP1lp:text-is("{sort_text}")',
            # 兜底：同 class 内的 :has-text 匹配，class 限定防止误命中面板外文本
            f'{self._FILTER_PANEL_SELECTOR} span.KlEyP1lp:has-text("{sort_text}")',
        ]
        hit = self._click_filter_option(page, sort_sels, f"排序={sort_text}", debug=debug)
        steps.append((f"排序={sort_text}", hit))
        # #fix-filter-interval：每对相邻筛选间固定停顿 2s。
        # 用户反馈：抖音 UI 切换有延迟，相邻两项间隔过短会丢点击；
        # 固定 2s 间隔让 React 状态稳定 / DOM 选中态 class 完全渲染。
        # 注意：v5 wheel 路径下筛选切换不重发 XHR（XHR 只在首次 search + wheel 触发），
        # 此处 2s 不是等 XHR，是给 React 状态稳定的固定节奏。
        # 4 项 3 个间隔，共约 6s 等待（点击 + 2s × 3）。
        _safe_wait_or_raise(page, 2000, "筛选间隔1")
        # 4. 发布时间
        pr = conditions.get("publish_range") or "any"
        text = self._PUBLISH_RANGE_TEXT.get(pr, "不限")
        range_sels = [
            f'{self._FILTER_PANEL_SELECTOR} span.KlEyP1lp:has-text("{text}")',
        ]
        hit = self._click_filter_option(page, range_sels, f"发布时间={text}", debug=debug)
        steps.append((f"发布时间={text}", hit))
        _safe_wait_or_raise(page, 2000, "筛选间隔2")
        # 5. 视频时长
        dr = conditions.get("duration_range") or "any"
        dur_text = self._DURATION_TEXT.get(dr, "不限")
        dur_sels = [
            f'{self._FILTER_PANEL_SELECTOR} span.KlEyP1lp:has-text("{dur_text}")',
        ]
        hit = self._click_filter_option(page, dur_sels, f"视频时长={dur_text}", debug=debug)
        steps.append((f"视频时长={dur_text}", hit))
        _safe_wait_or_raise(page, 2000, "筛选间隔3")
        # 6. 内容形式 = 视频（精确匹配字面量，避免命中含「视」字选项如「图文/视频」）
        hit = self._click_filter_option(page, self._CONTENT_VIDEO_SELECTORS, "内容形式=视频", debug=debug)
        steps.append(("内容形式=视频", hit))
        # 7. 把鼠标移开收起面板（不影响后续搜索结果滚动）
        try:
            page.mouse.move(700, 500)
        except Exception as e:
            _raise_if_browser_closed(e, "mouse.move 收起面板")
            logger.info("[筛选] 鼠标移开失败 {}（不影响后续）", e)
        _safe_wait_or_raise(page, 300, "收起面板")
        # 一次性汇总
        parts = []
        for name, ok in steps:
            mark = "✓" if ok else "✗"
            parts.append(f"{name}{mark}")
        if all(ok for _, ok in steps):
            logger.info("[筛选] 面板操作完成：{}", "，".join(parts))
        else:
            logger.info("[筛选] 面板部分失效，按生效项拉取：{}", "，".join(parts))
        return True

    def _click_first_visible(
        self, page, selectors: list[str], desc: str, expect_visible_only: bool = False,
    ) -> bool:
        """遍历 selector 链找第一个可见元素：默认 click；expect_visible_only=True 仅检测可见性。

        507 #123：用于点确认后等筛选生效 chip DOM，不实际 click chip。
        """
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    if expect_visible_only:
                        return True
                    loc.click()
                    return True
            except Exception:
                continue
        return False

    def _click_filter_option(self, page, selectors: list[str], desc: str, *, debug: bool = False) -> bool:
        """点筛选面板选项 + 等 .HjptjtzN 选中态 class 出现（最多 1s）。

        用户反馈：抖音 UI 切换有延迟，点完立即下一步会丢点击。把「等选中态」
        作为生效信号，比固定 wait_for_timeout 更稳——已生效立即返回，未生效最多等 1s。

        实现注意：page.evaluate + document.querySelector 不识别 Playwright 的
        :has-text 扩展伪类，必须用 Playwright locator API（page.locator）。

        生产路径：选中态出现后 sleep 500ms 让抖音 React 派发 XHR 完成。
        debug=True：选中态后 sleep 2.5s 让用户能在浏览器里看清每步操作；
        probe 调试用。
        """
        clicked = self._click_first_visible(page, selectors, desc)
        if not clicked:
            return False
        settle_ms = 2500 if debug else 500
        try:
            for _ in range(10):
                for sel in selectors:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() == 0:
                            continue
                        ok = loc.evaluate(
                            "(el) => el && el.classList && el.classList.contains('HjptjtzN')"
                        )
                        if ok:
                            page.wait_for_timeout(settle_ms)
                            return True
                    except Exception as e:
                        # #fix-browser-closed-stop：浏览器关闭中断，不再 swallow 继续
                        _raise_if_browser_closed(e, f"等选中态[{desc}]")
                        continue
                page.wait_for_timeout(100)
        except SearchBlockedError:
            # 内层 _raise_if_browser_closed 已转 SearchBlockedError，原样透传不包装（避免丢异常链）
            raise
        except Exception as e:
            _raise_if_browser_closed(e, f"等选中态[{desc}]")
        return False

    def search_all_for_ids(
        self,
        keyword: str,
        conditions: dict | None = None,
        idle_timeout: int = 60,
        max_pages: int = 50,
        *,
        debug: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> list[str]:
        """507 改造：搜索 + 筛选 + 翻页一次拿完，返回去重后的 aweme_id 列表。

        阶段 A 不下载视频 / 不抓详情数据，只累积 aweme_id。
        阶段 B 用 aweme_id 列表独立循环详情 + 下载 + 入库。

        参数:
            keyword: 搜索关键词
            conditions: 拉取任务 conditions dict（用于筛选面板：sort/publish_range/duration_range）
            idle_timeout: 静默多少秒无新 XHR 停止
            max_pages: 最大页数上限
            progress_cb: 翻页进度回调（任务 #589：实时显示抓取状态）；
                签名 (累计页数, 累计视频数) -> None，每抓到一页 XHR 后调一次。
        返回:
            去重保序的 aweme_id 字符串列表
        """
        self._ensure_open()
        self._captured.clear()
        conditions = conditions or {}
        if self._searched_keyword != keyword:
            try:
                self._click_and_type_keyword(self._page, keyword)
            except SearchBlockedError:
                raise
            except Exception as e:
                raise SearchBlockedError(f"搜索框操作失败：{e}") from e
            self._searched_keyword = keyword
            # 等搜索结果页 DOM 渲染（feed 流容器 + 筛选按钮）
            page = self._page
            try:
                page.wait_for_selector(
                    '[data-e2e="search-card"]', timeout=10_000,
                )
            except Exception:
                pass  # 找不到也不阻塞，走 _apply_filters 自身的可见性判断
            # 507 改造：应用筛选面板（排序/发布时间/视频时长/内容形式）
            self._apply_filters(page, conditions, debug=debug)
        # 等首屏 XHR 落库
        self._poll_captured_until(lambda: bool(self._captured), timeout=60)
        # 滚到结束（has_more=0 / 60s 无进展 / max_pages 上限）
        self._scroll_until(
            target_page=None, idle_timeout=idle_timeout, max_pages=max_pages,
            progress_cb=progress_cb,
        )
        # 提取所有 aweme_id（去重保序）
        seen: set[str] = set()
        ids: list[str] = []
        for body in self._captured:
            for item in (body.get("data") or []):
                aid = ((item.get("aweme_info") or {}).get("aweme_id")) or item.get("aweme_id")
                if aid and aid not in seen:
                    seen.add(aid)
                    ids.append(aid)
        # A3: 搜索完成 INFO 改 DEBUG。数字已在 material_service 终态 progress
        # 「搜索阶段完成：共抓到 N 个视频」展示，再加 INFO 重复。
        logger.debug(
            "[搜索] 搜索完成，共抓取 {} 条不重复视频", len(ids),
        )
        # 跨 search 累计页数（入口 _captured.clear() 会丢上轮，所以出口累加）。
        # material_service 终态 progress 用此字段反映 retry 后真实总页数。
        self._total_captured_pages += len(self._captured)
        return ids

    def _diag_context(self) -> str:
        """统一诊断上下文：page.url / title / 已捕获 XHR。"""
        _lines: list[str] = []
        try:
            _lines.append(f"page.url={self._page.url}")
        except Exception:
            pass
        try:
            _lines.append(f"page.title={self._page.title()!r}")
        except Exception:
            pass
        try:
            _lines.append(f"已收到响应: {self._captured[:5]}")
        except Exception:
            pass
        return " | ".join(_lines)

    @staticmethod
    def _match_search_xhr(r) -> bool:
        """判断响应是否为抖音搜索结果 XHR。

        2026-09-30 实测抖音接口名变更：从 search/item 改为 general/search/single。
        监听器和 expect_response 共享此规则。
        """
        url = r.url
        if not url or "aweme/v1/web" not in url:
            return False
        return any(p in url for p in (
            "general/search/single",
            "general/search/stream",
            "search/item",
        ))

    def _click_and_type_keyword(self, page, keyword: str) -> None:
        """v5 真实用户路径：mouse.click + keyboard.type + keyboard.press("Enter")。

        替代 v2 locator.fill + locator.press("Enter") 路径——后者对 React 受控
        组件偶发不触发 input 事件（曾导致 Enter 后跳到 /jingxuan）。
        v5 实测 page.mouse.click + page.keyboard.type(80ms/char) 100% 命中
        general/search/single XHR。

        搜索框 selector 按实测命中率排序，找不到任何一个就抛 SearchBlockedError。
        """
        # 抖音首页搜索框 selector 链（按实测命中率）：
        # 1. data-e2e 标记（抖音前端埋点，最稳）
        # 2. class 标记 search-input
        # 3. placeholder 含「搜索」
        # 4. type="search"（兜底）
        input_selectors = [
            'input[data-e2e="searchbar-input"]',
            'input.search-input',
            'input[placeholder*="搜索"]',
            'input[type="search"]',
        ]
        box = None
        used_selector = ""
        for sel in input_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    box = loc.bounding_box()
                    if box:
                        used_selector = sel
                        break
            except Exception:
                continue
        if box is None:
            raise SearchBlockedError(
                f"找不到搜索框（试过 {input_selectors}）| page.url={page.url}"
            )
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        logger.debug(
            "[搜索] 已定位搜索框 selector={}", used_selector,
        )
        # #fix-search-dom-wait：显式等搜索框 DOM 渲染完成（state=visible），
        # 给到最长 5s 停顿。命中即停，正常 1-3s（首页 hydration 时间）。
        # 解决「页面没加载完就 type 导致关键词被 React state 初始化覆盖丢失」。
        # 浏览器关闭时中断而非 fallback 继续。
        try:
            page.wait_for_selector(
                used_selector,
                state="visible",
                timeout=5_000,
            )
        except Exception as e:
            _raise_if_browser_closed(e, "等搜索框 DOM")
            # 找不到：仅在 warning 留痕，继续走下面逻辑（selector 链可能误判可见性）
            logger.warning("[搜索] wait_for_selector 搜索框超时（5s），按 fallback 继续")
        # 真实 mouse 序列：move → click（触发 focus + mousedown + mouseup + click）
        # #fix-browser-closed-stop：DOM 等到后、click 前用户关浏览器 → Page closed，
        # 必须经 closed 检测链转 SearchBlockedError 中断。
        try:
            page.mouse.move(cx, cy)
            page.mouse.click(cx, cy)
        except Exception as e:
            _raise_if_browser_closed(e, "点击搜索框")
            raise SearchBlockedError(f"点击搜索框失败：{e}") from e
        # #fix-search-typing：等搜索框获得焦点（页面可能未加载完，500ms 硬等不够）。
        # React 受控 input 在 hydration 完成前，type 进 DOM 的字符会被 React state 初始化覆盖丢失。
        # 等 activeElement === 搜索框 + 额外 200ms 让 React 接管 onChange 后再输入。
        try:
            page.wait_for_function(
                "(sel) => document.activeElement && document.activeElement.matches(sel)",
                arg=used_selector, timeout=5000,
            )
            _safe_wait_or_raise(page, 200, "focus 后等 React 接管")
        except Exception as e:
            # #fix-browser-closed-stop：浏览器关闭时中断（不要兜底继续输入）
            _raise_if_browser_closed(e, "等搜索框 focus")
            # 兜底：等不到 focus 仍按旧路径走（不要因等待失败中断搜索）
            logger.warning("[搜索] 等搜索框 focus 超时（5s），按 fallback 继续")
            _safe_wait_or_raise(page, 500, "focus 兜底等待")
        # #fix-browser-closed-stop：type/Enter/wait_for_timeout 全阶段补 closed 检测。
        # 用户在 type 进行中关浏览器 → keyboard.type 抛 closed 时也要中断，
        # 不应再走 swallow / outer fallback 继续输入。
        try:
            # 真实键盘输入：每个字符派发 keydown/keypress/keyup/input 事件
            page.keyboard.type(keyword, delay=80)
            _safe_wait_or_raise(page, 500, "type 后等待")
            # 真实键盘 Enter（触发 form submit / keyboard event 链）
            page.keyboard.press("Enter")
            _safe_wait_or_raise(page, 1500, "Enter 后等待")
        except Exception as e:
            _raise_if_browser_closed(e, "输入阶段")
            raise SearchBlockedError(f"输入阶段失败：{e}") from e
        # A4: 「已发起搜索」改 DEBUG。「启动浏览器」INFO 已覆盖关键事件，
        # 用户视角从 material_service 的 progress「搜索阶段：翻页采集中...」
        # 感知搜索在跑，此条仅留 trace 供排障。
        logger.debug(
            "[搜索] 已发起搜索 关键词={!r} 当前页={}",
            keyword, page.title(),
        )

    def close(self) -> None:
        """关闭会话：先关 ctx（chromium 子进程），再走 PlaywrightContextManager.__exit__
        （停 driver + 关闭 asyncio loop）。"""
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        self._page = None
        if self._pw_cm is not None:
            try:
                self._pw_cm.__exit__(None, None, None)
            except Exception:
                # 二次调用或已关闭时静默吞
                pass
            self._pw_cm = None
            self._pw = None

    def get_page(self):
        """暴露当前 page 给阶段 B 复用（同 persistent_context，无 chromium lock 冲突）。

        阶段 A 跑完搜索后 page 还停留在搜索结果页。阶段 B 可以 close 该 page
        再开新 page 抓详情（共用同一 ctx），或直接 page.goto 详情 URL。

        必须在 search_session.close() 之前用完；close 后 page 已被置 None。
        """
        return self._page

    def __enter__(self):
        # with 上下文进入即 lazy launch + goto 首页（search_page 调用前完成准备）
        self._ensure_open()
        return self

    def __exit__(self, *exc):
        self.close()


# ---------- 公开单页接口（兼容旧 search_single 调用风格） ----------

def search_single(
    profile_dir: Path,
    keyword: str,
    page: int = 1,
    search_id: str = "",  # 保留参数占位（实际浏览器会话无需 search_id）
    sort_type: int = 0,
    publish_time: int = 0,
    filter_duration: str = "",
    _session: Optional[BrowserSearchSession] = None,
) -> dict:
    """浏览器版单页搜索（替换旧 curl_cffi 实现，#506，2026-09-30 升级到 v5 wheel）。

    返回抖音搜索接口原始 JSON dict，含以下字段供调用方判断翻页：
        - data: list[dict] 本页视频项（含 aweme_info 嵌套）
        - cursor: int 下一请求 cursor
        - has_more: int 1=还有下一页 / 0=已到末尾
        - status_code: int 0=成功 / 非0=被风控

    调用方（如 material_service）可据此决定是否继续 search_single(page+1)，
    或直接用 search_all 一次性拉完。

    参数:
        profile_dir: 账号持久化 chromium profile 目录（accounts/<id>/profile/）
        keyword/page/sort_type/publish_time/filter_duration: 同旧签名
        _session: 复用 BrowserSearchSession（material_service 一轮任务传同一个，
                  跨页复用避免每页 rebuild playwright 进程）
    返回:
        抖音搜索接口原始 JSON dict
    异常:
        SearchBlockedError
    """
    own = False
    sess = _session
    if sess is None:
        sess = BrowserSearchSession(profile_dir, headless=True)
        own = True
    try:
        offset = (page - 1) * 10
        return sess.search_page(
            keyword=keyword,
            offset=offset,
            sort_type=sort_type,
            publish_time=publish_time,
            filter_duration=filter_duration,
        )
    finally:
        if own:
            sess.close()


# 排序/发布时间/时长选项常量（前端 UI 下拉用）
SORT_TYPE_DEFAULT = 0
PUBLISH_TIME_MAP = {"any": 0, "1d": 1, "7d": 7, "180d": 180}
PUBLISH_TIME_DEFAULT = 0
DURATION_MAP = {"any": "", "lt1m": "0-1", "1to5m": "1-5", "gt5m": "5-10000"}