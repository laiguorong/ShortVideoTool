# -*- coding: utf-8 -*-
"""抖音网页版搜索接口浏览器持久化会话版（#506 重写）。

# 506 改造：旧版用 curl_cffi 裸 HTTP 调 /aweme/v1/web/general/search/single/，
# 被搜索接口 2483 风控拦截。根因是搜索接口需要完整登录会话（持久化 chromium
# profile 的 storage_state + IndexedDB + ttwid 设备指纹），裸 cookie 不足。

# 新版走 launch_persistent_context(user_data_dir=profile_dir)——和发布视频
# (publish_actions 行 988) 一致。导航 www.douyin.com/search/<keyword>，
# 拦截 /aweme/v1/web/search/item/ XHR 响应（这是当前浏览器实际搜索接口，
# 验证 status_code=0 返回 aweme_list）。

# 翻页实现：URL offset 翻页 + page.goto 重 navigate（profile 持久化登录态）。
# 抖音 PC 搜索页 IntersectionObserver 不响应 window.scrollTo 触发新 XHR，
# 改用不同 offset 重导航；profile 持久化使浏览器复用登录态，免二次登录。

# 监听器生命周期：_on_response 在 _ensure_open() 一次性注册到 page，
# 每次 search_page 复用同一监听器——避免重复 on() 累积；search_page 用
# page.expect_response() 等待匹配 URL 的响应，比 polling wait_for_timeout
# 更准、避免捕获到旧 XHR 残留响应。

# asyncio loop 生命周期：BrowserSearchSession 严格用 `with sync_playwright()`
# 上下文管理器管理 PlaywrightContextManager，__exit__ 会完整关闭 driver 子进程
# + asyncio loop。手工调 `_pw_cm.start()` + `_pw_cm.__exit__` 配合 _own_loop=True
# 的方案在同线程 BrowserActor.detail 中转后仍残留 loop（page 2 search 抛
# "inside the asyncio loop"）。换成真正的 with 模式后实测不再冲突。
"""

import json
import time
from pathlib import Path
from typing import Optional

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

    def _ensure_open(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        from app.core.douyin.browser import UA
        # #P0-11：BrowserActor detail path 在同线程持有 sync_playwright runtime，
        # dispatcher loop 持续 running（_tls.playwright 不关）→ 当前线程
        # asyncio.get_running_loop() 返回 is_running=True 的 loop。
        # PlaywrightContextManager.__enter__ 检测到 running loop 抛
        # "inside the asyncio loop"。
        # 解法：search 前先强制关闭 BrowserActor 同线程的 playwright + browser runtime，
        # 再起新的 sync_playwright()——两个 runtime 在同一线程不共存，避免 loop 残留冲突。
        # BrowserActor 后续 detail 时会自动重建（_ensure_browser 检查 is_connected）。
        import asyncio as _asyncio
        from app.core.douyin.browser import browser_actor
        tls = getattr(browser_actor, "_tls", None)
        if tls:
            old_browser = getattr(tls, "browser", None)
            if old_browser is not None:
                try:
                    old_browser.close()
                except Exception:
                    pass
                tls.browser = None
            old_pw = getattr(tls, "playwright", None)
            if old_pw is not None:
                try:
                    old_pw.stop()
                except Exception:
                    pass
                tls.playwright = None

        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.__enter__()
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

    def _on_response(self, r) -> None:
        """统一响应拦截：仅匹配 search/item XHR 写入 _captured。"""
        url = r.url
        if url and "search/item" in url and "aweme/v1/web" in url:
            try:
                self._captured.append(r.json())
            except Exception:
                pass

    def search_page(
        self,
        keyword: str,
        offset: int = 0,
        sort_type: int = 0,
        publish_time: int = 0,
        filter_duration: str = "",
        timeout: int = 20,
    ) -> dict:
        """在已 launch 的浏览器里拦截 search/item XHR 拿一页结果。

        策略：
        - 清空 _captured 复用同一监听器
        - page.expect_response() 等待匹配 search/item 的响应（比 polling 更准）
        - page.goto 触发搜索（首屏 + 翻页都换 offset 重 navigate）

        参数:
            keyword: 搜索关键词
            offset: 翻页 offset（0 首次；>0 后续）
            sort_type/publish_time/filter_duration: 筛选下推（filter_selected 走 URL）
        返回:
            search/item 原始 JSON dict
        异常:
            SearchBlockedError: status_code!=0 或超时无 XHR
        """
        from urllib.parse import quote as _quote
        self._ensure_open()
        page = self._page
        # 清空上一轮捕获（监听器复用，必须显式清空避免新旧响应混杂）
        self._captured.clear()

        # 构造 URL（含 filter_selected 下推）
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "search_channel": "aweme_video_web",
            "sort_type": str(sort_type),
            "publish_time": str(publish_time),
            "keyword": keyword,
            "search_source": "normal_search",
            "offset": str(offset),
            "count": "10",
            "from_group_id": "",
            "pc_client_type": "1",
            "version_code": "190600",
            "version_name": "19.6.0",
            "cookie_enabled": "true",
        }
        if filter_duration:
            params["is_filter_search"] = "1"
            params["filter_selected"] = json.dumps(
                {"filter_duration": filter_duration}, separators=(",", ":")
            )
        # keyword 只在路径里出现一次（参数里不重复编码，避免二次解码乱码）
        encoded_keyword = _quote(keyword, safe="")
        encoded_params = [(k, _quote(str(v), safe="")) for k, v in params.items() if k != "keyword"]
        qs = "&".join(f"{k}={v}" for k, v in encoded_params)
        url = f"https://www.douyin.com/search/{encoded_keyword}?type=video&keyword={encoded_keyword}&{qs}"

        try:
            # 用 expect_response 等匹配 URL 的响应，避免 polling + wait_for_timeout 抓旧 XHR
            with page.expect_response(
                lambda r: "search/item" in r.url and "aweme/v1/web" in r.url,
                timeout=timeout * 1000,
            ) as resp_info:
                page.goto(url, wait_until="domcontentloaded")
            try:
                body = resp_info.value.json()
            except Exception as e:
                raise SearchBlockedError(f"搜索 XHR 响应非 JSON：{e}") from e
        except Exception as e:
            # Playwright TimeoutError / 其它异常统一包为 SearchBlockedError
            if isinstance(e, SearchBlockedError):
                raise
            raise SearchBlockedError(f"搜索 XHR 等待失败：{e}") from e
        if body.get("status_code") not in (0, None):
            raise SearchBlockedError(
                f"搜索接口 status_code={body.get('status_code')}"
            )
        return body

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
    """浏览器版单页搜索（替换旧 curl_cffi 实现，#506）。

    参数:
        profile_dir: 账号持久化 chromium profile 目录（accounts/<id>/profile/）
        keyword/page/sort_type/publish_time/filter_duration: 同旧签名
        _session: 复用 BrowserSearchSession（material_service 一轮任务传同一个）
    返回:
        search/item JSON dict
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