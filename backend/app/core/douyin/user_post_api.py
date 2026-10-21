# -*- coding: utf-8 -*-
"""抖音用户主页作品采集（2026-10-21 新增）。

# 与搜索拉取的关系：
- 素材拉取任务分阶段 A（只拿 aweme_id 列表）+ 阶段 B（详情/过滤/下载/入库）。
  本模块负责给「主页来源」的拉取任务提供阶段 A 的作品来源。
- 阶段 B 完全复用现有实现，本模块不涉及。

# 为什么继承 BrowserSearchSession 而不是复制：
launch / wheel 滚动 / 验证码检测 / 浏览器关闭防御这些代码里塞满了
asyncio loop 冲突（#P0-11）、chromium lock 冲突、浏览器被手动关闭
（#fix-browser-closed）等坑注释，复制一份必然漏掉某处；且同一 profile 目录
同时开两个 chromium 会直接撞 lock。所以本类只覆写「XHR 匹配规则」并新增
采集方法，其余全部继承，`search_api.py` 保持零改动。

# 与搜索页的关键差异：
1. 导航方式——搜索走「首页 + 搜索框输入」（直跳 search URL 被识别为爬虫，
   XHR 完全不返回）；主页走 page.goto 直达（首页 goto 已验证可用，
   详情页 goto 同理）。
2. 接口——搜索是 general/search/single，主页作品是 aweme/v1/web/aweme/post/。
3. 响应结构——搜索响应是 data[]（内嵌 aweme_info），主页响应是 aweme_list[]
   （扁平方���）。故覆写 _sum_aweme_in_body。
4. 图文混排——抖音主页「作品」tab 图文（note）与视频混排，而阶段 B 用
   url_kind="video" 打开 /video/{id}，图文 ID 会 404。故本模块主动过滤图文
   （与搜索侧「切视频 tab」达到同一效果）。
5. 首屏可能 SSR——主页首屏不一定发 XHR，故加 DOM 兜底抓 a[href*="/video/"]。
"""

import re
from typing import Callable, Optional

from loguru import logger

from app.core.douyin.search_api import (
    BrowserSearchSession,
    SearchBlockedError,
    _raise_if_browser_closed,
)

# 主页作品列表接口（抖音 web 端路径）
_POST_API = "aweme/v1/web/aweme/post/"
# 主页作品里显式排除的接口：comment 等子路径理论上也可能带 post 前缀，
# 白名单式排除比前缀包含更安全。
_POST_EXCLUDED = (
    "post/comment",
    "post/repost",
)

# 主页 URL 形态（完整链接）：www.douyin.com/user/<sec_uid>
#                          www.iesdouyin.com/share/user/<sec_uid>（移动端分享落地页）
_USER_URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:ies)?douyin\.com/(?:share/)?user/([A-Za-z0-9_-]{10,})",
    re.IGNORECASE,
)
# 短链形态：v.douyin.com/<短码>（短码可含连字符，如 5Ob-LbF5RLM）
_SHORT_URL_RE = re.compile(r"https?://v\.douyin\.com/[\w-]+/?", re.IGNORECASE)
# 分享文本抽 URL（与 share_import_service 的既有做法一致）
_TEXT_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
# 裸 sec_uid（用户直接粘 ID，不带任何 URL 包装）
# 下限 10：抖音 sec_uid 长度不固定，短的在 16 位左右（MS4wLjABAAAA 开头的
# base64 段）。整行 match + 纯 base64 字符集，中文描述行不会被误判。
_BARE_SEC_UID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
# DOM 兜底：从作品列表卡片 href 提 19 位数字 ID
_DOM_AWEME_ID_RE = re.compile(r"/(?:video|note)/(\d{15,25})")

# 移动端 UA：短链 302 在 PC UA 下偶发跳首页，降级用（对齐 client.resolve_share）
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _is_captcha_url(url: str) -> bool:
    """判断 URL 是否被重定向到验证/登录页（风控拦截的表征）。"""
    u = (url or "").lower()
    return "verify" in u or "captcha" in u or "/login" in u


def _is_note_aweme(aw: dict) -> bool:
    """判断 aweme 是否为图文（note）而非视频。

    抖音图文标识：aweme_type=68，或 images / video.images 节点非空。
    阶段 B 固定用 url_kind="video" 打开 /video/{id}，图文 ID 打开会 404，
    故阶段 A 必须先滤掉。
    """
    if not isinstance(aw, dict):
        return False
    if aw.get("aweme_type") == 68:
        return True
    if aw.get("images"):
        return True
    video = aw.get("video")
    if isinstance(video, dict) and video.get("images"):
        return True
    return False


def extract_sec_uid(line: str) -> str | None:
    """从一行用户输入解析出 sec_uid，失败返回 None（不抛异常）。

    沿用项目既有分享解析风格（client.py resolve_share）：先从整行文本正则抽
    URL，再短链 302 取 Location。支持的输入形态：
        - https://www.douyin.com/user/MS4wLjABAAAA...
        - https://www.iesdouyin.com/share/user/MS4wLjABAAAA...
        - https://v.douyin.com/XXXXXX/                （短链，302 后落回上面两种）
        - 「3.14 复制打开抖音…https://v.douyin.com/xxx/ …」（分享文本）
        - MS4wLjABAAAA...                            （裸 sec_uid）

    返回 None 的场景交给调用方记 warning 跳过该行，不阻断其它行。
    """
    text = (line or "").strip()
    if not text:
        return None

    # 1) 完整 user 链接（分享文本里也可能直接带完整链接）
    m = _USER_URL_RE.search(text)
    if m:
        return m.group(1)

    # 2) 裸 sec_uid（整行匹配，避免误抓中文描述里的长串）
    if _BARE_SEC_UID_RE.match(text):
        return text

    # 3) 短链：curl_cffi 不跟随 302 取 Location（跟随会跳 iesdouyin 跨域，
    #    实测偶发 3-7s DNS 超时），再对 Location 走上面两条规则。
    m_short = _SHORT_URL_RE.search(text)
    if not m_short:
        # 分享文本里可能是别的 URL（非 v.douyin.com 短链）→ 无法解析
        return None
    location = _resolve_short_url(m_short.group(0))
    if not location:
        return None
    m = _USER_URL_RE.search(location)
    if m:
        return m.group(1)
    return None


def _resolve_short_url(url: str) -> str | None:
    """短链 302 取 Location 头（不跟随重定向），失败返回 None。"""
    try:
        from curl_cffi import requests as creq
        from app.core.douyin.browser import UA
    except ImportError as exc:  # pragma: no cover
        logger.warning("[主页] curl_cffi 不可用，无法解析短链：{}", exc)
        return None
    from app.core.douyin.client import _HEADERS
    for ua in (UA, _MOBILE_UA):
        try:
            resp = creq.get(
                url,
                headers={**_HEADERS, "User-Agent": ua},
                impersonate="chrome",
                allow_redirects=False,
                timeout=15,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                if loc:
                    logger.debug("[主页] 短链解析 {} → {}", url, loc)
                    return loc
            logger.debug(
                "[主页] 短链 {} 未返回重定向（status={}）", url, resp.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[主页] 短链 {} 请求异常（ua={}）：{}", url, ua[:20], exc)
    return None


class BrowserUserPostSession(BrowserSearchSession):
    """用户主页作品采集会话（继承搜索会话复用 launch / scroll / close 骨架）。

    用法（与 BrowserSearchSession 生命周期一致）：

        session = BrowserUserPostSession(profile_dir, headless=None)
        try:
            ids = session.collect_homepage_ids(sec_uids, page_budget=50)
            page = session.get_page()   # 阶段 B 复用（同 persistent_context）
        finally:
            session.close()
    """

    # ---------- 覆写点 ----------

    @staticmethod
    def _match_search_xhr(r) -> bool:
        """覆写：匹配主页作品列表 XHR（父类匹配搜索结果 XHR）。

        命名沿用父类（_on_response 调用它），避免父类代码改动。
        显式排除 post/comment 等子路径，防止误抓非作品列表接口。
        """
        url = getattr(r, "url", "") or ""
        if _POST_API not in url:
            return False
        return not any(x in url for x in _POST_EXCLUDED)

    @staticmethod
    def _sum_aweme_in_body(body: dict) -> int:
        """覆写：主页响应用 aweme_list[]（扁平），不是搜索的 data[]。

        父类 _scroll_loop 每页用它统计条数（DEBUG 日志 + progress 回调）。
        """
        return len(body.get("aweme_list") or [])

    # ---------- 公开 API ----------

    def collect_homepage_ids(
        self,
        sec_uids: list[str],
        page_budget: int = 50,
        *,
        progress_cb: Optional[Callable[[int, int, int, int], None]] = None,
    ) -> list[str]:
        """逐个主页采集作品 aweme_id，去重保序返回。

        与 search_all_for_ids 的角色对等：阶段 A 只拿 ID 列表，不下载不抓详情。

        参数:
            sec_uids: 已解析的 sec_uid 列表（顺序 = 用户填写顺序）
            page_budget: 本轮跨所有主页的翻页总预算（1 页 ≈ 18 条作品）。
                预算耗尽即停止遍历剩余主页（warning 提示），避免 20+ 主页把
                单轮拉到几小时。
            progress_cb: 进度回调，签名
                (当前主页序号, 主页总数, 本轮累计页数, 本轮累计作品数) -> None
        返回:
            去重保序的 aweme_id 字符串列表
        异常:
            SearchBlockedError: 浏览器被关闭 / 被重定向到验证页（需中止整轮）
        """
        self._ensure_open()
        seen: set[str] = set()
        ids: list[str] = []
        total = len(sec_uids)
        if total == 0:
            logger.warning("[主页] 没有任何有效主页 sec_uid，跳过采集")
            return ids
        remaining = max(1, int(page_budget))
        logger.info(
            "[主页] 开始采集：{} 个主页，翻页预算 {} 页", total, remaining,
        )

        visited = 0
        total_pages = 0
        for i, sec_uid in enumerate(sec_uids, 1):
            if remaining <= 0:
                logger.warning(
                    "[主页] 翻页预算已用尽，剩余 {} 个主页本轮不采集"
                    "（可增大拉取轮数或减少主页数）",
                    total - i + 1,
                )
                break
            pages_used = self._collect_one(
                sec_uid, i, total, remaining, progress_cb, seen, ids,
            )
            remaining -= pages_used
            total_pages += pages_used
            visited += 1

        # 与父类 search_all_for_ids 同款出口累加：入口 _captured.clear() 会丢上轮，
        # 终态 progress 用此字段反映真实总页数。
        # 注意不能用 len(self._captured)：每个主页采集前都会 clear，循环结束时
        # 它只剩**最后一个主页**的页数 —— 必须用循环内累加的 total_pages。
        self._total_captured_pages += total_pages
        logger.info(
            "[主页] 采集完成：遍历 {}/{} 个主页，共 {} 页，拿到 {} 个新作品 ID",
            visited, total, total_pages, len(ids),
        )
        return ids

    # ---------- 内部实现 ----------

    def _collect_one(
        self,
        sec_uid: str,
        idx: int,
        total: int,
        page_budget: int,
        progress_cb,
        seen: set[str],
        ids: list[str],
    ) -> int:
        """采集单个主页，返回本页消耗的翻页预算（首屏算 1 页）。

        单主页失败（0 作品 / 页面异常）只记日志继续下一个——主页是一个个独立
        的博主，一个挂掉不该拖垮整轮。
        """
        page = self._page
        self._captured.clear()
        url = f"https://www.douyin.com/user/{sec_uid}"
        try:
            page.goto(url, wait_until="load", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            _raise_if_browser_closed(e, f"goto 主页[{idx}/{total}]")
            logger.warning(
                "[主页] 第 {}/{} 个主页打开失败 sec_uid={} err={}（跳过该主页）",
                idx, total, sec_uid, str(e)[:120],
            )
            return 0

        # 风控：主页被重定向到验证页 → 中止整轮（继续跑只会全废）
        try:
            final_url = page.url
        except Exception as e:  # noqa: BLE001
            _raise_if_browser_closed(e, "读主页 URL")
            final_url = ""
        if _is_captcha_url(final_url):
            raise SearchBlockedError(
                f"主页被重定向到验证页（风控），请到任务页手动执行过验证码后重试"
                f"（sec_uid={sec_uid}，url={final_url}）"
            )

        # 等首屏作品 XHR（15s）。主页首屏可能走 SSR 不发 XHR → 超时走 DOM 兜底。
        first_screen = self._poll_first_screen(idx, total, sec_uid)
        if not first_screen:
            dom_ids = self._collect_by_dom(page, sec_uid, idx, total)
            if dom_ids:
                logger.info(
                    "[主页] 第 {}/{} 个 sec_uid={} 首屏无 XHR，DOM 兜底拿到 {} 个作品",
                    idx, total, sec_uid, len(dom_ids),
                )
                self._append_ids(ids, seen, [{"aweme_id": x} for x in dom_ids])
                self._emit_progress(progress_cb, idx, total, 1, len(ids))
                return 1
            logger.warning(
                "[主页] 第 {}/{} 个 sec_uid={} 没有任何作品（XHR 与 DOM 均无数据，跳过）",
                idx, total, sec_uid,
            )
            return 1  # 首屏已经消耗 1 页预算

        # 滚动加载更多：预算剩余部分留给翻页
        remaining_scroll = max(1, page_budget - 1)
        self._scroll_until(
            target_page=None,
            idle_timeout=20,       # 主页作品少，20s 无新内容即视为到底
            max_pages=remaining_scroll,
        )
        new_items = self._append_ids(ids, seen, self._iter_awemes(self._captured))
        self._emit_progress(progress_cb, idx, total, len(self._captured), len(ids))
        logger.info(
            "[主页] 第 {}/{} 个 sec_uid={} 采集 {} 页 / 新增 {} 个作品（累计 {}）",
            idx, total, sec_uid, len(self._captured), new_items, len(ids),
        )
        return max(1, len(self._captured))

    def _poll_first_screen(self, idx: int, total: int, sec_uid: str) -> bool:
        """等首屏作品 XHR 落库，返回是否拿到。

        不复用父类 _poll_captured_until：父类超时抛 SearchBlockedError，
        而主页首屏无 XHR 是正常情况（SSR），必须降级为 DOM 兜底而非失败。
        """
        import time as _time
        page = self._page
        deadline = _time.time() + 15
        while _time.time() < deadline:
            if self._captured:
                return True
            try:
                page.wait_for_timeout(200)
            except Exception as e:  # noqa: BLE001
                _raise_if_browser_closed(e, "等主页首屏 XHR")
                return False
        logger.debug(
            "[主页] 第 {}/{} 个 sec_uid={} 等首屏 XHR 15s 未捕获",
            idx, total, sec_uid,
        )
        return False

    def _collect_by_dom(self, page, sec_uid: str, idx: int, total: int) -> list[str]:
        """DOM 兜底：从作品卡片 href 提取 aweme_id。

        场景：主页首屏 SSR 直出，浏览器不发 aweme/post/ XHR。此时 wheel 滚动
        也拿不到更多，靠页面已渲染的卡片链接拿到首屏作品。
        """
        ids: list[str] = []
        seen: set[str] = set()
        for sel in ('a[href*="/video/"]', 'a[href*="/note/"]'):
            try:
                hrefs = page.locator(sel).all()
            except Exception as e:  # noqa: BLE001
                _raise_if_browser_closed(e, "DOM 兜底取作品链接")
                return ids
            for loc in hrefs:
                try:
                    href = loc.get_attribute("href") or ""
                except Exception:
                    continue
                m = _DOM_AWEME_ID_RE.search(href)
                if not m:
                    continue
                aid = m.group(1)
                # 只留 /video/：阶段 B 固定 url_kind="video"，图文 ID 会 404
                if "/video/" not in href:
                    continue
                if aid not in seen:
                    seen.add(aid)
                    ids.append(aid)
            if ids:
                break
        return ids

    @staticmethod
    def _iter_awemes(bodies: list[dict]):
        """从捕获的响应体里逐条 yield aweme dict（图文由调用方过滤）。"""
        for body in bodies:
            for aw in (body.get("aweme_list") or []):
                if isinstance(aw, dict):
                    yield aw

    @staticmethod
    def _append_ids(ids: list[str], seen: set[str], awemes) -> int:
        """追加 aweme_id（去重保序 + 滤图文），返回新增条数。

        图文过滤与搜索侧「切视频 tab」达到同一效果：阶段 B 固定用
        url_kind="video" 打开 /video/{id}，图文 ID 打开会 404。
        DOM 兜底路径已按 href 过滤过，这里再过一次是为了兜住 XHR 路径。
        """
        added = 0
        skipped_note = 0
        for aw in awemes:
            aid = aw.get("aweme_id")
            if not aid or aid in seen:
                continue
            if _is_note_aweme(aw):
                skipped_note += 1
                continue
            seen.add(aid)
            ids.append(aid)
            added += 1
        if skipped_note:
            logger.info("[主页] 滤掉 {} 个图文作品（非视频，阶段 B 无法入库）", skipped_note)
        return added

    def _emit_progress(self, progress_cb, idx: int, total: int,
                       pages: int, videos: int) -> None:
        """进度回调（异常不影响采集主流程）。"""
        if progress_cb is None:
            return
        try:
            progress_cb(idx, total, pages, videos)
        except Exception:  # noqa: BLE001
            logger.debug("[主页] 进度回调异常", exc_info=True)