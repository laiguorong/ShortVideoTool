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
from typing import Optional
from urllib.parse import quote as _url_quote

from loguru import logger


class SearchBlockedError(Exception):
    """搜索接口被风控拦截（浏览器拦截或 status_code!=0）。"""


_SEARCH_URL = "https://www.douyin.com/aweme/v1/web/search/item/"


class BrowserSearchSession:
    """单页搜索会话：每次构造自带同步 playwright context + persistent context。

    设计：
    - 构造时 lazy launch（耗时 ~3-5s），只跑一页 search_page 后 close
    - 严格用 `with sync_playwright() as pw:` 走完整 __exit__ 关 driver + asyncio loop
    - material_service 每页 search 前 new + close 一次，避免与 detail 路径冲突
    """

    def __init__(self, profile_dir: Path, headless: bool = True):
        self._profile_dir = str(profile_dir)
        self._headless = headless
        self._pw = None              # SyncPlaywright 实例
        self._pw_cm = None           # PlaywrightContextManager（持有 loop）
        self._ctx = None             # BrowserContext（launch_persistent_context）
        self._page = None
        # 当前页捕获的响应列表（每次 search_page 入口清空 + 复用同一监听器写入）
        self._captured: list[dict] = []
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
        from app.core.douyin.browser import UA, browser_actor
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
            "[搜索会话] 启动浏览器（playwright） profile_dir={} headless={}",
            self._profile_dir, self._headless,
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
        # 一次性注册监听器（避免多次 on() 累积导致重复 append 错位）
        self._page.on("response", self._on_response)
        # 2026-09-30 v2：导航到抖音首页（建立登录态 + 搜索框 DOM），
        # 后续 search_page 通过 fill + Enter 在站内触发搜索。
        # 直跳 search/<keyword> 会被风控识别为爬虫（缺 keyboard event 指纹）。
        try:
            self._page.goto(
                "https://www.douyin.com/", wait_until="domcontentloaded", timeout=30_000,
            )
            logger.info(
                "[搜索会话] 已加载首页 url={} title={!r}",
                self._page.url, self._page.title(),
            )
        except Exception as e:  # noqa: BLE001
            # 首页加载失败不影响后续搜索（可能 profile 已带登录态），仅留痕
            logger.warning("[搜索会话] 首页加载异常 {}（继续走 search_page）", e)

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

    # ---------- v3 用户滚动模式 API（已废弃，v5 wheel 路径直接处理） ----------

    def trigger_search(self, keyword: str) -> None:
        """已废弃（DEPRECATED 2026-09-30）。v5 wheel 路径直接由 search_page /
        search_all_via_wheel 内部调用 _click_and_type_keyword，不再需要外部手动
        触发后人工滚动。请改用 search_page(keyword, offset=N) 或
        search_all_via_wheel(keyword)。

        保留仅供兼容旧调用方：内部等价于点击搜索框 + 输入关键词 + Enter。
        """
        import warnings
        warnings.warn(
            "trigger_search 已废弃，请改用 search_page 或 search_all_via_wheel",
            DeprecationWarning, stacklevel=2,
        )
        self._ensure_open()
        if self._searched_keyword != keyword:
            try:
                self._click_and_type_keyword(self._page, keyword)
            except SearchBlockedError:
                raise
            except Exception as e:
                raise SearchBlockedError(f"搜索框操作失败：{e}") from e
            self._searched_keyword = keyword

    def wait_for_next_xhr(self, timeout: int = 60) -> list[dict]:
        """已废弃（DEPRECATED 2026-09-30）。v5 wheel 路径不需要外部等待 XHR。
        请改用 search_page(keyword, offset=N) 等目标页 XHR 自然到达。
        """
        import warnings
        warnings.warn(
            "wait_for_next_xhr 已废弃，请改用 search_page",
            DeprecationWarning, stacklevel=2,
        )
        self._ensure_open()
        try:
            with self._page.expect_response(
                self._match_search_xhr, timeout=timeout * 1000,
            ):
                pass
        except Exception as exc:
            _diag = self._diag_context()
            raise SearchBlockedError(
                f"等待搜索 XHR 超时：{exc} | 诊断：{_diag}"
            ) from exc
        return list(self._captured)

    def get_captured_responses(self) -> list[dict]:
        """已废弃（DEPRECATED 2026-09-30）。v5 不需要手动读 _captured，
        search_page / search_all_via_wheel 直接返回结果。
        """
        import warnings
        warnings.warn(
            "get_captured_responses 已废弃，请改用 search_page / search_all_via_wheel",
            DeprecationWarning, stacklevel=2,
        )
        return list(self._captured)

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
        """
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if predicate() and self._captured:
                body = self._captured[-1]
                self._last_cursor = body.get("cursor")
                return body
            self._page.wait_for_timeout(200)
        _diag = self._diag_context()
        raise SearchBlockedError(
            f"等待 XHR 超时（{timeout}s 内未捕获 general/search/single）"
            f" | 诊断：{_diag}"
        )

    def _scroll_until(
        self,
        target_page: int | None = None,
        idle_timeout: int = 60,
        max_pages: int = 50,
        wheel_delta: int = 300,
        wheels_per_round: int = 5,
        round_settle_ms: int = 1500,
        wheel_interval_ms: int = 150,
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
        last_progress_ts = _time.time()
        start_count = len(self._captured)
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
                logger.info(
                    "[搜索翻页] wheel 命中 +{} 当前累积={} cursor={} has_more={}",
                    delta, len(self._captured), cursor, has_more,
                )
                # 终止条件
                if has_more == 0:
                    logger.info("[搜索翻页] has_more=0，到达底部")
                    break
                if len(self._captured) - start_count >= max_pages:
                    logger.info("[搜索翻页] 达到 max_pages={}", max_pages)
                    break
                if target_page is not None and len(self._captured) >= target_page:
                    return len(self._captured)
            else:
                elapsed = int(_time.time() - last_progress_ts)
                sy = page.evaluate("window.scrollY")
                sh = page.evaluate("document.body.scrollHeight")
                logger.info(
                    "[搜索翻页] 静默 {}s 无新 XHR | scrollY={} scrollHeight={}",
                    elapsed, sy, sh,
                )
                if elapsed >= idle_timeout:
                    logger.warning("[搜索翻页] {}s 无进展，停止", idle_timeout)
                    break
        return len(self._captured) - start_count

    def search_all_via_wheel(
        self,
        keyword: str,
        idle_timeout: int = 60,
        max_pages: int = 50,
    ) -> list[dict]:
        """v5 wheel 真实滚动一次拿完所有数据。

        适用场景：material_service 单次任务拉完整关键词的全部搜索结果，
        不需要分页调用 search_page。
        返回：list of aweme dict（每条为单页 data 数组元素，含 aweme_info 嵌套），
        按 XHR 到达顺序排列，已按 aweme_id 去重。
        """
        self._ensure_open()
        self._captured.clear()
        if self._searched_keyword != keyword:
            try:
                self._click_and_type_keyword(self._page, keyword)
            except SearchBlockedError:
                raise
            except Exception as e:
                raise SearchBlockedError(f"搜索框操作失败：{e}") from e
            self._searched_keyword = keyword
        # 先确保拿到首屏（点击 Enter 后等 XHR 落库）
        self._poll_captured_until(lambda: bool(self._captured), timeout=60)
        # 滚到结束
        self._scroll_until(
            target_page=None, idle_timeout=idle_timeout, max_pages=max_pages,
        )
        # 合并所有 data，按 aweme_id 去重保序
        seen: set[str] = set()
        out: list[dict] = []
        for body in self._captured:
            for item in (body.get("data") or []):
                aid = ((item.get("aweme_info") or {}).get("aweme_id")) or item.get("aweme_id")
                if aid and aid not in seen:
                    seen.add(aid)
                    out.append(item)
        logger.info(
            "[搜索] search_all_via_wheel 完成 XHR={} 去重 aweme={}",
            len(self._captured), len(out),
        )
        return out

    # ---------- 507 改造：筛选面板 UI 化 ----------

    # 筛选面板 UI 元素 selector 常量（抖音搜索结果页右上角 [筛选]）
    # 507 需求：排序依据=最新发布，发布时间 / 视频时长 / 内容形式 按 conditions 选。
    # UI 改版 → selector 失效时静默降级为无筛选（用户决策）。
    _FILTER_BTN_SELECTORS = [
        'button:has-text("筛选")',
        '[data-e2e="filter-btn"]',
        'div:has-text("筛选")',
    ]
    _SORT_LATEST_SELECTORS = [
        'text="最新发布"',
        'text="最新"',
        'div:has-text("最新发布")',
    ]
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
    _CONTENT_VIDEO_SELECTORS = [
        # 加 parent 限定防误命中（如分类标题/相关推荐含「视频」字样）
        'div[role="radio"]:has-text("视频")',
        '[class*="filter-content"] [role="radio"]:has-text("视频")',
        '[class*="filter"] label:has-text("视频")',
        'label:has-text("视频"):not(:has-text("视频推荐"))',
    ]
    _CONFIRM_BTN_SELECTORS = [
        'button:has-text("确定")',
        'button:has-text("确认")',
        'button:has-text("完成")',
        '[data-e2e="filter-confirm"]',
    ]
    # 507 #123 验证：筛选 chip / 已选状态（点确认后等 DOM 变化）
    # 抖音筛选生效后通常在结果页顶部展示「已选：最新发布 / 7天内 / 1-5分钟 / 视频」chip
    _FILTER_CHIP_SELECTORS = [
        '[class*="filter-active"]',
        '[class*="selected-tag"]',
        '[data-e2e="filter-chip"]',
        'div:has-text("已选筛选")',
    ]

    def _apply_filters(self, page, conditions: dict) -> bool:
        """507 改造：搜索结果页点 [筛选] 按钮 + 设置排序/发布时间/视频时长/内容形式。

        返回 True 表示筛选面板成功设置至少一项；False 表示找不到面板（UI 改版或非结果页）。
        UI 改版 → 静默降级，不抛错（用户决策：筛选失效 = 不需要过滤）。

        507 #125 修复：通用 text= selector 加 parent 限定（filter panel 内）防误命中。
        507 #123 修复：点确认后等筛选 chip DOM 出现验证生效；找不到则记 warning。
        """
        # 1. 点 [筛选] 按钮
        if not self._click_first_visible(page, self._FILTER_BTN_SELECTORS, "筛选按钮"):
            logger.warning("[筛选] 找不到筛选按钮，按无筛选拉取")
            return False
        page.wait_for_timeout(500)
        # 面板限定 selector（点了筛选按钮后，filter panel 一般是浮层/抽屉）
        panel_scope = '[class*="filter-panel"], [class*="filter-modal"], [role="dialog"]'
        # 2. 排序依据 = 最新发布（507 强制要求）
        if not self._click_first_visible(
            page, [f'{panel_scope} {sel}' for sel in self._SORT_LATEST_SELECTORS] + list(self._SORT_LATEST_SELECTORS),
            "最新发布",
        ):
            logger.warning("[筛选] 找不到'最新发布'选项")
        page.wait_for_timeout(300)
        # 3. 发布时间
        pr = conditions.get("publish_range") or "any"
        text = self._PUBLISH_RANGE_TEXT.get(pr, "不限")
        range_sels = [
            f'{panel_scope} text="{text}"',
            f'{panel_scope} div[role="radio"]:has-text("{text}")',
            f'text="{text}"',
            f'div[role="radio"]:has-text("{text}")',
        ]
        if not self._click_first_visible(page, range_sels, f"发布时间={text}"):
            logger.warning("[筛选] 找不到发布时间选项 {}= {}", pr, text)
        page.wait_for_timeout(300)
        # 4. 视频时长
        dr = conditions.get("duration_range") or "any"
        dur_text = self._DURATION_TEXT.get(dr, "不限")
        dur_sels = [
            f'{panel_scope} text="{dur_text}"',
            f'{panel_scope} div[role="radio"]:has-text("{dur_text}")',
            f'text="{dur_text}"',
            f'div[role="radio"]:has-text("{dur_text}")',
        ]
        if not self._click_first_visible(page, dur_sels, f"视频时长={dur_text}"):
            logger.warning("[筛选] 找不到视频时长选项 {}= {}", dr, dur_text)
        page.wait_for_timeout(300)
        # 5. 内容形式 = 视频
        if not self._click_first_visible(
            page, [f'{panel_scope} {sel}' for sel in self._CONTENT_VIDEO_SELECTORS] + list(self._CONTENT_VIDEO_SELECTORS),
            "内容形式=视频",
        ):
            logger.warning("[筛选] 找不到'视频'选项")
        page.wait_for_timeout(300)
        # 6. 点确认按钮关闭面板
        if not self._click_first_visible(page, self._CONFIRM_BTN_SELECTORS, "确认按钮"):
            logger.warning("[筛选] 找不到确认按钮，尝试点页面其他位置关闭面板")
            # 兜底：点页面边缘收起面板
            try:
                page.mouse.click(10, 200)
                page.wait_for_timeout(500)
            except Exception:
                pass
        else:
            # #123 验证：点确认后等筛选生效信号（chip / 已选标签）
            page.wait_for_timeout(800)
            chip_visible = self._click_first_visible(
                page, self._FILTER_CHIP_SELECTORS, "筛选生效chip", expect_visible_only=True
            )
            if not chip_visible:
                # chip 找不到不视为致命（抖音 UI 可能不渲染 chip），仅记 warning
                logger.warning("[筛选] 点确认后未找到已选chip，筛选可能未生效")
        logger.info("[筛选] 面板操作完成 publish_range={} duration_range={}", pr, dr)
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

    def search_all_for_ids(
        self,
        keyword: str,
        conditions: dict | None = None,
        idle_timeout: int = 60,
        max_pages: int = 50,
    ) -> list[str]:
        """507 改造：搜索 + 筛选 + 翻页一次拿完，返回去重后的 aweme_id 列表。

        阶段 A 不下载视频 / 不抓详情数据，只累积 aweme_id。
        阶段 B 用 aweme_id 列表独立循环详情 + 下载 + 入库。

        参数:
            keyword: 搜索关键词
            conditions: 拉取任务 conditions dict（用于筛选面板：sort/publish_range/duration_range）
            idle_timeout: 静默多少秒无新 XHR 停止
            max_pages: 最大页数上限
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
            self._apply_filters(page, conditions)
        # 等首屏 XHR 落库
        self._poll_captured_until(lambda: bool(self._captured), timeout=60)
        # 滚到结束（has_more=0 / 60s 无进展 / max_pages 上限）
        self._scroll_until(
            target_page=None, idle_timeout=idle_timeout, max_pages=max_pages,
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
        logger.info(
            "[搜索] search_all_for_ids 完成 XHR={} 去重 aweme_id={}",
            len(self._captured), len(ids),
        )
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
        logger.info(
            "[搜索会话] 真实点击搜索框 selector={} 位置=({}, {})",
            used_selector, cx, cy,
        )
        # 真实 mouse 序列：move → click（触发 focus + mousedown + mouseup + click）
        page.mouse.move(cx, cy)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(500)
        # 真实键盘输入：每个字符派发 keydown/keypress/keyup/input 事件
        page.keyboard.type(keyword, delay=80)
        page.wait_for_timeout(500)
        # 验证 input.value 写入成功（排查 placeholder 错位等异常）
        try:
            value = page.evaluate(
                'document.querySelector(\'input[data-e2e="searchbar-input"]\')?.value || ""'
            )
        except Exception:
            value = "<eval fail>"
        logger.info(
            "[搜索会话] type 完成 keyword={!r} input.value={!r} url={}",
            keyword, value, page.url,
        )
        # 真实键盘 Enter（触发 form submit / keyboard event 链）
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        logger.info(
            "[搜索会话] Enter 后 url={} title={!r}",
            page.url, page.title(),
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


def search_all(
    profile_dir: Path,
    keyword: str,
    search_id: str = "",
    sort_type: int = 0,
    publish_time: int = 0,
    filter_duration: str = "",
    _session: Optional[BrowserSearchSession] = None,
    idle_timeout: int = 60,
    max_pages: int = 50,
) -> list[dict]:
    """浏览器版一次性拉完搜索结果（v5 wheel 路径）。

    适用场景：material_service 单次任务拿完整关键词的全部视频，UI 不需要
    「加载更多」按钮。内部走 wheel 真实滚动累积，has_more=0 / 60s 无进展
    / max_pages 三个终止条件。

    参数:
        profile_dir: 账号持久化 chromium profile 目录
        keyword: 搜索关键词
        search_id/sort_type/publish_time/filter_duration: 占位（v5 未生效）
        _session: 复用 BrowserSearchSession
        idle_timeout: 静默多少秒无新 XHR 后停止，默认 60s
        max_pages: 最大页数上限，默认 50
    返回:
        list[dict] 去重后的视频项（每个为 data 数组元素，按 XHR 到达顺序）
    异常:
        SearchBlockedError: 找不到搜索框 / 首屏 XHR 失败
    """
    own = False
    sess = _session
    if sess is None:
        sess = BrowserSearchSession(profile_dir, headless=True)
        own = True
    try:
        return sess.search_all_via_wheel(
            keyword=keyword,
            idle_timeout=idle_timeout,
            max_pages=max_pages,
        )
    finally:
        if own:
            sess.close()


# 排序/发布时间/时长选项常量（前端 UI 下拉用）
SORT_TYPE_DEFAULT = 0
PUBLISH_TIME_MAP = {"any": 0, "1d": 1, "7d": 7, "180d": 180}
PUBLISH_TIME_DEFAULT = 0
DURATION_MAP = {"any": "", "lt1m": "0-1", "1to5m": "1-5", "gt5m": "5-10000"}