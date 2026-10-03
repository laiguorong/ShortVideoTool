# -*- coding: utf-8 -*-
"""真实抖音客户端实现（基于 2026-09 已验证路线）。

能力地图（探测结论）：
- check_cookie：creator.douyin.com 域身份回源（web_profile）；
- search_videos：搜索接口纯 HTTP 直连（仅需 Cookie + Chrome TLS 指纹，
  2026-09-09 实测免 a_bogus/msToken，见 search_api.py）；
- download_video：douyinvod CDN 直链仅需 UA+Referer，无需 Cookie；
- resolve_share：短链 302 取 ID → 浏览器拦 detail XHR（视频页 CSR 化）；
- 门店拉取：selection_service 直接走 poi_service._in_session 批量路径
  （#141 删 pull_shops 单次封装，生产不再经 client）。

风控策略：全接口由 rate_limiter 全局间隔控制；搜索空 data 视为软拦截
抛 RiskControlError 由上层冷却重试。
"""

import json
import re
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

from app.core.douyin.base import DouyinClient, DouyinClientError, LoginInvalidError, RiskControlError
from app.core.douyin.browser import UA
from app.core.douyin.rate_limiter import rate_limiter
from app.core.douyin.web_profile import fetch_profile

# 通用请求头（抖音系站点校验 UA/Referer）
_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class RealDouyinClient(DouyinClient):
    """真实实现：纯 HTTP（已验证免签接口）+ 浏览器执行器（签名墙接口）。"""

    def __init__(self):
        """初始化（无状态，浏览器执行器为模块级单例懒启动）。"""
        # #分享导入容错：人工等待窗口由调用方显式透传 manual_wait_ms，避免
        # 单例属性被拉取/分享两条调用链相互污染。默认 30s。
        pass

    # ---------- 账号 ----------

    def check_cookie(self, cookie: str) -> dict:
        """Cookie 有效性校验：/user/self 渲染数据回源真实身份。"""
        rate_limiter.acquire()
        profile = fetch_profile(cookie)
        if profile is None:
            if "sessionid" not in cookie:
                raise LoginInvalidError("Cookie 缺少 sessionid，登录态无效")
            # 网络波动或页面结构变化，无法判定——按失效处理由上层 relogin 兜底
            raise DouyinClientError("身份回源失败，无法校验 Cookie")
        rate_limiter.report_success()
        return {
            "valid": True,
            "douyin_id": profile["douyin_id"],
            "uid": profile["uid"],
            "nickname": profile["nickname"],
            "avatar": profile["avatar"],
        }

    # ---------- 选品（creator.douyin.com 真实接口，#449） ----------
    # #141：pull_shops 单次封装已删（生产用 selection_service 走 _in_session 批量路径）。
    # 选品拉取由 selection_service._run_one_round 直接调 poi_service.search_poi_in_session
    # / fetch_poi_detail_in_session（复用 session，高效）。

    # ---------- 素材搜索（纯 HTTP 接口路线，2026-09-09 实测免签） ----------

    def search_videos(self, profile_dir, conditions: dict, page: int,
                      search_id: str = "",
                      _session=None) -> dict:
        """按关键词搜索视频：浏览器持久化会话版（#506 替换 curl_cffi 裸 HTTP）。

        #506 改造：旧版用 cookie 裸 HTTP 调 general/search/single 被 2483 风控。
        根因是搜索接口要完整登录会话（持久化 chromium profile + storage_state +
        IndexedDB + 设备指纹）。新版走 launch_persistent_context（和发布一致）。

        参数:
            profile_dir: 账号持久化 chromium profile 目录（Path）
            conditions: 搜索条件（keyword + sort_type + publish_range + duration_range）
            page: 页码（offset=(page-1)*10）
            search_id: 保留参数占位（浏览器会话无 search_id 概念）
            _session: 复用 BrowserSearchSession（material_service 整轮翻页一个）
        返回:
            {"has_next": bool, "videos": [...解析后的视频 dict]}
        异常:
            RiskControlError 搜索被风控
        """
        from pathlib import Path as _Path
        keyword = _derive_search_keyword(conditions)
        if not keyword:
            return {"has_next": False, "videos": []}
        from app.core.douyin.search_api import (
            DURATION_MAP,
            PUBLISH_TIME_MAP,
            SearchBlockedError,
            search_single,
        )
        raw_sort = conditions.get("sort_type")
        sort_type = 2 if raw_sort is None else int(raw_sort)
        publish_time = PUBLISH_TIME_MAP.get(conditions.get("publish_range") or "any",
                                             PUBLISH_TIME_MAP["any"])
        filter_duration = DURATION_MAP.get(
            conditions.get("duration_range") or "any", ""
        )
        try:
            body = search_single(
                _Path(profile_dir), keyword, page,
                search_id=search_id,
                sort_type=sort_type,
                publish_time=publish_time,
                filter_duration=filter_duration,
                _session=_session,
            )
        except SearchBlockedError as e:
            raise RiskControlError(f"搜索被拦截：{e}") from e
        videos = []
        bad_items = 0  # 任务 #134：字段错位 item 数（顶层 aweme_id 但无 aweme_info 嵌套）
        for item in (body or {}).get("data") or []:
            video = _parse_search_item(item)
            if video:
                videos.append(video)
            elif isinstance(item, dict) and not item.get("aweme_info") and item.get("aweme_id"):
                bad_items += 1
        if bad_items:
            logger.warning(
                "[搜索解析] 本页 {} 条字段错位 item 已跳过（无 aweme_info 嵌套，page={}）",
                bad_items, page)
        if not videos and page == 1:
            # 首页 0 条：搜索词无结果 / 风控软拦截 / 全部字段错位——按风控处理由上层冷却
            msg = "搜索首页无数据（疑似风控软拦截或关键词无结果）"
            if bad_items and not (body or {}).get("data"):
                msg = "搜索首页无数据"
            raise RiskControlError(msg)
        # v22 可观测：has_more + 原始/解析条数，便于排查翻页早退
        raw_items = (body or {}).get("data") or []
        logger.info(
            "[search-videos] page={} has_more={!r} raw_items={} parsed_videos={}",
            page, body.get("has_more"), len(raw_items), len(videos),
        )
        return {"has_next": body.get("has_more") == 1, "videos": videos}

    # ---------- 分享链接解析 ----------

    def resolve_share(self, share_text: str, manual_wait_ms: int = 30000) -> dict:
        """解析分享文本：仅支持视频链接，音乐链接一律拒绝。

        #fix-music-node：分享导入只接视频链接（/video/{vid} 或 /note/{id}）。
        音乐链接（/music/{mid}）抛 DouyinClientError，由 _process_item 写 failed 状态。

        视频链接走 _fetch_aweme_detail（开浏览器拦 /aweme/v1/web/aweme/detail/，
        任务 #131 验证匿名可拿全字段）。

        短链 302 后根据最终 URL 的路径分流；解析到 /music/... 路径直接拒绝。
        """
        from curl_cffi import requests as creq

        rate_limiter.acquire()

        # 1. 提取链接（短码可含连字符，如 5Ob-LbF5RLM；漏 - 会截断短码导致降级跳首页）
        m = re.search(r"https?://v\.douyin\.com/[\w-]+/?", share_text)
        if not m:
            # #fix-music-node：音乐链接直接拒绝
            m_music = re.search(r"https?://www\.douyin\.com/music/(\d+)", share_text)
            if m_music:
                logger.info("[分享解析] 拒绝音乐长链 text={!r:.60} music_id={}",
                            share_text, m_music.group(1))
                raise DouyinClientError(
                    "仅支持视频链接导入，音乐链接请到视频页导入（自动抽取 BGM 入音乐库）")
            m_video = re.search(r"https?://www\.douyin\.com/(?:video|note)/(\d+)", share_text)
            if m_video:
                kind = "note" if "/note/" in m_video.group(0) else "video"
                return self._fetch_aweme_detail(m_video.group(1), url_kind=kind)
            raise DouyinClientError("分享文本中未识别到抖音链接")
        else:
            # 2. 短链解析（任务 #132 改造）：
            #    关键变更 — 不跟随 302，改用 `allow_redirects=False` 取 Location 头，
            #    避免跟随到 `iesdouyin.com` 跨域触发 DNS 解析超时（实测偶发 3-7s）。
            #    Location 形态：
            #    - https://www.douyin.com/video/{vid}
            #    - https://www.douyin.com/note/{id}（图文）
            #    - https://www.iesdouyin.com/share/music/{mid}?from_ssr=1  （音乐，#fix-music-node 拒绝）
            video_id = ""
            music_id = ""
            hops: list[str] = []
            mobile_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17_0 Mobile/15E148 Safari/604.1")
            id_pattern = re.compile(
                r"(?:iesdouyin\.com|www\.douyin\.com)/(?:video|note|music|share/(?:video|music))/(\d+)")
            for ua in (UA, mobile_ua):
                last_err: Exception | None = None
                resp = None
                for attempt in range(3):
                    try:
                        resp = creq.get(m.group(0),
                                        headers={**_HEADERS, "User-Agent": ua},
                                        impersonate="chrome",
                                        allow_redirects=False, timeout=15)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < 2:
                            wait_s = 1 + attempt
                            logger.warning(
                                "[分享解析] 短链请求失败 text={!r:.60} url={} ua={} attempt={} err={} → 等待 {}s 重试",
                                share_text, m.group(0)[:120],
                                "mobile" if ua is mobile_ua else "desktop",
                                attempt + 1, e, wait_s,
                            )
                            import time as _time
                            _time.sleep(wait_s)
                if resp is None:
                    rate_limiter.report_failure()
                    logger.warning(
                        "[分享解析] 短链请求重试 3 次仍失败 text={!r:.60} url={} err={}",
                        share_text, m.group(0)[:120], last_err,
                    )
                    raise DouyinClientError(f"短链请求失败：{last_err}") from last_err
                location = resp.headers.get("Location") or resp.headers.get("location") or ""
                hops.append(f"{resp.status_code} -> {location[:140]}")
                m_loc = id_pattern.search(location)
                if m_loc:
                    target_id = m_loc.group(1)
                    # #fix-music-node：解析到 /music/ 路径直接拒绝
                    if "/music" in location:
                        music_id = target_id
                    else:
                        video_id = target_id
                    break
            # #fix-music-node：音乐链接拒绝（包括短链跳音乐页）
            if music_id:
                logger.info("[分享解析] 拒绝音乐链接 text={!r:.60} music_id={}",
                            share_text, music_id)
                raise DouyinClientError(
                    "仅支持视频链接导入，音乐链接请到视频页导入（自动抽取 BGM 入音乐库）")
            if not video_id:
                logger.warning("[分享解析] 短链跳转未解析到视频 ID text={!r:.60} hops={}",
                               share_text, " | ".join(hops))
                raise DouyinClientError(
                    f"短链跳转异常：未解析到视频 ID（{hops[-1][:80] if hops else m.group(0)[:80]}）")

        # 3. 视频详情：开浏览器加载视频页，拦截 detail XHR（页面自己算签名，绕过 403）
        # 任务 #131：_fetch_aweme_detail 默认匿名即可拿全字段，无需账号 cookie
        # #分享导入容错：默认开 30s 人工等待窗口（0 响应=可能风控过码，让用户有时间操作）；
        # 调用方可按需传 manual_wait_ms 拉长窗口（如重试过验证码场景）。
        return self._fetch_aweme_detail(video_id, manual_wait_ms=manual_wait_ms)

    def _fetch_in_page(self, page, video_id: str, timeout_ms: int = 20000,
                       manual_wait_ms: int = 0,
                       url_kind: str = "video") -> list[dict]:
        """#161：阶段 B 复用阶段 A page 抓详情。

        在传入 page 上注册响应拦截器 → page.goto 详情 URL → event-driven 等 detail XHR 落库。
        不创建新 persistent_context（避免同 profile_dir chromium lock 冲突），
        不创建新 sync_playwright runtime（避免 asyncio loop 冲突）。

        加载模型（替换原 domcontentloaded）：
        - wait_until="load"：等 DOMContentLoaded + 资源加载（图片/脚本/字体）。
          比 domcontentloaded 晚 2-5s，但更接近真人「页面打开完成」感。
        - load 后主动 wait_for_timeout(3000)：模拟真人停留看视频 + 等 React 派发
          更多 XHR（评论/推荐）。配合 event-driven 检测，XHR 已捕获立即返回。
        - 抖音反爬：停留 < 3s 易被识别为机器人，停留 5-10s 节奏更安全。
        - timeout 从 15s 扩到 20s：load 比 domcontentloaded 慢 2-5s，需扩上限。

        监听模型（替换原 polling 1s 粒度）：
        - 注册 page.on("response") 拦截器，匹配 `/aweme/v1/web/aweme/detail/`
          URL 时 captured.append + detail_event.set() 唤醒 settle 循环
        - detail_event 是 threading.Event，同线程 wait() 可被 _on_response 立即 set 唤醒
        - 节省 100ms~1s 检测延迟（polling 1s 粒度 vs event-driven 100ms 内）

        重试策略：page.goto 失败重试 1 次。抖音页面因风控脚本注入延迟 / 视频流 XHR
        阻塞 load 触发偶发 20s+ 超时。首次失败大多是网络抖动，重试大概率成功。

        返回拦截到的响应 JSON 列表（_fetch_aweme_detail 取匹配的 aweme_detail）。
        """
        captured: list[dict] = []
        # event-driven 唤醒：detail XHR 命中时 _on_response 立即 set()，
        # settle 循环 wait(1.0) 立即返回无需等满 1s
        detail_event = threading.Event()

        def _on_response(resp):
            # 注意：detail_event.set() 必须放在 try/except 外（即使 JSON 解析失败
            # 也要 set），确保「XHR 已到达」信号不被解析异常吞掉。captured.append 失败
            # 仅影响本次解析层结果，但 XHR 到达本身是上层等到的关键事件。
            if "/aweme/v1/web/aweme/detail/" not in resp.url:
                return
            detail_event.set()
            try:
                captured.append(resp.json())
            except Exception as e:
                logger.debug(
                    "[详情抓取] XHR JSON 解析失败 video_id={} err={}",
                    video_id, str(e)[:80],
                )
        page.on("response", _on_response)
        try:
            # 首次 page.goto 失败重试 1 次（load 比 domcontentloaded 更慢，超时概率更高）。
            # 重试前 sleep 1s backoff（避免连续命中同一网络抖动）。
            for attempt in range(2):
                try:
                    page.goto(
                        # #fix-music-node：note 链接走 /note/{id}（避免强行打开 /video/{id} → 404）
                        f"https://www.douyin.com/{url_kind}/{video_id}",
                        timeout=timeout_ms, referer="https://www.douyin.com/",
                        wait_until="load",  # ← 等资源全加载（图片/脚本/字体）
                    )
                    break
                except Exception as e:
                    if attempt == 0:
                        logger.debug(
                            "[详情抓取] page.goto 超时 video_id={} attempt=1/2 err={} 1s 后重试",
                            video_id, str(e)[:120],
                        )
                        try:
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        continue
                    raise
            # 模拟真人停留：load 后再 3s 让用户看清视频 + 等 React hydration + 触发
            # 评论/推荐 XHR。这是反爬节奏关键窗口（< 3s 易被识别为机器人）。
            # 固定 3s 节奏（不抖动）保证页面停留时间一致，避免节奏波动被识别。
            # page 异常断开时 try/except 兜底，不阻断主流程（finally 内 remove_listener
            # 会清理 listener）。
            try:
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.debug(
                    "[详情抓取] 真人停留异常 video_id={} err={}（继续等 XHR）",
                    video_id, str(e)[:80],
                )
            # event-driven 等待 detail XHR：捕获到立即 set() 唤醒，
            # 上限 5s 兜底（极端情况下 XHR 在停留后才发出）
            for _ in range(5):
                if detail_event.wait(1.0):
                    break
            # manual_wait 阶段：用 detail_event 检查而非 captured（XHR 命中但 JSON 解析
            # 失败时 captured 仍为空，但不需要再等 manual_wait；detail_event 已 set 即说明
            # 至少收到过一次响应，只是解析层失败）。
            if not detail_event.is_set() and manual_wait_ms > 0:
                deadline = manual_wait_ms // 1000
                for _ in range(deadline):
                    if detail_event.wait(1.0):
                        break
        finally:
            # 解除拦截器避免下一页 goto 重复 append
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
        return captured

    def _fetch_aweme_detail(self, video_id: str, cookie: str = "",
                            manual_wait_ms: int = 0,
                            account_id: str = "",
                            page=None,
                            url_kind: str = "video") -> dict:
        """公共方法（任务 #131）：通过浏览器拦 detail XHR 拿视频完整数据。

        拉取侧 + 分享侧共用：搜索列表 `aweme_info` author 节点字段值不可信（follower=0），
        必须开详情页拦 `/aweme/v1/web/aweme/detail/` 拿 54 字段 author 完整数据。

        任务 #131 E2E 实测：匿名（cookie=""）即可拿全字段（与账号登录态一致），
        cookie 仅在需要账号增强（如 BGM 来源标记）时传入。

        #fix-unify-profile-storage：传 account_id 时统一走账号持久化 profile（user_data_dir），
        # 不再读 storage.json / storage_state 注入（避免 storage.json 缺失时降级到纯 cookies
        # → 签名失败 0 响应）。

        #161 复用阶段 A page：传 page 参数时跳过 BrowserActor 独立 launch_persistent_context，
        # 直接在传入 page 上注册拦截器 + page.goto 详情 URL，避免同 profile_dir chromium lock
        # 冲突 + asyncio loop 冲突。**page 复用时 account_id 不生效**——profile 由 page 所在
        # context 决定（阶段 A 创建时已绑定账号 profile）。

        #fix-music-node：url_kind 决定打开的详情页 URL 类型：
        - "video"（默认）：https://www.douyin.com/video/{id} → 普通视频
        - "note"：https://www.douyin.com/note/{id} → 图文（任务 #fix-music-node 修 note 链接 404）

        参数:
            video_id: 抖音视频/图文 ID（短链 302 解析后 / 搜索列表直接拿）
            cookie: 账号登录态（默认空串=匿名；任务 #131 验证匿名可拿全字段）
            manual_wait_ms: 人工等待窗口毫秒数（0 响应时让人过验证码，>0 进入等待）。
                分享导入手动重试时通常传 30000~60000（30s~60s）。
            account_id: 账号 ID（#fix-unify-profile-storage：传时走 accounts/<id>/profile/）。
                page 复用路径下不生效。
            page: 复用阶段 A 的 Playwright page（None 时回退到 BrowserActor 独立 launch）
            url_kind: "video" / "note"（#fix-music-node）
        返回:
            统一视频字段 dict（_parse_detail_item 输出）
        异常:
            DouyinClientError: 浏览器层失败 / 详情接口 0 响应
        """
        from app.core.douyin.browser import browser_actor, _cookie_header_to_playwright
        # #fix-unify-profile-storage：传 account_id 时走持久化 profile（user_data_dir）路径
        user_data_dir = None
        cookies = None
        if account_id:
            from app.services.douyin_account import get_profile_dir
            user_data_dir = get_profile_dir(account_id)
        if user_data_dir is None:
            # 降级：cookies 路径或匿名
            cookies = _cookie_header_to_playwright(cookie, ".douyin.com") if cookie else None
        try:
            if page is not None:
                # #161：阶段 B 复用阶段 A page（同 persistent_context，无 chromium lock 冲突）。
                # 直接 page.goto 详情 URL + 注册响应拦截器，跳开 BrowserActor
                # launch_persistent_context（撞同 profile_dir lock）。
                # timeout 20000：wait_until=load 比 domcontentloaded 慢 2-5s，需扩上限。
                bodies = self._fetch_in_page(page, video_id, timeout_ms=20000,
                                              manual_wait_ms=manual_wait_ms,
                                              url_kind=url_kind)
            else:
                # #fix-music-node：note 链接走 /note/{id}（避免被强行打开 /video/{id} → 404）
                detail_url = f"https://www.douyin.com/{url_kind}/{video_id}"
                bodies = browser_actor.run(
                    detail_url,
                    capture=lambda u: "/aweme/v1/web/aweme/detail/" in u,
                    cookies=cookies, user_data_dir=user_data_dir,
                    timeout_ms=20000, manual_wait_ms=manual_wait_ms,
                )
        except Exception as e:  # noqa: BLE001 浏览器层失败
            rate_limiter.report_failure()
            # 兜底 reason：异常无消息时（Playwright 部分底层异常 args 为空），
            # 不输出 "详情页加载失败：" 这种结尾冒号的尴尬文案。
            msg = str(e) or repr(e)
            reason = f"详情页加载失败：{msg}" if msg else (
                f"详情页加载失败（{type(e).__name__}，无消息，建议稍后重试）")
            # #fix-browser-closed：浏览器被用户手动关闭（TargetClosedError）属于用户主动操作，
            # 静默 WARNING 不刷 traceback；其他异常仍走 logger.exception 全栈打印。
            if "closed" in msg.lower() or "TargetClosedError" in type(e).__name__:
                logger.warning(
                    "[详情获取] 浏览器已关闭 video_id={} type={}",
                    video_id, type(e).__name__)
            else:
                logger.exception(
                    "[详情获取] 浏览器层异常 video_id={} reason={} type={} args={}",
                    video_id, reason, type(e).__name__, e.args)
            raise DouyinClientError(reason) from e
        for body in bodies:
            aw = body.get("aweme_detail") or {}
            if str(aw.get("aweme_id")) == video_id:
                rate_limiter.report_success()
                return _parse_detail_item(aw, video_id)
        rate_limiter.report_failure()
        # 0 响应时：可能是风控验证码拦截，提示用户手动重试过验证码
        if manual_wait_ms > 0:
            raise DouyinClientError(
                "详情页加载失败：仍未拦截到响应（可能错过验证码窗口，请再重试）")
        raise DouyinClientError(
            "详情页加载失败：未拦截到 detail 接口响应（可能触发风控/验证码，"
            "请到任务页对该条点重试以启用人工过码窗口）")

    def _share_cookie(self) -> str:
        """分享解析用登录态（可选）：任务 #131 验证匿名即可拿全字段，默认返回空串。

        仅当调用方需要账号增强（如 BGM 来源标记、互动数据二次校验）时才取账号 cookie。
        无账号 / 账号失效 / 数据库未初始化 → 空串匿名。
        """
        try:
            from app.db import get_db
            from app.core import crypto as _crypto
            d = get_db()
            row = d.query_one(
                "SELECT cookie_encrypted FROM account WHERE deleted=0 AND status='normal' LIMIT 1")
            return _crypto.decrypt(row["cookie_encrypted"]) if row else ""
        except Exception:  # noqa: BLE001 数据库未初始化等场景按匿名处理
            return ""

    # ---------- 视频下载 ----------

    def download_video(self, url: str, save_path: str) -> str:
        """CDN 直链下载（仅需 UA+Referer，无需 Cookie）。

        #P0-7：原 urllib.request.urlopen 缺 TLS 指纹伪装，抖音 CDN 对纯 Python
        urllib 返 403/风控页。改用 curl_cffi 模拟 Chrome 124 指纹
        （与 playwright bundled chromium-1234 TLS ClientHello 对齐）。
        """
        from curl_cffi import requests as creq
        rate_limiter.acquire()
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = creq.get(
                url,
                headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                impersonate="chrome124",
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            with open(p, "wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:  # noqa: BLE001 网络类失败统一抛客户端异常
            # 失败时清理残文件（避免 #material 6 处磁盘垃圾）
            p.unlink(missing_ok=True)
            rate_limiter.report_failure()
            raise DouyinClientError(f"视频下载失败：{e}") from e
        if p.stat().st_size < 1024:
            p.unlink(missing_ok=True)
            rate_limiter.report_failure()
            raise DouyinClientError("下载内容异常（小于 1KB，疑似风控页）")
        rate_limiter.report_success()
        return str(p)

    # ---------- 以下待账号条件成熟后接入 ----------

    def publish_video(self, cookie: str, video_path: str, title: str, topics: str,
                      shop_poi_id: Optional[str],
                      account_id: Optional[str] = None,
                      schedule: str = "",
                      allow_save: Optional[bool] = None) -> dict:
        # #发布链路（#calc_mode + #E2E）：creator.douyin.com 浏览器会话路线
        # - storage_state：从账号目录加载（account_id 必传，否则降级用 cookie）
        # - 上传 + 填字段 + 点发布 + 拦截 create_v2 拿 item_id
        # 实现细节见 app.core.douyin.publish_actions.sync_publish_video
        from app.core.douyin.publish_actions import sync_publish_video
        from app.services.douyin_account import get_account_manager
        from pathlib import Path
        from datetime import datetime

        storage_state = None  # 保留为 None（#fix-unify-profile-storage 不再依赖 storage_state）
        profile_dir: Path | None = None
        account_dir: Path | None = None
        if account_id:
            from app.services.douyin_account import get_profile_dir
            mgr = get_account_manager()
            # #fix-unify-profile-storage：profile_dir 替代 storage_state 注入
            profile_dir = get_profile_dir(account_id)
            account_dir = mgr._account_dir(account_id)

        # #fix-unify-profile-storage：profile_dir 有 Cookies 文件即视为有效
        # （cookie 有效性由 launch_persistent_context 启动后 chromium 自动校验，过期时 403
        # 上层 publish_video 兜底；本处只做"profile 缺失"快速失败）
        if account_id and profile_dir is not None:
            has_cookies = (profile_dir / "Default" / "Network" / "Cookies").exists() or \
                          (profile_dir / "Default" / "Cookies").exists()
            if not has_cookies:
                raise LoginInvalidError(
                    f"账号 {account_id} profile 缺失或 cookies 为空，请先登录"
                )

        # 定时发布：当前时间 < schedule + 10min 视为过期（抖音拒接过期）
        if schedule:
            try:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        when = datetime.strptime(schedule, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    when = None
                if when and when <= datetime.now():
                    return {"success": False, "online_video_id": None,
                            "message": f"定时发布时间已过：{schedule}"}
            except Exception:
                pass

        # location：POI 名称（base.signature 没强制；空时跳过 POI）
        location = shop_poi_id or ""

        try:
            return sync_publish_video(
                # #452：profile_dir 替代 storage_state dict 注入
                profile_dir=profile_dir,
                video_path=Path(video_path),
                title=title,
                declaration="无需添加自主声明",
                location=location,
                schedule=schedule or "",
                allow_save=allow_save,
                account_dir=account_dir,
                # #bugfix：走独立 chromium 进程（不复用 browser_actor 单例），
                # 避免同 fingerprint 短时间多次发被抖音风控关联。
                reuse_browser_actor=False,
                # #bugfix：403 后等用户过二次验证最长 300s（默认 180 太短，用户扫码来不及）
                risk_wait_seconds=300,
            )
        except Exception as exc:
            # 兜底：网络/浏览器异常归类
            msg = str(exc) or repr(exc)
            if "登录" in msg or "cookie" in msg.lower() or "LoginInvalid" in msg:
                raise LoginInvalidError(msg) from exc
            return {"success": False, "online_video_id": None,
                    "message": f"发布异常：{msg}"}


# ---------- 模块内工具函数 ----------

def _derive_search_keyword(conditions: dict) -> str:
    """从拉取条件推导搜索词（v120 简化：仅依赖 keyword 与 title_regex）。

    优先级：显式 keyword > 标题正则中提取的连续汉字/字母词（取最长的一段）。
    旧版 topics/desc_keywords 推导分支已删除（前端不再支持），保留兼容读取但忽略。
    """
    kw = (conditions.get("keyword") or "").strip()
    if kw:
        return kw
    pattern = (conditions.get("title_regex") or "").strip()
    if pattern:
        # 取正则里最长的纯词法段（汉字/字母/数字）
        words = re.findall(r"[一-龥A-Za-z0-9]{2,}", pattern)
        if words:
            return max(words, key=len)
    return ""


def _parse_music(aw: dict) -> dict:
    """解析 aweme 的 music 节点 → 背景音轨信息（BGM 同步入音乐库用）。

    抖音曲库/视频原声都挂在 music 节点；版权受限曲目 play_url 为空 → 返回空 dict，
    下游据此跳过 BGM 拉取（不报错、不影响视频入库）。

    字段来源（#fix-music-node）：统一从 music 节点读，不再走 music_detail HTTP 接口。
    - 封面：music.cover_medium.url_list[0]，无则降级 cover_thumb
    - 头像：music.avatar_medium.url_list[0]（音乐作者头像，非视频作者）
    """
    m = aw.get("music") or {}
    play_list = ((m.get("play_url") or {}).get("url_list")) or []
    url = play_list[0] if play_list else ""
    if not url:
        return {}
    # author 双形态兼容：真实详情接口多为字符串（原声作者名），
    # 部分接口/曲库形态为 dict（取 nickname）
    raw_author = m.get("author")
    if isinstance(raw_author, dict):
        author = (raw_author.get("nickname") or "").strip()
    elif isinstance(raw_author, str):
        author = raw_author.strip()
    else:
        author = ""
    # duration/audition_duration 单位均为秒（实测 111 = 1分51秒），转毫秒统一
    duration = int(m.get("duration") or m.get("audition_duration") or 0) * 1000
    music_id = str(m.get("mid") or "")
    # 封面：cover_large → cover_medium → cover_thumb 降级（与 _parse_music_info 对齐；
    # #fix-music-node：aweme.music 节点通常无 cover_large，但部分曲库形态会有）
    cover_url = ""
    for ck in ("cover_large", "cover_medium", "cover_thumb"):
        cu = ((m.get(ck) or {}).get("url_list")) or []
        if cu and cu[0]:
            cover_url = cu[0]
            break
    cover_url = _normalize_image_url(cover_url) if cover_url else ""
    # 作者头像：avatar_medium
    avatar_url = ""
    au = ((m.get("avatar_medium") or {}).get("url_list")) or []
    if au and au[0]:
        avatar_url = au[0]
    avatar_url = _normalize_image_url(avatar_url) if avatar_url else ""
    return {
        # music.mid 是原声/曲目的唯一 ID（同一首原声多视频共用，用于去重）
        "music_id": music_id,
        "title": (m.get("title") or "").strip() or "未知原声",
        "author": author,
        "duration_ms": duration,
        "download_url": url,
        # 原声聚合页链接（mid 拼接，#45）
        "share_url": f"https://www.douyin.com/music/{music_id}" if music_id else "",
        # 封面/头像：直接从 music 节点读（统一路径，不再走 music_detail）
        "cover_url": cover_url,
        "avatar_url": avatar_url,
    }


def _normalize_image_url(url: str) -> str:
    """音乐封面 URL 路径段 `.heic` → `.jpeg`（大小写不敏感，#封面空白修复）。

    参考 `avatar_cache.py` 头像 URL 漂移修复（行 87）：抖音图床扩展名按需切换，
    `.heic` 在 Chromium / Webkit 上不可解码 → 统一换 `.jpeg` 保证浏览器显示。

    只动 path 段（`?` 前）的 `.heic` 后缀，query 参数里的 `.heic` 是签名/参数值
    不应误伤。
    """
    if not url:
        return url
    if "?" in url:
        path, query = url.split("?", 1)
    else:
        path, query = url, ""
    path = re.sub(r"\.heic\b", ".jpeg", path, flags=re.IGNORECASE)
    return f"{path}?{query}" if query else path


def _parse_aweme_common(aw: dict, fallback_id: str, source: str) -> Optional[dict]:
    """搜索/详情接口共用的 aweme 解析（任务 #136）。

    抖音搜索 `aweme_info` 与详情 `aweme_detail` 顶层结构高度相似（都有
    `aweme_id / video / author / statistics / music` 等），差异在：
    - 搜索 author 节点只含基础字段（无 follower_count/total_favorited）
    - 详情 author 节点含用户主页字段（follower_count/total_favorited/aweme_count）
    - 搜索有 `text_extra / desc` 用于话题标签；详情有 `image_infos / chapter_list`

    本函数只抽「完全相同结构」的字段（不依赖数据源差异），返回统一 dict。
    各自 `_parse_search_item` / `_parse_detail_item` 在此基础上叠加自己的特化字段
    （topics / desc / 长视频 / 多图等）。

    参数:
        aw: aweme_info（搜索）或 aweme_detail（详情）节点
        fallback_id: 兜底视频 ID（解析无 aweme_id 时使用）
        source: 'search' / 'detail' — 用于日志标记，调用方负责字段差异补全
    返回:
        统一字段 dict；aweme_id 缺失返 None
    """
    aweme_id = str(aw.get("aweme_id") or fallback_id or "").strip()
    if not aweme_id or aweme_id in ("0", "-"):
        return None
    video = aw.get("video") or {}
    play = video.get("play_addr") or {}
    play_list = play.get("url_list") or []
    # 抖音详情接口 video 节点带多个直链字段，2026-09 实测验证：
    # - play_addr_* 系列（play_addr / play_addr_h264 / play_addr_265）= **无水印**（用户肉眼判断确认）
    # - download_addr / download_suffix_logo_addr = **带水印**（高码率 12.4MB 但强制水印）
    # 任务要求严格无水印：只采用 play_addr_* 系列，不带水印兜底——全部失败就报错，
    # 避免误下载到带水印版本。
    #
    # 优先级（无水印 + 默认播放字段优先）：
    # 1. play_addr（原始 720p play_addr，多 CDN 节点 7.16MB）
    # 2. play_addr_h264（H.264 显式声明，7.16MB）
    # 3. play_addr_265（HEVC，最低码率 2.4MB，最后兜底）
    #
    # 空值过滤：服务端偶发返回 url_list=[null, ""] 等脏数据，下游 _download_with_retry
    # 不能识别 None / 空串为有效 URL，会 curl 进 download_video 触发 404 → 错换 CDN 节点。
    play_h264 = (video.get("play_addr_h264") or {}).get("url_list") or []
    play_265 = (video.get("play_addr_265") or {}).get("url_list") or []
    # 拼接顺序：play_addr → h264 → 265，全失败时 dl_list 为空 → 上层报"无无水印版本"
    # 元素为 (field, url) 元组，_download_with_retry 拿 field 写日志便于排查节点归属
    dl_list = [(f, u) for f, lst in (
        ("play_addr", play_list), ("play_addr_h264", play_h264), ("play_addr_265", play_265)
    ) for u in lst if u]
    play_list = [u for u in play_list if u]
    # 宽高三源提取（任务 #133 复盘）：dimension → video.width/height → play_addr.width/height
    dim = video.get("dimension") or {}
    w, h = int(dim.get("width") or 0), int(dim.get("height") or 0)
    if not (w and h):
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
    if not (w and h):
        w = int(play.get("width") or 0)
        h = int(play.get("height") or 0)
    width, height = (w, h) if (w and h) else (0, 0)
    orientation = ("horizontal" if w >= h else "vertical") if (w and h) else "vertical"
    duration_ms = int(video.get("duration") or video.get("duration_ms") or 0)
    # 任务 #509：图声视频警告。aweme_type ∈ {2, 68} 已在 _parse_search_item 过滤，
    # 此处都是普通视频（aweme_type ∈ {0,1,4}）。宽高 = 0 + 时长 > 0 + play_addr 非空
    # → 抖音 play_addr CDN 可能返回 audio-only m4a（用于原声提取场景），
    # 下游抽帧必然 -22 失败。仅打 WARNING 标记，不阻止下载（部分图声视频用户可能需要）。
    # WARNING 放在 w/h + duration_ms 都定义完后，复用不重复 int(or 0)。
    if not (w and h) and duration_ms > 0 and play_list:
        logger.warning(
            "[搜索解析] 疑似图声视频 {}：aweme_type={} 时长={}ms 但宽高=0，"
            "play_addr CDN 可能返回 m4a 音频",
            aweme_id, aw.get("aweme_type"), duration_ms,
        )
    author = aw.get("author") or {}
    avatar_list = ((author.get("avatar_thumb") or {}).get("url_list")) or []
    stats = aw.get("statistics") or {}
    return {
        "video_id": aweme_id,
        "duration_ms": duration_ms,
        "orientation": orientation,
        "width": width,
        "height": height,
        "resolution": f"{w}x{h}" if (w and h) else "",
        "download_url": (dl_list[0][1] if dl_list else ""),  # dl_list[0] = (field, url)
        # 仅 3 个无水印 play_addr_* 字段拼接（已剔除 download_addr / suffix_logo 带水印字段）：
        # 顺序 play_addr → play_addr_h264 → play_addr_265（默认播放字段优先）
        # _download_with_retry 遍历本字段逐 URL 尝试，节点失败自动跳下一条；
        # download_urls 元素为 (field, url) 元组，日志带 field 便于排查节点归属；
        # dl_list 为空时上层报"无无水印版本"，不下载带水印版本（任务硬性要求）。
        "download_urls": dl_list,
        "share_url": f"https://www.douyin.com/video/{aweme_id}",
        "author_nickname": (author.get("nickname") or "").strip(),
        "author_douyin_id": str(author.get("unique_id") or author.get("short_id") or ""),
        "author_avatar": avatar_list[0] if avatar_list else "",
        # 任务 #136+#131 E2E 实测：搜索接口 author 节点含 follower_count/total_favorited/
        # signature 字段，但未登录或非该账号粉丝看到的值不可信（实测 follower_count=0
        # 而详情接口拿到 816068）。拉取侧必须走 _fetch_aweme_detail 补抓真实值。
        "author_sec_uid": str(author.get("sec_uid") or ""),
        "author_signature": (author.get("signature") or "").strip(),
        "author_follower_count": int(author.get("follower_count") or 0),
        "author_total_favorited": int(author.get("total_favorited") or 0),
        # 互动数据
        "digg_count": int(stats.get("digg_count") or 0),
        "comment_count": int(stats.get("comment_count") or 0),
        "collect_count": int(stats.get("collect_count") or 0),
        "share_count": int(stats.get("share_count") or 0),
        # 封面 + 发布时间
        "cover_url": ((video.get("cover") or {}).get("url_list") or [""])[0] or "",
        "publish_time": "",  # 由调用方按 aw.create_time 格式化（搜索/详情字段一致）
        # 背景音轨（BGM 同步拉取用）
        "music": _parse_music(aw),
        "_source": source,  # 调试用：标记数据来源，便于排查字段差异
    }


def _parse_detail_item(aw: dict, fallback_id: str) -> dict:
    """解析详情接口 aweme_detail → 统一视频字段（任务 #136：委托 _parse_aweme_common）。

    详情接口 author 节点含完整作者主页字段（follower_count / total_favorited）。
    """
    parsed = _parse_aweme_common(aw, fallback_id, source="detail")
    if parsed is None:
        # aweme_id 缺失时回退到最小 dict（与旧实现保持兼容，绝不应走到这）
        return {"video_id": fallback_id, "title": "无标题"}
    raw_title = (aw.get("desc") or "").strip().split("\n")[0][:50] or "无标题"
    from datetime import datetime
    publish_time = ""
    if aw.get("create_time"):
        try:
            publish_time = datetime.fromtimestamp(int(aw["create_time"])).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            publish_time = ""
    # detail 接口特有：title / author_title / publish_time 格式化
    parsed["title"] = raw_title
    parsed["author_title"] = raw_title
    parsed["publish_time"] = publish_time
    # 任务 #137：详情接口用 caption 字段存话题标签（"@xxx创作的原声 #话题1 #话题2"），
    # 与 _parse_search_item 从 text_extra 拼接 topics 字段对齐。detail 解析也补
    # topics 便于前端统一展示。
    caption = (aw.get("caption") or "").strip()
    parsed["topics"] = caption if caption else ""
    return parsed


def _is_music_play_url(url: str) -> bool:
    """判断 play_addr 是否指向音乐 CDN（任务 #40 实测发现）。

    抖音搜索 data 数组混入 BGM 项时 play_addr 指向
    `sf*-cdn-tos.douyinstatic.com/obj/ies-music/...mp3`，与正常视频
    `v*-weba.douyinvod.com/.../video/...` 域名不同；用于 `_parse_search_item`
    过滤音乐误入。
    """
    if not url:
        return False
    u = url.lower()
    return ("ies-music" in u) or ("douyinstatic.com/obj/ies-music" in u)


def _parse_search_item(item: dict) -> Optional[dict]:
    """解析搜索结果单条：data[i].aweme_info → 统一视频字段。

    过滤规则：
    - 字段错位 item（顶层有 aweme_id 但无 aweme_info 嵌套）：服务端降级时返回
      的「推荐卡片」模板，share_url/download_url/author 绑三个不同实体。
      任务 #134：删 fallback，宁可当 None 跳过，让 _run_pull_round 记日志。
    - 空 aweme_info 项：aweme_id 为空/None/"0"/"-" 等真值假值都视为无效
    - BGM 误入：抖音搜索 data 数组可能混入 aweme_music（play_addr 指向
      sf11-cdn-tos.douyinstatic.com/obj/ies-music/...mp3）→ 视为 music 项跳过
    - 任务 #129：默认过滤非视频内容。抖音 aweme_type 枚举：
        0 / 4 = 普通视频、1 = 普通视频、2 = 图文、68 = 图文/实况
      aweme_type ∈ {2, 68} 时 video.play_addr.url_list 为空 + duration=0，
      后续 ffmpeg 抽帧必失败被误判「字幕/人脸检测抽帧失败」误杀。
      非视频项直接返回 None 让上层 _run_pull_round 跳过。
    """
    # 任务 #134：只接受嵌套形态 data[i].aweme_info。顶层 item 含 aweme_id 但无
    # aweme_info 嵌套的「推荐卡片」形态字段错位严重（服务端拼装的 aweme_id /
    # video.play_addr / author 来源三个不同实体），不再 fallback 解析。
    aweme_info = item.get("aweme_info")
    if not isinstance(aweme_info, dict) or not aweme_info:
        logger.debug("[搜索解析] 跳过字段错位 item（无 aweme_info 嵌套，item keys={}）",
                     sorted(item.keys())[:8])
        return None
    aw = aweme_info
    aweme_id = str(aw.get("aweme_id") or "").strip()
    if not aweme_id or aweme_id in ("0", "-"):
        return None
    aweme_type = aw.get("aweme_type")
    if aweme_type in (2, 68):
        logger.debug("[搜索解析] 跳过非视频项 {}（aweme_type={} 图文/实况）",
                     aweme_id, aweme_type)
        return None
    video = aw.get("video") or {}
    play_list = (video.get("play_addr") or {}).get("url_list") or []
    # BGM 过滤：play_addr 指向 ies-music CDN + 视频字段残缺（cover.url_list 为空
    # 或 duration 为 0）→ 视为 aweme_music 音乐项，跳过不入视频流。
    if play_list and _is_music_play_url(play_list[0]):
        cover_list = (video.get("cover") or {}).get("url_list") or []
        if not cover_list or not (video.get("duration") or 0):
            return None
    # 任务 #136：共用解析函数 _parse_aweme_common 取通用字段（author/video/stats）。
    # 搜索接口 author 节点不含 follower_count/total_favorited，命中分支返 0（接口限制）。
    parsed = _parse_aweme_common(aw, aweme_id, source="search")
    if parsed is None:
        return None
    # 搜索接口特有：topics / desc / 横竖屏兜底（orientation=None 允许客户端二次判定）
    desc = (aw.get("desc") or "").strip()
    topics = "".join(
        f"#{seg.get('hashtag_name', '')}" for seg in (aw.get("text_extra") or [])
        if seg.get("hashtag_name")
    )
    from datetime import datetime
    publish_time = ""
    if aw.get("create_time"):
        try:
            publish_time = datetime.fromtimestamp(int(aw["create_time"])).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            publish_time = ""
    raw_title = desc.split("\n")[0][:50] or "无标题"
    parsed["title"] = raw_title
    parsed["author_title"] = raw_title
    parsed["desc"] = desc[:500]
    parsed["topics"] = topics
    parsed["publish_time"] = publish_time
    # 搜索接口 orientation 在 dimension 缺失时返 None（任务 #133）：
    # 客户端 _match_conditions 按 width/height 二次判定。
    if parsed.get("orientation") == "vertical" and not (parsed.get("width") and parsed.get("height")):
        parsed["orientation"] = None
    return parsed
